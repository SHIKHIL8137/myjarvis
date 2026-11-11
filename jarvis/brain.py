"""Ollama client + model manager (native /api/chat: it is the only way to set num_ctx per request).
Reasoning text never reaches callers: the `thinking` field is dropped and any <think>..</think> is filtered.
Supports Ollama tool calling for approved web search/fetch tools with clean fallback if a model lacks tool support."""
import json
import logging
import time
from typing import AsyncIterator

import httpx

from . import audit
from .config import BrainSpec, Settings, get_provider_api_key, has_provider_api_key
from .netsec import NetworkSecurityError, safe_request

log = logging.getLogger("jarvis.brain")


class BrainError(Exception):
    """Ollama unreachable, model missing, or bad reply. Message is safe to show the user."""


class ToolsUnsupportedError(BrainError):
    """Raised when the selected Ollama model rejects the `tools` parameter."""


class ThinkFilter:
    """Streaming remover of <think>...</think> blocks (tags may be split across chunks)."""

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, s: str) -> str:
        self.buf += s
        out = ""
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]  # keep a possible partial closing tag
                    return out
                self.buf, self.inside = self.buf[i + 8:], False
            else:
                i = self.buf.find("<think>")
                if i >= 0:
                    out, self.buf, self.inside = out + self.buf[:i], self.buf[i + 7:], True
                    continue
                keep = max((k for k in range(1, 7) if "<think>".startswith(self.buf[-k:])), default=0)
                out += self.buf[:len(self.buf) - keep]
                self.buf = self.buf[len(self.buf) - keep:]
                return out

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


