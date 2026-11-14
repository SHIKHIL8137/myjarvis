"""MCP client manager built on the official `mcp` SDK (2.x, verified on Python 3.14).

Security model (all external content is untrusted):
* every server is disabled by default and spawned only when the owner enables it; bounded restarts, never respawned in a loop
* tools are exposed as ``<server>.<tool>``; only owner-vouched / server-annotated read-only tools run without approval
* write / send / delete / unknown tools go through the existing ``ApprovalService`` (single approval system)
* "critical" calls (owner-configured keywords, e.g. locks/alarms) additionally need spoken confirmation
* every invocation is audited through ``jarvis.audit.record_external`` (argument *names* only, never values)
* results are secret-redacted and wrapped with ``wrap_untrusted`` before reaching the model or memory
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import audit
from .config import MCPServerSpec, Settings
from .security.pii_firewall import redact_secrets
from .security.untrusted import UNTRUSTED_DATA_POLICY, memory_safe, wrap_untrusted  # noqa: F401  (memory_safe re-exported)

log = logging.getLogger("jarvis.mcp")

# Verbs that make a tool state-changing. Matched on tokens of the tool name only.
_WRITE_TOKENS = frozenset(
    "delete remove drop destroy erase clear wipe send post publish write create add insert update set put patch edit move rename "
    "run exec execute shell install uninstall lock unlock open close arm disarm turn toggle trigger reset restart kill stop start "
    "pay transfer commit push merge upload call invoke apply save reply forward cancel approve".split()
)
_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


class MCPError(Exception):
    pass


def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if t]


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def classify_tool(spec: MCPServerSpec, tool: dict, arguments: dict | None = None) -> dict:
    """Return {"level": read|write|critical, "reason": str}. Conservative: anything not clearly read-only is write.

    critical = a state-changing call touching an owner-configured critical keyword (locks, covers, alarms...):
    needs approval AND spoken confirmation. Merely *reading* such a device's state stays a read.
    """
    name = tool["name"]
    toks = set(_tokens(name))
    if name in spec.read_only_tools:
        return {"level": "read", "reason": "owner-vouched read-only tool"}
    if tool.get("read_only_hint") is True and not toks & _WRITE_TOKENS:
        return {"level": "read", "reason": "server annotates readOnlyHint"}
    reason = "name implies a state change" if toks & _WRITE_TOKENS else "unknown tool: conservative default"
    crit = {k.strip().lower() for k in spec.critical_keywords if k.strip()}
    if crit:
        seen = set(toks)
        for sv in _walk_strings(arguments or {}):
            seen.update(_tokens(sv))
        if seen & crit:
            return {"level": "critical", "reason": "state change touching a critical keyword (" + ", ".join(sorted(seen & crit)) + ")"}
    return {"level": "write", "reason": reason}


def resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR} references from the process environment at spawn time (credentials are never stored in settings)."""
    out: dict[str, str] = {}
    for k, v in env.items():
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v or "")
        if m:
            if m.group(1) not in os.environ:
                raise MCPError(f"environment variable {m.group(1)} (for {k}) is not set")
            out[k] = os.environ[m.group(1)]
        else:
            out[k] = v
    return out


def _spoken_digits(text: str) -> str:
    out = []
    for w in re.findall(r"[a-z]+|\d", (text or "").lower()):
        out.append(_DIGIT_WORDS.get(w, w if w.isdigit() else ""))
    return "".join(out)


class _Call:
    def __init__(self, tool: str, args: dict, timeout: float, future: asyncio.Future):
        self.tool, self.args, self.timeout, self.future = tool, args, timeout, future
        self.task: asyncio.Future | None = None


