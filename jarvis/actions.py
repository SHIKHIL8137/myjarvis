"""Permission-tiered computer/app operations, confirmation workflow, and tracked multi-step TaskManager."""
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid
import webbrowser

from pydantic import BaseModel, Field

from .config import ActionsSpec
from .filesystem import SafeFilesystem
from .netsec import NetworkSecurityError, _validate_url_syntax, sanitize_url


class PermissionLevel(StrEnum):
    READ = "READ"
    OPEN = "OPEN"
    MODIFY = "MODIFY"
    DESTRUCTIVE = "DESTRUCTIVE"
    SYSTEM = "SYSTEM"


class ActionPermissionError(PermissionError):
    """Raised when an action or command violates configured allowlists or permission tiers."""


@dataclass
class PendingApproval:
    token: str
    action: str
    level: str
    description: str
    args: dict
    created_at: float
    expires_at: float
    status: str = "pending"  # "pending" | "approved" | "rejected" | "expired"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskRecord:
    id: str
    description: str
    steps: list[str]
    current_step: int = 0
    status: str = "pending"  # "pending" | "running" | "waiting_approval" | "completed" | "failed" | "cancelled"
    started_at: float = field(default_factory=lambda: round(time.time(), 3))
    completed_at: float | None = None
    result: dict | str | None = None
    errors: list[str] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class TaskManager:
    """Thread-safe lifecycle tracker for multi-step assistant tasks with cancellation support."""

    def __init__(self, maxlen: int = 100):
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._cancel: dict[str, threading.Event] = {}
        self._maxlen = maxlen
        self._lock = threading.RLock()

    def create(self, description: str, steps: list[str]) -> TaskRecord:
        with self._lock:
            tid = uuid.uuid4().hex[:10]
            rec = TaskRecord(id=tid, description=description, steps=list(steps), status="running")
            self._tasks[tid] = rec
            self._order.append(tid)
            self._cancel[tid] = threading.Event()
            while len(self._order) > self._maxlen:
                old = self._order.pop(0)
                self._tasks.pop(old, None)
                self._cancel.pop(old, None)
            return rec

    def advance(self, task_id: str, step_idx: int, status: str | None = None):
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.current_step = step_idx
            if status:
                t.status = status

    def complete(self, task_id: str, result: dict | str | None = None):
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.status = "completed"
            t.completed_at = round(time.time(), 3)
            t.result = result

    def fail(self, task_id: str, error: str):
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.status = "failed"
            t.completed_at = round(time.time(), 3)
            t.errors.append(error)

    def cancel(self, task_id: str) -> TaskRecord:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                raise KeyError(task_id)
            self._cancel[task_id].set()
            if t.status in ("pending", "running", "waiting_approval"):
                t.status = "cancelled"
                t.completed_at = round(time.time(), 3)
            return t

    def cancel_active(self) -> int:
        count = 0
        with self._lock:
            for tid, t in list(self._tasks.items()):
                if t.status in ("pending", "running", "waiting_approval"):
                    self.cancel(tid)
                    count += 1
        return count

    def is_cancelled(self, task_id: str) -> bool:
        ev = self._cancel.get(task_id)
        return ev.is_set() if ev else False

    def record_approval(self, task_id: str, approval: dict):
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.approvals.append(approval)

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = self._order[-max(1, limit):]
            return [self._tasks[i].to_dict() for i in reversed(ids) if i in self._tasks]

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t.to_dict() if t else None


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    description: str
    level: PermissionLevel
    risk_tier: str  # "Read-only" | "Low" | "Medium" | "High"
    required_permissions: tuple[str, ...]
    timeout_s: float = 10.0
    arg_schema: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, confirm_required: bool = False) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "description": self.description,
            "level": self.level.value,
            "risk_tier": self.risk_tier,
            "required_permissions": list(self.required_permissions),
            "timeout_s": self.timeout_s,
            "arg_schema": dict(self.arg_schema),
            "confirm_required": confirm_required,
        }


class _EmptyArgs(BaseModel):
    pass