class Brain:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.current = settings.boot_brain
        self.adhoc: dict[str, BrainSpec] = {}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=3))
        self.last_stats: dict = {}

    # ---- specs ---------------------------------------------------------------------------------------
    @property
    def spec(self) -> BrainSpec:
        return self.settings.brains.get(self.current) or self.adhoc[self.current]

    @property
    def host(self) -> str:
        return self.spec.base_url.rsplit("/v1", 1)[0]

    @property
    def provider(self) -> str:
        p = getattr(self.spec, "provider", None)
        if p:
            return str(p).lower()
        if str(self.current).startswith("openai:"):
            return "openai"
        if str(self.current).startswith("anthropic:"):
            return "anthropic"
        return "ollama"

    def _cloud_allowed(self, prov: str) -> str:
        if self.settings.offline_enforced and not (self.settings.internet.enabled and audit.is_online_enabled()):
            raise BrainError(f"Cloud model {prov!r} is blocked in Local Mode. Enable Online Assisted Mode and providers.allow_cloud first.")
        if not self.settings.providers.allow_cloud:
            raise BrainError(f"Cloud provider {prov!r} is disabled (providers.allow_cloud=false). Local mode remains active.")
        key = get_provider_api_key(prov)
        if not key:
            raise BrainError(f"API key for cloud provider {prov!r} is not configured.")
        return key

    # ---- ollama state --------------------------------------------------------------------------------
    async def _get(self, path: str, timeout: float = 5):
        try:
            r = await self.client.get(self.host + path, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, OSError) as e:
            raise BrainError(f"Ollama is not reachable at {self.host} ({type(e).__name__}). Start the Ollama app and try again.") from e

    async def installed(self) -> list[str]:
        return [m["name"] for m in (await self._get("/api/tags"))["models"]]

    async def running(self) -> list[dict]:
        return (await self._get("/api/ps"))["models"]

    async def health(self) -> dict:
        try:
            v = (await self._get("/api/version", 2))["version"]
            return {"up": True, "version": v, "models": await self.installed(), "loaded": [m["name"] for m in await self.running()]}
        except BrainError as e:
            return {"up": False, "error": str(e)}

    async def catalogue(self) -> list[dict]:
        """Every selectable model: configured local brains, installed Ollama models, and optional cloud entries."""
        inst = await self.installed()
        out = [{
            "key": k,
            "model": s.model,
            "provider": getattr(s, "provider", "ollama"),
            "local": True,
            "thinking": s.thinking,
            "tools": True,
            "installed": s.model in inst,
            "current": k == self.current,
        } for k, s in self.settings.brains.items()]
        known = {s.model for s in self.settings.brains.values()}
        out += [{
            "key": m,
            "model": m,
            "provider": "ollama",
            "local": True,
            "thinking": False,
            "tools": True,
            "installed": True,
            "current": m == self.current,
        } for m in inst if m not in known]

        p = self.settings.providers
        if has_provider_api_key("openai"):
            k = f"openai:{p.openai_model}"
            out.append({"key": k, "model": p.openai_model, "provider": "openai", "local": False, "thinking": False, "tools": True,
                        "installed": bool(p.allow_cloud and self.settings.internet.enabled), "current": self.current == k})
        if has_provider_api_key("anthropic"):
            k = f"anthropic:{p.anthropic_model}"
            out.append({"key": k, "model": p.anthropic_model, "provider": "anthropic", "local": False, "thinking": False, "tools": True,
                        "installed": bool(p.allow_cloud and self.settings.internet.enabled), "current": self.current == k})
        return out

    async def resolve_choice(self, want: str) -> str | None:
        """Resolve natural alias ('fast', 'reasoning', 'best installed reasoning', 'llama', 'openai', etc.)."""
        q = (want or "").strip().lower()
        aliases = {
            "fast": "local", "quick": "local", "default": self.settings.boot_brain, "normal": "local",
            "instruct": "local", "local": "local", "thinking": "local_thinking", "reasoning": "local_thinking",
            "deep": "local_thinking", "smart": "local_thinking", "best reasoning": "local_thinking",
            "best installed reasoning model": "local_thinking",
        }
        if q in aliases:
            return aliases[q]
        if q.startswith("openai") or q == "gpt":
            return f"openai:{self.settings.providers.openai_model}"
        if q.startswith("anthropic") or "claude" in q:
            return f"anthropic:{self.settings.providers.anthropic_model}"
        inst = await self.installed()
        for m in inst:
            if m.lower() == q:
                return m
        for m in inst:
            if q and (m.lower().startswith(q) or q in m.lower()):
                return m
        return None

    async def switch(self, key_or_model: str) -> dict:
        """Select a configured brain, installed Ollama model, or gated cloud adapter. Never pulls models."""
        if key_or_model.startswith(("openai:", "anthropic:")):
            prov, model_id = key_or_model.split(":", 1)
            self._cloud_allowed(prov)
            base_url = self.settings.providers.openai_base_url if prov == "openai" else self.settings.providers.anthropic_base_url
            spec = BrainSpec(base_url=base_url, model=model_id, thinking=False)
            setattr(spec, "provider", prov)
            self.adhoc[key_or_model] = spec
            if key_or_model != self.current:
                if self.provider == "ollama":
                    await self.unload(self.spec.model)
                self.current = key_or_model
            return {"key": key_or_model, "model": model_id, "provider": prov, "thinking": False, "local": False}

        inst = await self.installed()
        if key_or_model in self.settings.brains:
            key, spec = key_or_model, self.settings.brains[key_or_model]
        elif key_or_model in inst:
            base = self.settings.brains[self.settings.boot_brain]
            key, spec = key_or_model, base.model_copy(update={"model": key_or_model, "thinking": False})
            self.adhoc[key] = spec
        else:
            raise BrainError(f"Model {key_or_model!r} is not installed. Installed: {', '.join(inst)}. Run `ollama pull <tag>` if you want it.")
        if spec.model not in inst:
            raise BrainError(f"Model {spec.model!r} is not installed. Run `ollama pull {spec.model}` first.")
        if key != self.current:
            if self.provider == "ollama":
                await self.unload(self.spec.model)
            self.current = key
        return {"key": key, "model": spec.model, "provider": "ollama", "thinking": spec.thinking, "local": True}

    async def unload(self, model: str) -> None:
        try:
            await self.client.post(self.host + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=15)
        except httpx.HTTPError:
            log.warning("could not unload %s", model)

    async def warm(self) -> float:
        t = time.time()
        await self.client.post(self.host + "/api/generate", timeout=300, json={
            "model": self.spec.model, "prompt": "", "keep_alive": self.spec.keep_alive, "options": {"num_ctx": self.spec.num_ctx}})
        return time.time() - t

    # ---- chat ----------------------------------------------------------------------------------------
    def _body(self, messages: list[dict], stream: bool, tools: list[dict] | None = None) -> dict:
        s = self.spec
        body = {
            "model": s.model,
            "messages": messages,
            "stream": stream,
            "think": s.thinking,
            "keep_alive": s.keep_alive,
            "options": {"num_ctx": s.num_ctx, "num_predict": s.max_tokens, "temperature": 0.6},
        }
        if tools:
            body["tools"] = tools
        return body

    async def _cloud_complete(self, prov: str, messages: list[dict]) -> AsyncIterator[dict]:
        key = self._cloud_allowed(prov)
        t0 = time.time()
        try:
            if prov == "openai":
                url = self.settings.providers.openai_base_url.rstrip("/") + "/chat/completions"
                res = await safe_request(
                    "POST",
                    url,
                    json_body={"model": self.spec.model, "messages": messages, "max_tokens": self.spec.max_tokens},
                    headers={"Authorization": f"Bearer {key}"},
                    timeout_s=30.0,
                )
                data = json.loads(res.body.decode("utf-8"))
                txt = data["choices"][0]["message"]["content"]
            elif prov == "anthropic":
                url = self.settings.providers.anthropic_base_url.rstrip("/") + "/messages"
                sys_msg = "\n".join(m["content"] for m in messages if m["role"] == "system")
                chat_msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
                res = await safe_request(
                    "POST",
                    url,
                    json_body={"model": self.spec.model, "system": sys_msg, "messages": chat_msgs, "max_tokens": self.spec.max_tokens},
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout_s=30.0,
                )
                data = json.loads(res.body.decode("utf-8"))
                txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            else:
                raise BrainError(f"Unsupported cloud provider: {prov}")
        except (NetworkSecurityError, ConnectionError) as e:
            raise BrainError(f"Cloud provider {prov} blocked by security policy: {e}") from e
        except Exception as e:
            raise BrainError(f"Cloud request failed: {e}") from e

        self.last_stats = {"ttft": time.time() - t0, "tokens": len(txt.split()), "tps": 0.0, "total": time.time() - t0, "model": self.spec.model, "truncated": False}
        yield {"type": "token", "text": txt}

    async def stream_events(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Stream `/api/chat` (or gated cloud provider) yielding {"type": "token", "text"} and {"type": "tool_calls", "calls"}."""
        if self.provider in ("openai", "anthropic"):
            async for ev in self._cloud_complete(self.provider, messages):
                yield ev
            return
        flt, t0, first = ThinkFilter(), time.time(), None
        collected_calls: list[dict] = []
        try:
            async with self.client.stream("POST", self.host + "/api/chat", json=self._body(messages, True, tools=tools)) as r:
                if r.status_code == 404:
                    raise BrainError(f"Model {self.spec.model!r} not found in Ollama. Run `ollama pull {self.spec.model}`.")
                if r.status_code == 400 and tools:
                    body_txt = (await r.aread()).decode("utf-8", errors="replace")
                    if "tool" in body_txt.lower():
                        raise ToolsUnsupportedError(body_txt)
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    c = json.loads(line)
                    if c.get("error"):
                        if tools and "tool" in str(c["error"]).lower():
                            raise ToolsUnsupportedError(c["error"])
                        raise BrainError(c["error"])
                    msg = c.get("message") or {}
                    if msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            fn = tc.get("function") or {}
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"raw": args}
                            collected_calls.append({
                                "id": tc.get("id") or f"call_{len(collected_calls)}",
                                "name": fn.get("name", ""),
                                "arguments": args if isinstance(args, dict) else {},
                            })
                    piece = flt.feed(msg.get("content", ""))  # `thinking` field intentionally ignored
                    if piece:
                        first = first or time.time() - t0
                        yield {"type": "token", "text": piece}
                    if c.get("done"):
                        n = c.get("eval_count", 0)
                        ed = c.get("eval_duration") or 1
                        self.last_stats = {
                            "ttft": first,
                            "tokens": n,
                            "tps": n / (ed / 1e9),
                            "total": time.time() - t0,
                            "prompt_tokens": c.get("prompt_eval_count", 0),
                            "prompt_s": (c.get("prompt_eval_duration") or 0) / 1e9,
                            "load_s": (c.get("load_duration") or 0) / 1e9,
                            "model": self.spec.model,
                            "truncated": c.get("done_reason") == "length",
                        }
        except httpx.HTTPError as e:
            raise BrainError(f"Ollama request failed ({type(e).__name__}). Is Ollama running?") from e
        tail = flt.flush()
        if tail:
            yield {"type": "token", "text": tail}
        if collected_calls:
            yield {"type": "tool_calls", "calls": collected_calls}

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        async for ev in self.stream_events(messages):
            if ev["type"] == "token":
                yield ev["text"]

    async def complete(self, messages: list[dict]) -> str:
        return "".join([p async for p in self.stream(messages)]).strip()