class ServerRuntime:
    """One persistent stdio session owned by a single asyncio task (required by the SDK's anyio scopes)."""

    def __init__(self, name: str, spec: MCPServerSpec):
        self.name, self.spec = name, spec
        self.state = "stopped"  # stopped | starting | ready | crashed | failed
        self.tools: list[dict] = []
        self.restarts = 0
        self.last_error = ""
        self.pid: int | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[_Call | None] = asyncio.Queue()
        self._ready: asyncio.Future | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready = loop.create_future()
        self.state, self.last_error = "starting", ""
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self.spec.startup_timeout_s)
        except BaseException as e:
            msg = "startup timed out" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__)
            await self.stop()
            self.state, self.last_error = "failed", msg
            if isinstance(e, asyncio.CancelledError):
                raise
            raise MCPError(f"{self.name}: {msg}") from None

    async def _run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            cmd = shutil.which(self.spec.command) or self.spec.command
            params = StdioServerParameters(command=cmd, args=list(self.spec.args), env=resolve_env(self.spec.env) or None)
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = [self._tool_dict(t) for t in listed.tools]
                    self.state = "ready"
                    if self._ready and not self._ready.done():
                        self._ready.set_result(True)
                    while True:
                        item = await self._queue.get()
                        if item is None:
                            break
                        if not await self._serve(session, item):
                            break
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001  (ExceptionGroup from anyio task groups included)
            self.last_error = _root_message(e)
            if self._ready and not self._ready.done():
                self._ready.set_exception(MCPError(self.last_error))
            self.state = "crashed"
        finally:
            self.pid = None
            if self.state in ("ready", "recycling"):
                self.state = "stopped"
            self._fail_pending("server stopped")

    async def _serve(self, session, item: _Call) -> bool:
        """Run one call. Returns False when the session must be recycled (timeout, cancel, crash)."""
        item.task = asyncio.ensure_future(asyncio.wait_for(session.call_tool(item.tool, item.args), item.timeout))
        try:
            res = await item.task
        except asyncio.CancelledError:
            self.state, self.last_error = "recycling", "call cancelled; session recycled"
            if not item.future.done():
                item.future.set_exception(MCPError("call cancelled"))
            return False
        except asyncio.TimeoutError:
            self.state, self.last_error = "recycling", "call timed out; session recycled"
            if not item.future.done():
                item.future.set_exception(MCPError(f"timed out after {item.timeout:g}s"))
            return False
        except BaseException as e:  # noqa: BLE001
            self.state, self.last_error = "crashed", _root_message(e)
            if not item.future.done():
                item.future.set_exception(MCPError(self.last_error))
            return False
        if not item.future.done():
            item.future.set_result(res)
        return True

    def _fail_pending(self, why: str) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item and not item.future.done():
                item.future.set_exception(MCPError(why))

    @staticmethod
    def _tool_dict(t) -> dict:
        ann = getattr(t, "annotations", None)
        return {
            "name": t.name,
            "description": (getattr(t, "description", "") or "")[:600],
            "input_schema": getattr(t, "input_schema", None) or {"type": "object", "properties": {}},
            "read_only_hint": getattr(ann, "read_only_hint", None) if ann else None,
            "destructive_hint": getattr(ann, "destructive_hint", None) if ann else None,
        }

    async def call(self, tool: str, args: dict) -> Any:
        if not (self._task and not self._task.done()) or self.state != "ready":
            raise MCPError(f"{self.name} is not running")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        item = _Call(tool, args, self.spec.call_timeout_s, fut)
        await self._queue.put(item)
        try:
            return await fut
        except asyncio.CancelledError:
            self.state = "recycling"  # in-flight call abandoned: the session is recycled, never reused mid-call
            if item.task and not item.task.done():
                item.task.cancel()
            raise

    async def stop(self) -> None:
        """Graceful stop (queue sentinel -> SDK closes stdin / terminates the child), then cancel; every wait is bounded."""
        task, self._task = self._task, None
        if task and not task.done():
            self._queue.put_nowait(None)
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                log.warning("MCP %s: graceful stop timed out, cancelling session task", self.name)
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=5)
                if not done:
                    log.error("MCP %s: session task did not terminate (child process cleanup stuck)", self.name)
            if task.done() and not task.cancelled():
                task.exception()  # consume, so asyncio does not log "exception was never retrieved"
        if self.state not in ("failed", "crashed"):
            self.state = "stopped"