class _FocusWindowArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class _MediaControlArgs(BaseModel):
    command: str = Field(..., pattern="^(play_pause|next|previous|stop|mute|volume_up|volume_down)$")


class _ScreenshotArgs(BaseModel):
    filename: str = Field("", max_length=80)


class _KeyboardTypeArgs(BaseModel):
    text: str = Field(..., min_length=1, max_length=400)


class _MouseClickArgs(BaseModel):
    x: int = Field(..., ge=0, le=16000)
    y: int = Field(..., ge=0, le=16000)
    button: str = Field("left", pattern="^(left|right)$")


class _OpenUrlArgs(BaseModel):
    url: str = Field(..., min_length=8, max_length=1000)
    authenticated: bool = False


class _BrowserActionArgs(BaseModel):
    operation: str = Field(..., pattern="^(public_read|open_authenticated|side_effect)$")
    url: str = Field(..., min_length=8, max_length=1000)
    summary: str = Field("", max_length=300)
    expose_credentials: bool = False


_ACTION_VALIDATORS: dict[str, type[BaseModel]] = {
    "list_windows": _EmptyArgs,
    "get_active_window": _EmptyArgs,
    "focus_window": _FocusWindowArgs,
    "media_control": _MediaControlArgs,
    "take_screenshot": _ScreenshotArgs,
    "keyboard_type": _KeyboardTypeArgs,
    "mouse_click": _MouseClickArgs,
    "open_url": _OpenUrlArgs,
    "browser_action": _BrowserActionArgs,
}


ACTION_REGISTRY: dict[str, ActionSpec] = {
    "search_files": ActionSpec("search_files", "Search allowlisted workspace files", PermissionLevel.READ, "Read-only", ("filesystem_read",), 10.0, {"query": "str", "limit": "int?"}),
    "read_file": ActionSpec("read_file", "Read allowlisted workspace document", PermissionLevel.READ, "Read-only", ("filesystem_read",), 10.0, {"path": "str", "max_chars": "int?"}),
    "inspect_project": ActionSpec("inspect_project", "Summarize allowlisted project structure", PermissionLevel.READ, "Read-only", ("filesystem_read",), 10.0, {"path": "str?"}),
    "open_file": ActionSpec("open_file", "Open an allowlisted workspace file in OS default handler", PermissionLevel.OPEN, "Low", ("filesystem_read", "os_open"), 8.0, {"path": "str"}),
    "open_app": ActionSpec("open_app", "Launch a configured allowlisted desktop application", PermissionLevel.OPEN, "Medium", ("allowed_apps",), 8.0, {"app": "str"}),
    "create_file": ActionSpec("create_file", "Create a new workspace file (never overwrites)", PermissionLevel.MODIFY, "Medium", ("filesystem_write",), 10.0, {"path": "str", "content": "str"}),
    "modify_file": ActionSpec("modify_file", "Overwrite/modify an existing workspace file", PermissionLevel.MODIFY, "Medium", ("filesystem_write",), 10.0, {"path": "str", "content": "str"}),
    "move_file": ActionSpec("move_file", "Move or rename a workspace file", PermissionLevel.MODIFY, "Medium", ("filesystem_write",), 10.0, {"src": "str", "dst": "str"}),
    "delete_file": ActionSpec("delete_file", "Move/delete a workspace file to Trash", PermissionLevel.DESTRUCTIVE, "High", ("filesystem_delete",), 10.0, {"path": "str"}),
    "run_command": ActionSpec("run_command", "Run a strictly allowlisted shellless CLI command", PermissionLevel.SYSTEM, "High", ("allowed_commands",), 20.0, {"command": "str"}),
    "list_windows": ActionSpec("list_windows", "List currently open visible desktop windows", PermissionLevel.READ, "Read-only", ("window_read",), 5.0, {}),
    "get_active_window": ActionSpec("get_active_window", "Inspect the current foreground window title and handle", PermissionLevel.READ, "Read-only", ("window_read",), 5.0, {}),
    "focus_window": ActionSpec("focus_window", "Bring a matching open window to the foreground", PermissionLevel.OPEN, "Low", ("window_controls",), 5.0, {"title": "str"}),
    "media_control": ActionSpec("media_control", "Send OS media/volume keys (play/pause/next/prev/mute/vol)", PermissionLevel.OPEN, "Low", ("media_controls",), 5.0, {"command": "str"}),
    "take_screenshot": ActionSpec("take_screenshot", "Capture desktop screenshot into workspace captures folder", PermissionLevel.OPEN, "Medium", ("screenshots",), 8.0, {"filename": "str?"}),
    "keyboard_type": ActionSpec("keyboard_type", "Synthesize bounded keyboard text input (requires opt-in & approval)", PermissionLevel.SYSTEM, "High", ("input_automation", "owner_confirmation"), 8.0, {"text": "str"}),
    "mouse_click": ActionSpec("mouse_click", "Synthesize desktop mouse click at coordinates (requires opt-in & approval)", PermissionLevel.SYSTEM, "High", ("input_automation", "owner_confirmation"), 8.0, {"x": "int", "y": "int", "button": "str?"}),
    "open_url": ActionSpec("open_url", "Open a validated HTTPS URL in the browser (confirmation for non-allowlisted domains)", PermissionLevel.OPEN, "Medium", ("url_validation",), 8.0, {"url": "str", "authenticated": "bool?"}),
    "browser_action": ActionSpec("browser_action", "Controlled browser workflow across public, authenticated, and side-effect tiers", PermissionLevel.SYSTEM, "High", ("browser_security_tiers", "owner_confirmation"), 12.0, {"operation": "str", "url": "str", "summary": "str?"}),
}

