"""Permission-tiered computer/app operations, confirmation workflow, and tracked multi-step TaskManager."""
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import os
import shlex
import subprocess
import threading
import time
import uuid

from .config import ActionsSpec
from .filesystem import SafeFilesystem


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


_ACTION_LEVELS = {
    "search_files": PermissionLevel.READ,
    "read_file": PermissionLevel.READ,
    "inspect_project": PermissionLevel.READ,
    "open_file": PermissionLevel.OPEN,
    "open_app": PermissionLevel.OPEN,
    "create_file": PermissionLevel.MODIFY,
    "modify_file": PermissionLevel.MODIFY,
    "move_file": PermissionLevel.MODIFY,
    "delete_file": PermissionLevel.DESTRUCTIVE,
    "run_command": PermissionLevel.SYSTEM,
}


class ActionExecutor:
    """Executes validated computer/filesystem actions under strict permission levels & confirmation tokens."""

    def __init__(self, spec: ActionsSpec, fs: SafeFilesystem, tasks: TaskManager | None = None):
        self.spec = spec
        self.fs = fs
        self.tasks = tasks or TaskManager()
        self._pending: dict[str, PendingApproval] = {}
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def requires_confirmation(self, action: str) -> bool:
        lvl = _ACTION_LEVELS.get(action, PermissionLevel.SYSTEM)
        return lvl.value in set(self.spec.confirm_levels)

    def request_or_execute(self, action: str, args: dict, *, confirmed_token: str | None = None, preapproved: bool = False) -> dict:
        if not self.spec.enabled:
            raise ActionPermissionError("Computer and filesystem actions are disabled in settings")
        if action not in _ACTION_LEVELS:
            raise ActionPermissionError(f"Unsupported action: {action!r}")

        lvl = _ACTION_LEVELS[action]
        if self.requires_confirmation(action) and not preapproved:
            if confirmed_token:
                return self.confirm(confirmed_token)
            tok = uuid.uuid4().hex[:10]
            now = round(time.time(), 3)
            desc = f"{action}({args})"
            pa = PendingApproval(
                token=tok,
                action=action,
                level=lvl.value,
                description=desc,
                args=dict(args),
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
        return self._run(action, args, lvl)

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
                proc = subprocess.Popen(cmd, shell=False)
                res = {"launched": app_key, "pid": proc.pid}
                outcome = verify_process_state(tool_name="open_app", pid=proc.pid, target=app_key, started_only=True)
            elif action == "run_command":
                cmd_str = (args.get("command") or "").strip()
                if not cmd_str or any(c in cmd_str for c in ("|", "&", ";", ">", "<", "`", "$(", "\n", "\r")):
                    raise ActionPermissionError("Shell chaining/metacharacters are not permitted")
                if not any(cmd_str == prefix or cmd_str.startswith(prefix + " ") for prefix in self.spec.allowed_commands):
                    raise ActionPermissionError(f"Command {cmd_str!r} is not in allowed_commands: {self.spec.allowed_commands}")
                argv = shlex.split(cmd_str, posix=(os.name != "nt"))
                proc = subprocess.run(argv, cwd=str(self.fs.default_base), capture_output=True, text=True, timeout=20, shell=False)
                res = {"returncode": proc.returncode, "stdout": proc.stdout[:4000], "stderr": proc.stderr[:2000]}
                outcome = verify_process_state(tool_name="run_command", exit_code=proc.returncode, target=cmd_str)
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