def _root_message(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return (str(e) or type(e).__name__)[:300]


class MCPManager:
    def __init__(
        self,
        settings: Settings,
        approvals=None,
        *,
        audit_file: Path | None = None,
        is_online_fn: Callable[[], bool] | None = None,
    ):
        self.settings = settings
        self.approvals = approvals
        self.audit_file = audit_file
        self._online = is_online_fn or (lambda: bool(settings.internet.enabled and audit.is_online_enabled()))
        self._rt: dict[str, ServerRuntime] = {}
        self._spoken_ok: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- configuration / lifecycle --------------------------------------------------------------------
    @property
    def specs(self) -> dict[str, MCPServerSpec]:
        return self.settings.mcp.servers

    def _runtime(self, name: str) -> ServerRuntime:
        spec = self.specs.get(name)
        if spec is None:
            raise MCPError(f"unknown MCP server {name!r}")
        rt = self._rt.get(name)
        if rt is None or rt.spec is not spec:
            if rt and rt._task and not rt._task.done():
                return rt
            rt = self._rt[name] = ServerRuntime(name, spec)
        return rt

    def _running(self) -> int:
        return sum(1 for r in self._rt.values() if r._task and not r._task.done())

    async def start(self, name: str) -> dict:
        spec = self.specs.get(name)
        if spec is None:
            raise MCPError(f"unknown MCP server {name!r}")
        if not spec.enabled:
            raise MCPError(f"{name} is disabled; enable it in Settings first")
        if spec.network and self.settings.offline_enforced and not self._online():
            raise MCPError(f"{name} needs network access but offline mode is enforced; enable Online Assisted Mode first")
        rt = self._runtime(name)
        if rt.state == "ready" and rt._task and not rt._task.done():
            return self.server_status(name)
        if self._running() >= self.settings.mcp.max_running:
            raise MCPError(f"max_running={self.settings.mcp.max_running} MCP servers reached")
        await rt.start()
        self._audit("mcp_server_start", name, True, "started")
        return self.server_status(name)

    async def stop(self, name: str) -> None:
        rt = self._rt.get(name)
        if rt:
            await rt.stop()
            self._audit("mcp_server_stop", name, True, "stopped")

    async def restart(self, name: str) -> dict:
        rt = self._runtime(name)
        rt.restarts = 0
        await rt.stop()
        return await self.start(name)

    async def start_enabled(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, spec in self.specs.items():
            if not spec.enabled:
                continue
            try:
                await self.start(name)
                out[name] = "ready"
            except Exception as e:  # noqa: BLE001
                out[name] = f"failed: {e}"
        return out

    async def shutdown(self) -> None:
        for rt in list(self._rt.values()):
            await rt.stop()

    async def _ensure_ready(self, name: str) -> ServerRuntime:
        """Bounded crash recovery: a crashed server is restarted on demand up to spec.max_restarts times."""
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            rt = self._runtime(name)
            if rt.state == "ready" and rt._task and not rt._task.done():
                return rt
            if rt._task is not None and rt.state in ("crashed", "stopped", "ready", "recycling"):  # session ended on its own
                if rt.restarts >= rt.spec.max_restarts:
                    rt.state = "failed"
                    raise MCPError(f"{name} crashed {rt.restarts} times; restart it manually ({rt.last_error})")
                rt.restarts += 1
                await rt.stop()
                await self.start(name)
                return rt
            raise MCPError(f"{name} is not running")

    # ---- introspection (never includes env values / secrets) --------------------------------------------
    def server_status(self, name: str) -> dict:
        spec = self.specs[name]
        rt = self._rt.get(name)
        if rt and rt._task and rt._task.done() and rt.state == "ready":
            rt.state = "crashed"
        return {
            "name": name,
            "enabled": spec.enabled,
            "state": rt.state if rt else "stopped",
            "tool_count": len(rt.tools) if rt else 0,
            "restarts": rt.restarts if rt else 0,
            "last_error": rt.last_error if rt else "",
            "network": spec.network,
            "command": spec.command,
            "args": list(spec.args),
            "env_keys": sorted(spec.env),
            "description": spec.description,
        }

    def status(self) -> list[dict]:
        return [self.server_status(n) for n in self.specs]

    def tools(self, *, include_down: bool = False) -> list[dict]:
        out: list[dict] = []
        for name, rt in self._rt.items():
            spec = self.specs.get(name)
            if not spec or not spec.enabled or (rt.state != "ready" and not (include_down and rt.state in ("crashed", "stopped", "recycling") and rt._task is not None)):
                continue
            for t in rt.tools:
                if spec.allowed_tools and t["name"] not in spec.allowed_tools:
                    continue
                out.append({**t, "server": name, "full_name": f"{name}.{t['name']}", "risk": classify_tool(spec, t)["level"]})
        return out

    def tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["full_name"],
                    "description": f"[MCP:{t['server']}] {t['description']}"[:700],
                    "parameters": t["input_schema"],
                },
            }
            for t in self.tools()
        ]

    def is_mcp_tool(self, full_name: str) -> bool:
        return "." in full_name and full_name.split(".", 1)[0] in self.specs

    def _find(self, full_name: str) -> tuple[str, dict]:
        server, _, tool = full_name.partition(".")
        for t in self.tools(include_down=True):  # a crashed server is restarted on demand by _ensure_ready
            if t["server"] == server and t["name"] == tool:
                return server, t
        raise MCPError(f"unknown or unavailable MCP tool {full_name!r}")

    # ---- invocation ---------------------------------------------------------------------------------
    async def call_tool(self, full_name: str, arguments: dict | None = None, *, source: str = "llm", task_id: str | None = None) -> dict:
        """Entry point for the brain. Returns a JSON-able dict; sensitive tools return approval_required instead of running."""
        arguments = dict(arguments or {})
        try:
            server, tool = self._find(full_name)
        except MCPError as e:
            self._audit("mcp_call", full_name, False, f"rejected: {e}")
            return {"ok": False, "error": str(e)}
        spec = self.specs[server]
        verdict = classify_tool(spec, tool, arguments)
        if verdict["level"] == "read":
            return await self._invoke(server, tool["name"], arguments, full_name, verdict)
        if self.approvals is None:
            self._audit("mcp_call", full_name, False, "denied: no approval service")
            return {"ok": False, "error": "approval service unavailable; refusing non-read-only tool"}
        critical = verdict["level"] == "critical"
        code = "".join(secrets.choice("0123456789") for _ in range(4)) if critical else ""
        preview = redact_secrets(json.dumps(arguments, ensure_ascii=False, default=str), mask_email=False)[0][:1500]
        req = await asyncio.to_thread(
            self.approvals.create_request,
            action_type="mcp_critical" if critical else "mcp_write",
            target_resource=full_name,
            preview_diff=f"MCP tool {full_name} ({verdict['reason']})\nArguments: {preview}"
            + (f"\nAlso requires spoken confirmation: say 'confirm {code}'" if critical else ""),
            args={"tool": full_name, "arguments": arguments, "critical": critical, "code": code, "source": source, "task_id": task_id},
        )
        self._audit("mcp_call", full_name, False, f"approval_required level={verdict['level']} args={sorted(arguments)}")
        return {
            "ok": False,
            "status": "approval_required",
            "approval_token": req["token"],
            "spoken_confirmation_required": critical,
            "message": "Owner approval is required before this tool runs." + (" Spoken confirmation is also required." if critical else ""),
        }

    async def _invoke(self, server: str, tool: str, arguments: dict, full_name: str, verdict: dict) -> dict:
        spec = self.specs[server]
        if spec.network and self.settings.offline_enforced and not self._online():
            self._audit("mcp_call", full_name, False, "denied: offline mode")
            return {"ok": False, "error": "offline mode: this MCP server needs network access"}
        t0 = time.perf_counter()
        try:
            rt = await self._ensure_ready(server)
            res = await rt.call(tool, arguments)
        except asyncio.CancelledError:
            self._audit("mcp_call", full_name, True, "cancelled")
            raise
        except MCPError as e:
            self._audit("mcp_call", full_name, True, f"error: {e} level={verdict['level']} args={sorted(arguments)}")
            return {"ok": False, "error": str(e)}
        text = self._result_text(res)
        is_err = bool(getattr(res, "is_error", False))
        self._audit("mcp_call", full_name, True, f"{'tool_error' if is_err else 'ok'} level={verdict['level']} args={sorted(arguments)} ms={(time.perf_counter() - t0) * 1000:.0f}")
        return {"ok": not is_err, "policy": UNTRUSTED_DATA_POLICY, "result": self.sanitize(text, full_name)}

    @staticmethod
    def _result_text(res: Any) -> str:
        parts = []
        for c in getattr(res, "content", None) or []:
            txt = getattr(c, "text", None)
            parts.append(txt if isinstance(txt, str) else f"[non-text content: {getattr(c, 'type', 'unknown')}]")
        return "\n".join(parts)

    def sanitize(self, text: str, full_name: str = "") -> str:
        clean, _ = redact_secrets(text or "", mask_email=True)
        return wrap_untrusted(clean, source_type="external_tool_output", source_id=full_name or "mcp", max_chars=self.settings.mcp.max_result_chars)

    # ---- approvals ----------------------------------------------------------------------------------
    def is_mcp_approval(self, row: dict) -> bool:
        return str(row.get("action_type", "")).startswith("mcp_")

    async def execute_approved(self, token: str) -> dict:
        """Run an MCP call whose approval was granted. Critical calls also need spoken confirmation first."""
        row = await asyncio.to_thread(self.approvals.get, token)
        if not self.is_mcp_approval(row):
            raise PermissionError("not an MCP approval")
        a = row["args"]
        if row["status"] != "approved":
            raise PermissionError(f"approval {token} is {row['status']}")
        if a.get("critical") and token not in self._spoken_ok:
            return {"ok": False, "status": "awaiting_spoken_confirmation", "approval_token": token}
        await asyncio.to_thread(self.approvals.consume_approved, token, expected_action_type=row["action_type"])
        self._spoken_ok.discard(token)
        server, tool = self._find(a["tool"])
        verdict = classify_tool(self.specs[server], tool, a.get("arguments") or {})
        return await self._invoke(server, tool["name"], a.get("arguments") or {}, a["tool"], verdict)

    def pending_spoken(self) -> list[dict]:
        if self.approvals is None:
            return []
        return [r for r in self.approvals.list_active() if self.is_mcp_approval(r) and r["args"].get("critical")]

    async def confirm_spoken(self, utterance: str, *, source: str) -> dict | None:
        """Called for every voice utterance. Only a *voice-sourced* 'confirm <code>' for a live critical request counts."""
        if source != "voice" or not self.approvals or "confirm" not in (utterance or "").lower():
            return None
        digits = _spoken_digits(utterance.lower().split("confirm", 1)[1])
        for row in self.pending_spoken():
            if row["args"].get("code") and digits.startswith(row["args"]["code"]):
                self._spoken_ok.add(row["token"])
                self._audit("mcp_spoken_confirm", row["args"]["tool"], True, "spoken confirmation accepted")
                fresh = await asyncio.to_thread(self.approvals.get, row["token"])
                if fresh["status"] == "approved":
                    return await self.execute_approved(row["token"])
                return {"ok": False, "status": "awaiting_owner_approval", "approval_token": row["token"]}
        return None

    # ---- audit --------------------------------------------------------------------------------------
    def _audit(self, action: str, target: str, allowed: bool, reason: str) -> None:
        audit.record_external(action, target, f"mcp:{target.split('.', 1)[0]}", allowed, reason=reason, audit_file=self.audit_file)