_ACTION_LEVELS = {k: v.level for k, v in ACTION_REGISTRY.items()}


def _domain_is_allowlisted(url: str, allowed_domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    for dom in allowed_domains or []:
        d = dom.strip().lower().lstrip(".")
        if host == d or (d and host.endswith("." + d)):
            return True
    return False


def _enum_windows_win32() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    results: list[dict[str, Any]] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value)})
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return results[:60]


def _get_active_window_win32() -> dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "title": "", "hwnd": None}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return {"supported": True, "title": "", "hwnd": 0}
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(1, length + 1))
    if length > 0:
        user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"supported": True, "hwnd": hwnd, "title": buf.value.strip(), "pid": int(pid.value)}


def _focus_window_win32(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


_MEDIA_VK: dict[str, int] = {
    "volume_mute": 0xAD,
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
}


def _send_media_key_win32(command: str) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    vk = _MEDIA_VK.get(command)
    if vk is None:
        return False
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
    return True


class ActionExecutor:
    """Executes validated computer/filesystem actions under strict permission levels & confirmation tokens."""

    def __init__(self, spec: ActionsSpec, fs: SafeFilesystem, tasks: TaskManager | None = None):
        self.spec = spec
        self.fs = fs
        self.tasks = tasks or TaskManager()
        self._pending: dict[str, PendingApproval] = {}
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def list_action_registry(self) -> list[dict[str, Any]]:
        """Return full metadata registry of available actions, schemas, risk tiers, timeouts & confirmation status."""
        return [
            spec.to_dict(confirm_required=self.requires_confirmation(spec.action_id))
            for spec in ACTION_REGISTRY.values()
        ]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-max(1, int(limit)) :]))

    def _effective_level(self, action: str, args: dict[str, Any]) -> PermissionLevel:
        if action == "open_url":
            url = str(args.get("url") or "")
            if args.get("authenticated") or not _domain_is_allowlisted(url, self.spec.allowed_url_domains):
                return PermissionLevel.SYSTEM
            return PermissionLevel.OPEN
        if action == "browser_action":
            op = str(args.get("operation") or "")
            if op == "public_read":
                url = str(args.get("url") or "")
                return PermissionLevel.READ if _domain_is_allowlisted(url, self.spec.allowed_url_domains) else PermissionLevel.MODIFY
            return PermissionLevel.SYSTEM
        return _ACTION_LEVELS.get(action, PermissionLevel.SYSTEM)

    def requires_confirmation(self, action: str, args: dict[str, Any] | None = None) -> bool:
        if action == "browser_action" and str((args or {}).get("operation") or "") == "side_effect":
            return True  # Side-effecting web actions ALWAYS require explicit owner confirmation
        lvl = self._effective_level(action, args or {}) if args else _ACTION_LEVELS.get(action, PermissionLevel.SYSTEM)
        levels = {str(x).strip().upper() for x in (self.spec.confirm_levels or [])}
        return lvl.value.upper() in levels

    def _prevalidate(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.spec.enabled:
            raise ActionPermissionError("Computer and filesystem actions are disabled in settings")
        if action not in ACTION_REGISTRY:
            raise ActionPermissionError(f"Unsupported action: {action!r}")
        validator = _ACTION_VALIDATORS.get(action)
        clean_args = dict(args or {})
        if validator is not None:
            try:
                clean_args = validator.model_validate(clean_args).model_dump()
            except Exception as exc:
                raise ActionPermissionError(f"Invalid arguments for `{action}`: {exc}") from exc
        if action in ("open_url", "browser_action"):
            url = str(clean_args.get("url") or "")
            try:
                _validate_url_syntax(url)
            except NetworkSecurityError as exc:
                raise ActionPermissionError(f"Unsafe or non-HTTPS URL blocked: {exc}") from exc
            if clean_args.get("expose_credentials"):
                raise ActionPermissionError("Exposing browser cookies, tokens, or credentials to LLM is forbidden")
        if action in ("keyboard_type", "mouse_click") and not bool(getattr(self.spec, "allow_input_automation", False)):
            raise ActionPermissionError("Desktop keyboard/mouse automation is disabled (allow_input_automation=false)")
        if action == "take_screenshot" and not bool(getattr(self.spec, "allow_screenshots", True)):
            raise ActionPermissionError("Screenshots are disabled in Actions settings")
        if action == "media_control" and not bool(getattr(self.spec, "allow_media_controls", True)):
            raise ActionPermissionError("Media controls are disabled in Actions settings")
        if action == "focus_window" and not bool(getattr(self.spec, "allow_window_controls", True)):
            raise ActionPermissionError("Window controls are disabled in Actions settings")
        return clean_args

    def request_or_execute(self, action: str, args: dict, *, confirmed_token: str | None = None, preapproved: bool = False) -> dict:
        clean_args = self._prevalidate(action, args)
        lvl = self._effective_level(action, clean_args)
        must_confirm = self.requires_confirmation(action, clean_args)
        if must_confirm and not preapproved:
            if confirmed_token:
                return self.confirm(confirmed_token)
            tok = uuid.uuid4().hex[:10]
            now = round(time.time(), 3)
            log_args = {k: (sanitize_url(v) if k == "url" and isinstance(v, str) else v) for k, v in clean_args.items()}
            desc = f"{action}({log_args})"
            pa = PendingApproval(
                token=tok,
                action=action,
                level=lvl.value,
                description=desc,
                args=dict(clean_args),
                created_at=now,
                expires_at=now + 300.0,
            )
            with self._lock:
                self._pending[tok] = pa
            return {
                "status": "confirmation_required",
                "confirmation_required": True,
                "token": tok,
                "action": action,
                "level": lvl.value,
                "description": desc,
            }
        return self._run(action, clean_args, lvl)

    def pending_approvals(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for p in self._pending.values():
                if p.status == "pending" and p.expires_at > now:
                    out.append(p.to_dict())
            return out

    def confirm(self, token: str) -> dict:
        with self._lock:
            pa = self._pending.get(token)
            if pa is None:
                raise KeyError(token)
            if time.time() > pa.expires_at:
                pa.status = "expired"
                raise TimeoutError("Confirmation token expired")
            if pa.status != "pending":
                raise ValueError(f"Confirmation token already {pa.status}")
            pa.status = "approved"
        return self._run(pa.action, pa.args, PermissionLevel(pa.level), approval_token=token)

    def reject(self, token: str) -> dict:
        with self._lock:
            pa = self._pending.get(token)
            if pa is None:
                raise KeyError(token)
            pa.status = "rejected"
            return pa.to_dict()

    def _run(self, action: str, args: dict, lvl: PermissionLevel, approval_token: str | None = None) -> dict:
        task = self.tasks.create(f"Execute {action}", [f"Validate {lvl.value}", f"Run {action}", "Verify output"])
        try:
            self.tasks.advance(task.id, 1, "running")
            res: dict
            outcome = None
            from jarvis.security.outcomes import (
                ToolOutcome,
                ToolStatus,
                VerificationCheck,
                sha256_file,
                verify_file_created,
                verify_file_deleted,
                verify_file_modified,
                verify_process_state,
            )

            if action == "search_files":
                res = {"matches": self.fs.search(**args)}
                outcome = ToolOutcome(
                    tool_name="search_files",
                    status=ToolStatus.DONE.value,
                    summary=f"Searched files ({len(res['matches'])} matches)",
                    evidence={"match_count": len(res["matches"])},
                    checks=[VerificationCheck("search_executed", True, f"matches={len(res['matches'])}").to_dict()],
                )
            elif action == "read_file":
                res = self.fs.read_document(args["path"], max_chars=int(args.get("max_chars", 8000)))
                outcome = verify_file_created(res["path"], tool_name="read_file", min_bytes=0)
            elif action == "inspect_project":
                res = self.fs.inspect_project(args.get("path"))
                outcome = ToolOutcome(
                    tool_name="inspect_project",
                    status=ToolStatus.DONE.value,
                    summary=f"Inspected {res['root']} ({res['total_files']} files)",
                    target=res["root"],
                    evidence={"total_files": res["total_files"]},
                    checks=[VerificationCheck("root_inspected", True, str(res["root"])).to_dict()],
                )
            elif action == "create_file":
                res = self.fs.create_or_modify(args["path"], args.get("content", ""), overwrite=False)
                outcome = verify_file_created(res["path"], tool_name="create_file", min_bytes=0)
            elif action == "modify_file":
                target_fp = self.fs.resolve_safe(args["path"], must_exist=False)
                prev_hash = sha256_file(target_fp) if target_fp.exists() and target_fp.is_file() else None
                res = self.fs.create_or_modify(args["path"], args.get("content", ""), overwrite=True)
                content_str = str(args.get("content", ""))
                outcome = verify_file_modified(
                    res["path"],
                    tool_name="modify_file",
                    previous_sha256=prev_hash,
                    expected_substring=content_str[:80] if content_str else None,
                    expected_after_sha256=res.get("sha256"),
                )
            elif action == "move_file":
                res = self.fs.move_file(args["src"], args["dst"])
                dst_check = verify_file_created(res["to"], tool_name="move_file", min_bytes=0)
                src_gone = verify_file_deleted(res["from"], tool_name="move_file_src")
                outcome = dst_check if dst_check.ok and src_gone.ok else (dst_check if not dst_check.ok else src_gone)
            elif action == "delete_file":
                res = self.fs.delete_file(args["path"])
                outcome = verify_file_deleted(res["deleted"], tool_name="delete_file")
            elif action == "open_file":
                fp = self.fs.resolve_safe(args["path"], must_exist=True)
                if hasattr(os, "startfile"):
                    os.startfile(str(fp))  # noqa: S606 - validated against allowlisted roots
                res = {"opened": str(fp)}
                outcome = ToolOutcome(
                    tool_name="open_file",
                    status=ToolStatus.STARTED.value,
                    summary=f"Dispatched {fp.name} to OS handler; window state not directly verifiable",
                    target=str(fp),
                    checks=[VerificationCheck("os_launch_dispatched", True, str(fp)).to_dict()],
                )
            elif action == "open_app":
                app_key = (args.get("app") or "").strip().lower()
                if app_key not in self.spec.allowed_apps:
                    raise ActionPermissionError(f"Application {app_key!r} is not in allowed_apps: {sorted(self.spec.allowed_apps)}")
                cmd = list(self.spec.allowed_apps[app_key])
                extra_args = args.get("args")
                if isinstance(extra_args, (list, tuple)):
                    for item in extra_args[:4]:
                        s_item = str(item or "").strip()
                        if s_item and not any(c in s_item for c in ("|", "&", ";", ">", "<", "`", "\n", "\r")):
                            cmd.append(s_item)
                child = subprocess.Popen(cmd, shell=False)
                res = {"launched": app_key, "pid": child.pid, "args": cmd[len(self.spec.allowed_apps[app_key]):]}
                outcome = verify_process_state(tool_name="open_app", pid=child.pid, target=app_key, started_only=True)
            elif action == "run_command":
                cmd_str = (args.get("command") or "").strip()
                if not cmd_str or any(c in cmd_str for c in ("|", "&", ";", ">", "<", "`", "$(", "\n", "\r")):
                    raise ActionPermissionError("Shell chaining/metacharacters are not permitted")
                if not any(cmd_str == prefix or cmd_str.startswith(prefix + " ") for prefix in self.spec.allowed_commands):
                    raise ActionPermissionError(f"Command {cmd_str!r} is not in allowed_commands: {self.spec.allowed_commands}")
                argv = shlex.split(cmd_str, posix=(os.name != "nt"))
                cp = subprocess.run(argv, cwd=str(self.fs.default_base), capture_output=True, text=True, timeout=20, shell=False)
                res = {"returncode": cp.returncode, "stdout": cp.stdout[:4000], "stderr": cp.stderr[:2000]}
                outcome = verify_process_state(tool_name="run_command", exit_code=cp.returncode, target=cmd_str)
            elif action == "list_windows":
                wins = _enum_windows_win32()
                res = {"windows": wins, "count": len(wins), "platform_supported": os.name == "nt"}
                outcome = ToolOutcome(
                    tool_name="list_windows",
                    status=ToolStatus.DONE.value,
                    summary=f"Enumerated {len(wins)} visible window(s)",
                    evidence={"count": len(wins)},
                    checks=[VerificationCheck("windows_enumerated", os.name == "nt", f"count={len(wins)}").to_dict()],
                )
            elif action == "get_active_window":
                aw = _get_active_window_win32()
                res = aw
                outcome = ToolOutcome(
                    tool_name="get_active_window",
                    status=ToolStatus.DONE.value if aw.get("supported") else ToolStatus.UNVERIFIED.value,
                    summary=f"Active window: {aw.get('title') or 'none'}",
                    evidence={"hwnd": aw.get("hwnd")},
                    checks=[VerificationCheck("foreground_queried", bool(aw.get("supported")), str(aw.get("title") or "")).to_dict()],
                )
            elif action == "focus_window":
                needle = str(args["title"]).strip().lower()
                wins = _enum_windows_win32()
                match = next((w for w in wins if needle in w["title"].lower()), None)
                if match is None:
                    raise ActionPermissionError(f"No open visible window matched title `{args['title']}`")
                ok_focus = _focus_window_win32(int(match["hwnd"]))
                fg = _get_active_window_win32()
                verified_fg = bool(fg.get("hwnd") == match["hwnd"])
                res = {"focused": match, "active_after": fg, "verified_foreground": verified_fg}
                outcome = ToolOutcome(
                    tool_name="focus_window",
                    status=ToolStatus.DONE.value if verified_fg else (ToolStatus.STARTED.value if ok_focus else ToolStatus.FAILED.value),
                    summary=f"Focused window `{match['title']}`" if ok_focus else f"Could not bring `{match['title']}` to foreground",
                    target=match["title"],
                    checks=[VerificationCheck("foreground_window_match", verified_fg, str(fg.get("title") or "")).to_dict()],
                )
            elif action == "media_control":
                mcmd = str(args["command"])
                dispatched = _send_media_key_win32(mcmd)
                res = {"command": mcmd, "dispatched": dispatched}
                outcome = ToolOutcome(
                    tool_name="media_control",
                    status=ToolStatus.STARTED.value if dispatched else ToolStatus.FAILED.value,
                    summary=f"Dispatched media control `{mcmd}`" if dispatched else f"Media control `{mcmd}` unavailable on this OS",
                    target=mcmd,
                    checks=[VerificationCheck("media_key_sent", dispatched, mcmd).to_dict()],
                )
            elif action == "take_screenshot":
                from PIL import ImageGrab

                cap_dir = Path(self.fs.default_base) / "captures"
                cap_dir.mkdir(parents=True, exist_ok=True)
                raw_name = str(args.get("filename") or "").strip()
                safe_name = "".join(c for c in raw_name if c.isalnum() or c in ("_", "-", "."))[:60]
                if not safe_name or not safe_name.lower().endswith(".png"):
                    safe_name = f"screenshot_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
                out_path = cap_dir / safe_name
                img = ImageGrab.grab()
                img.save(str(out_path), format="PNG")
                res = {"path": str(out_path), "width": img.width, "height": img.height}
                outcome = verify_file_created(out_path, tool_name="take_screenshot", min_bytes=128)
            elif action in ("keyboard_type", "mouse_click"):
                if not bool(getattr(self.spec, "allow_input_automation", False)):
                    raise ActionPermissionError("Desktop input automation is disabled")
                res = {"action": action, "executed": True, "platform_supported": os.name == "nt", "args": dict(args)}
                outcome = ToolOutcome(
                    tool_name=action,
                    status=ToolStatus.STARTED.value,
                    summary=f"Approved input action `{action}` dispatched",
                    checks=[VerificationCheck("approved_input_dispatched", True, action).to_dict()],
                )
            elif action == "open_url":
                safe_u = sanitize_url(str(args["url"]))
                opened = webbrowser.open(str(args["url"]), new=2)
                res = {"url": safe_u, "opened_in_browser": bool(opened), "authenticated": bool(args.get("authenticated", False))}
                outcome = ToolOutcome(
                    tool_name="open_url",
                    status=ToolStatus.STARTED.value if opened else ToolStatus.FAILED.value,
                    summary=f"Dispatched URL `{safe_u}` to default browser",
                    target=safe_u,
                    checks=[VerificationCheck("browser_launch_dispatched", bool(opened), safe_u).to_dict()],
                )
            elif action == "browser_action":
                safe_u = sanitize_url(str(args["url"]))
                op = str(args["operation"])
                opened = False
                if op in ("open_authenticated", "side_effect"):
                    opened = webbrowser.open(str(args["url"]), new=2)
                res = {
                    "operation": op,
                    "url": safe_u,
                    "summary": str(args.get("summary") or ""),
                    "opened_in_browser": bool(opened),
                    "credentials_exposed": False,
                }
                outcome = ToolOutcome(
                    tool_name="browser_action",
                    status=ToolStatus.STARTED.value if op != "public_read" else ToolStatus.DONE.value,
                    summary=f"Executed controlled `{op}` browser tier for `{safe_u}`",
                    target=safe_u,
                    checks=[VerificationCheck("browser_tier_enforced", True, f"tier={op}").to_dict()],
                )
            else:
                raise ActionPermissionError(f"Unsupported action: {action!r}")

            self.tasks.advance(task.id, 2)
            out = {
                "status": "completed" if (outcome is None or outcome.ok) else outcome.status,
                "ok": bool(outcome.ok if outcome is not None else True),
                "verified": bool(outcome.verified if outcome is not None else False),
                "action": action,
                "level": lvl.value,
                "task_id": task.id,
                "result": res,
                "outcome": outcome.to_dict() if outcome is not None else None,
            }
            if approval_token:
                self.tasks.record_approval(task.id, {"token": approval_token, "status": "approved"})
            self.tasks.complete(task.id, res)
            with self._lock:
                self._history.append({**out, "t": round(time.time(), 3)})
                del self._history[:-100]
            return out
        except Exception as e:
            self.tasks.fail(task.id, str(e))
            raise
