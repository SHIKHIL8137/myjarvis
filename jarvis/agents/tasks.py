"""P10.20 / P10.22 Centralized Assistant Task Lifecycle Service (backed by AssistantTaskRow)."""
from __future__ import annotations

from pathlib import Path
import random
import time
from typing import Any, Callable, Iterable

from sqlalchemy import and_, or_, select, update

from ..config import DATA
from ..core.contracts import TaskPartialReportContract, normalize_task_status
from ..core.errors import TaskTransitionError
from ..core.logging_ctx import correlation_id_var
from ..db.engine import DatabaseManager
from ..db.models import ApprovalRequestRow, AssistantTaskRow, FilesystemPermissionRequestRow, FilesystemPermissionRow
from ..security.outcomes import ToolOutcome, ToolStatus, VerificationCheck

TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "planning",
    "scheduled",
    "awaiting_clarification",
    "awaiting_permission",
    "awaiting_approval",
    "waiting",
    "running",
    "paused",
    "waiting_quota",
    "completed",
    "done",
    "unverified",
    "started",
    "failed",
    "cancelled",
)
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "done", "unverified", "started", "failed", "cancelled"})
ACTIVE_TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "planning",
    "scheduled",
    "awaiting_clarification",
    "awaiting_permission",
    "awaiting_approval",
    "waiting",
    "running",
    "paused",
    "waiting_quota",
)

_WAIT_STATES: frozenset[str] = frozenset({"waiting", "awaiting_clarification", "awaiting_permission", "awaiting_approval"})
_DONE_STATES: frozenset[str] = frozenset({"completed", "done", "unverified", "started"})

VALID_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"queued", "planning", "scheduled", "running", "paused", "waiting_quota", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "planning": frozenset({"planning", "queued", "scheduled", "running", "paused", "waiting_quota", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "scheduled": frozenset({"scheduled", "queued", "running", "paused", "failed", "cancelled"}),
    "running": frozenset({"running", "queued", "scheduled", "paused", "waiting_quota", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "waiting": frozenset({"waiting", "queued", "running", "paused", "waiting_quota", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "awaiting_clarification": frozenset({"awaiting_clarification", "queued", "planning", "running", "paused", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "awaiting_permission": frozenset({"awaiting_permission", "queued", "planning", "running", "paused", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "awaiting_approval": frozenset({"awaiting_approval", "queued", "planning", "running", "paused", "failed", "cancelled", *_WAIT_STATES, *_DONE_STATES}),
    "waiting_quota": frozenset({"waiting_quota", "queued", "running", "paused", "failed", "cancelled"}),
    "paused": frozenset({"paused", "queued", "scheduled", "running", "failed", "cancelled"}),
    "started": frozenset({"started", "running", "paused", "failed", "cancelled", *_WAIT_STATES, "completed", "done", "unverified"}),
    "unverified": frozenset({"unverified", "queued", "paused", "failed", "cancelled"}),
    "completed": frozenset({"completed"}),
    "done": frozenset({"done"}),
    "failed": frozenset({"failed", "queued", "cancelled"}),
    "cancelled": frozenset({"cancelled"}),
}


_CANONICAL_RETURN_ALIAS = {"done": "completed", "waiting": "paused"}


def validate_task_transition(current_status: str | None, next_status: str) -> str:
    """Validate durable task state transitions, raising TaskTransitionError on illegal jumps."""
    nxt = str(next_status or "").strip()
    if nxt not in TASK_STATUSES:
        raise TaskTransitionError(f"Unknown target task status: {nxt!r}")
    if current_status is None:
        return _CANONICAL_RETURN_ALIAS.get(nxt, nxt)
    cur = str(current_status).strip()
    if cur == nxt:
        return _CANONICAL_RETURN_ALIAS.get(nxt, nxt)
    allowed = VALID_TASK_TRANSITIONS.get(cur)
    if allowed is not None and nxt not in allowed:
        raise TaskTransitionError(
            f"Illegal task state transition: {cur} -> {nxt}",
            details={"current_status": cur, "next_status": nxt},
        )
    return _CANONICAL_RETURN_ALIAS.get(nxt, nxt)

UNSAFE_AUTO_RESUME_INTENTS: frozenset[str] = frozenset(
    {
        "filesystem_ops",
        "shell_exec",
        "docker_ops",
        "recovery",
        "git_push",
        "worktree_merge",
        "dev_autonomous",
        "email_send",
        "db_restore",
    }
)

SAFE_RESUMABLE_WORKER_KINDS: frozenset[str] = frozenset(
    {
        "deep_research",
        "research",
        "knowledge_sync",
        "reminder_check",
        "reindex",
    }
)


class AssistantTaskService:
    """Manages persistent end-to-end tasks routed through the central Jarvis assistant."""

    def __init__(
        self,
        db: DatabaseManager,
        event_publisher: Callable[[dict[str, Any]], Any] | None = None,
        *,
        data_dir: Path | None = None,
    ):
        self.db = db
        self.data_dir = Path(data_dir) if data_dir is not None else Path(getattr(db, "data_dir", DATA))
        self._publish = event_publisher

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any] | None) -> None:
        self._publish = publisher

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish({"type": "task_updated", "task": payload})
        except Exception:
            pass

    async def create_task(
        self,
        user_request: str,
        *,
        source: str = "text",
        intent: str = "general_chat",
        status: str = "planning",
        worker_kind: str = "assistant",
        max_attempts: int = 3,
        not_before: float | None = None,
        scheduled_for: float | None = None,
        deadline_at: float | None = None,
        step_budget: int = 20,
        token_budget: int = 120000,
        tool_budget: int = 40,
        payload: dict[str, Any] | None = None,
        checkpoint_json: dict[str, Any] | None = None,
        partial_report: dict[str, Any] | None = None,
        pause_reason: str = "",
        correlation_id: str | None = None,
        parent_task_id: str | None = None,
        plan: Iterable[Any] | None = None,
        selected_capabilities: Iterable[str] | None = None,
        selected_agents: Iterable[str] | None = None,
        current_step: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        eff_status = status if status in TASK_STATUSES else "queued"
        now = time.time()
        eff_corr = (correlation_id or "").strip() or (correlation_id_var.get() if correlation_id_var.get() != "-" else "")
        eff_not_before = scheduled_for if (scheduled_for is not None and not_before is None) else not_before
        row = AssistantTaskRow(
            user_request=str(user_request or ""),
            source=str(source or "text")[:24],
            intent=str(intent or "general_chat")[:64],
            status=eff_status,
            worker_kind=str(worker_kind or "assistant")[:48],
            attempts=0,
            max_attempts=max(1, int(max_attempts)),
            not_before=eff_not_before,
            next_retry_at=None,
            deadline_at=deadline_at,
            scheduled_for=scheduled_for,
            step_budget=max(1, int(step_budget)),
            steps_used=0,
            token_budget=max(1, int(token_budget)),
            tokens_used=0,
            tool_budget=max(1, int(tool_budget)),
            tools_used_count=0,
            plan=list(plan or []),
            selected_capabilities=list(selected_capabilities or []),
            selected_agents=list(selected_agents or []),
            tools_used=[],
            permissions_requested=[],
            current_step=str(current_step or "")[:200],
            pause_reason=str(pause_reason or "")[:240],
            result="",
            generated_files=[],
            error_info="",
            quota_reason="",
            payload=dict(payload or {}),
            checkpoint_json=dict(checkpoint_json or {}),
            partial_report=dict(partial_report or {}),
            steer_notes=[],
            completed_step_keys=[],
            correlation_id=eff_corr[:64],
            parent_task_id=(str(parent_task_id)[:36] if parent_task_id else None),
            claimed_by=None,
            claimed_at=None,
            meta=dict(meta or {}),
            created_at=now,
            updated_at=now,
            completed_at=now if eff_status in TERMINAL_TASK_STATUSES else None,
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        intent: str | None = None,
        plan: Iterable[Any] | None = None,
        selected_capabilities: Iterable[str] | None = None,
        selected_agents: Iterable[str] | None = None,
        tools_used: Iterable[str] | None = None,
        permissions_requested: Iterable[Any] | None = None,
        current_step: str | None = None,
        pause_reason: str | None = None,
        result: str | None = None,
        generated_files: Iterable[Any] | None = None,
        error_info: str | None = None,
        quota_reason: str | None = None,
        checkpoint_json: dict[str, Any] | None = None,
        checkpoint_patch: dict[str, Any] | None = None,
        partial_report: dict[str, Any] | None = None,
        payload_patch: dict[str, Any] | None = None,
        not_before: float | None = None,
        scheduled_for: float | None = None,
        clear_scheduled_for: bool = False,
        clear_not_before: bool = False,
        next_retry_at: float | None = None,
        claimed_by: str | None = None,
        clear_claim: bool = False,
        meta_patch: dict[str, Any] | None = None,
        append_tool: str | None = None,
        append_file: str | dict[str, Any] | None = None,
        append_permission: str | dict[str, Any] | None = None,
        enforce_transition: bool = False,
    ) -> dict[str, Any] | None:
        if not task_id:
            return None
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if status is not None and status in TASK_STATUSES:
                if enforce_transition:
                    validate_task_transition(row.status, status)
                row.status = status
                if status in TERMINAL_TASK_STATUSES:
                    row.claimed_by = None
                    row.claimed_at = None
                    if row.completed_at is None:
                        row.completed_at = now
                else:
                    row.completed_at = None
            if intent is not None:
                row.intent = str(intent)[:64]
            if plan is not None:
                row.plan = list(plan)
            if selected_capabilities is not None:
                row.selected_capabilities = list(selected_capabilities)
            if selected_agents is not None:
                row.selected_agents = list(selected_agents)
            if tools_used is not None:
                row.tools_used = list(tools_used)
            if append_tool:
                cur_tools = list(row.tools_used or [])
                if append_tool not in cur_tools:
                    cur_tools.append(append_tool)
                row.tools_used = cur_tools
            if permissions_requested is not None:
                row.permissions_requested = list(permissions_requested)
            if append_permission is not None:
                cur_perms = list(row.permissions_requested or [])
                if append_permission not in cur_perms:
                    cur_perms.append(append_permission)
                row.permissions_requested = cur_perms
            if current_step is not None:
                row.current_step = str(current_step)[:200]
            if pause_reason is not None:
                row.pause_reason = str(pause_reason)[:240]
            if result is not None:
                row.result = str(result)
            if generated_files is not None:
                row.generated_files = list(generated_files)
            if append_file is not None:
                cur_files = list(row.generated_files or [])
                if append_file not in cur_files:
                    cur_files.append(append_file)
                row.generated_files = cur_files
            if error_info is not None:
                row.error_info = str(error_info)
            if quota_reason is not None:
                row.quota_reason = str(quota_reason)[:200]
            if checkpoint_json is not None:
                row.checkpoint_json = dict(checkpoint_json)
            if checkpoint_patch:
                merged_cp = dict(row.checkpoint_json or {})
                merged_cp.update(checkpoint_patch)
                row.checkpoint_json = merged_cp
            if partial_report is not None:
                row.partial_report = dict(partial_report)
            if payload_patch:
                merged_p = dict(row.payload or {})
                merged_p.update(payload_patch)
                row.payload = merged_p
            if clear_scheduled_for:
                row.scheduled_for = None
            elif scheduled_for is not None:
                row.scheduled_for = scheduled_for
            if clear_not_before:
                row.not_before = None
            elif not_before is not None:
                row.not_before = not_before
            if next_retry_at is not None:
                row.next_retry_at = next_retry_at
            if clear_claim:
                row.claimed_by = None
                row.claimed_at = None
            elif claimed_by is not None:
                row.claimed_by = claimed_by[:64]
                row.claimed_at = now
            if meta_patch:
                merged = dict(row.meta or {})
                merged.update(meta_patch)
                row.meta = merged
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            return self._to_dict(row) if row is not None else None

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            stmt = select(AssistantTaskRow).order_by(AssistantTaskRow.created_at.desc()).limit(max(1, min(limit, 200)))
            if status:
                st_clean = str(status).strip().lower()
                if st_clean == "done":
                    stmt = stmt.where(AssistantTaskRow.status.in_(("completed", "done")))
                elif st_clean == "waiting":
                    stmt = stmt.where(AssistantTaskRow.status.in_(tuple(_WAIT_STATES)))
                else:
                    stmt = stmt.where(AssistantTaskRow.status == status)
            elif active_only:
                stmt = stmt.where(AssistantTaskRow.status.in_(ACTIVE_TASK_STATUSES))
            rows = (await s.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    @staticmethod
    def build_partial_report(task_or_row: Any, *, reason: str) -> dict[str, Any]:
        """Build structured partial progress report when a task pauses, suspends on quota, or times out."""
        get_val = task_or_row.get if isinstance(task_or_row, dict) else lambda k, d=None: getattr(task_or_row, k, d)
        completed_steps = [str(x) for x in (get_val("completed_step_keys") or [])]
        cp = dict(get_val("checkpoint_json") or {})
        budget = {
            "steps_used": int(get_val("steps_used") or 0),
            "step_budget": int(get_val("step_budget") or 20),
            "tokens_used": int(get_val("tokens_used") or 0),
            "token_budget": int(get_val("token_budget") or 120000),
            "tools_used_count": int(get_val("tools_used_count") or 0),
            "tool_budget": int(get_val("tool_budget") or 40),
        }
        report = TaskPartialReportContract(
            task_id=str(get_val("task_id") or ""),
            reason=str(reason or "stopped_before_completion")[:240],
            completed_steps=completed_steps,
            checkpoint_keys=sorted(str(k) for k in cp.keys()),
            budget_usage=budget,
            summary=(
                f"Stopped ({reason}): {len(completed_steps)} completed step(s), "
                f"steps={budget['steps_used']}/{budget['step_budget']}, "
                f"tokens={budget['tokens_used']}/{budget['token_budget']}, "
                f"tools={budget['tools_used_count']}/{budget['tool_budget']}."
            ),
            recorded_at=round(time.time(), 3),
        )
        return report.model_dump()

    async def pause_task(self, task_id: str, *, reason: str = "Paused by owner") -> dict[str, Any] | None:
        """Pause an active task while preserving checkpoints and recording a partial progress report."""
        cur = await self.get_task(task_id)
        if cur is None:
            return None
        validate_task_transition(cur["status"], "paused")
        partial = self.build_partial_report(cur, reason=reason)
        return await self.update_task(
            task_id,
            status="paused",
            pause_reason=reason or "Paused by owner",
            current_step=f"Paused: {(reason or 'Paused by owner')[:180]}",
            partial_report=partial,
            clear_claim=True,
            enforce_transition=True,
        )

    async def resume_task(self, task_id: str, *, reason: str = "Manually resumed by owner") -> dict[str, Any] | None:
        """Validate safety/approval gates and re-queue a paused, quota-suspended, or scheduled task."""
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if row.status in ("awaiting_permission", "awaiting_approval", "awaiting_clarification"):
                raise TaskTransitionError(
                    "Task requires explicit permission or approval decision before resuming",
                    details={"current_status": row.status},
                )
            if row.status not in ("paused", "waiting_quota", "queued", "scheduled"):
                raise TaskTransitionError(
                    f"Task cannot be resumed from status '{row.status}'",
                    details={"current_status": row.status},
                )
            # Revalidate any bound FS/approval grants before resuming protected work
            meta = dict(row.meta or {})
            fs_reqs = (
                await s.execute(select(FilesystemPermissionRequestRow).where(FilesystemPermissionRequestRow.task_id == row.task_id))
            ).scalars().all()
            if any(r.status == "pending" for r in fs_reqs):
                raise TaskTransitionError("Pending filesystem permission request must be resolved before resuming")
            if any(r.status == "expired" or (r.expires_at and r.expires_at <= now) for r in fs_reqs):
                meta["approval_expired"] = True
                row.meta = meta
                row.status = "awaiting_permission"
                await s.commit()
                raise TaskTransitionError("Filesystem permission expired; re-approval required before resuming")
            app_tok = meta.get("approval_token")
            if app_tok:
                ap_row = await s.get(ApprovalRequestRow, str(app_tok))
                if ap_row and (ap_row.status in ("expired", "rejected", "cancelled") or (ap_row.expires_at and ap_row.expires_at <= now)):
                    meta["approval_expired"] = True
                    row.meta = meta
                    row.status = "awaiting_approval"
                    await s.commit()
                    raise TaskTransitionError("Approval token expired or rejected; manual re-approval required")
            validate_task_transition(row.status, "queued")
            row.status = "queued"
            row.pause_reason = ""
            row.quota_reason = ""
            row.not_before = None
            row.scheduled_for = None
            row.claimed_by = None
            row.claimed_at = None
            row.current_step = str(reason or "Manually resumed by owner")[:200]
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def cancel_task(self, task_id: str, *, reason: str = "Cancelled by owner") -> dict[str, Any] | None:
        cancelled_out = ToolOutcome(
            tool_name="assistant_task",
            status=ToolStatus.CANCELLED.value,
            summary=reason or "Cancelled by owner",
            checks=[],
            error=reason or "Cancelled by owner",
        ).to_dict()
        return await self.update_task(
            task_id,
            status="cancelled",
            current_step=reason,
            error_info=reason,
            clear_claim=True,
            meta_patch={"outcome": cancelled_out, "outcome_status": ToolStatus.CANCELLED.value, "verified": False},
        )

    async def claim_next_task(
        self,
        worker_id: str,
        *,
        worker_kinds: Iterable[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest runnable queued, due scheduled, or cooled-down waiting_quota task."""
        ts = now if now is not None else time.time()
        kinds = tuple(worker_kinds) if worker_kinds else None
        async with self.db.async_session() as s:
            cand_stmt = (
                select(AssistantTaskRow)
                .where(
                    or_(
                        AssistantTaskRow.status == "queued",
                        and_(
                            AssistantTaskRow.status == "scheduled",
                            or_(AssistantTaskRow.scheduled_for.is_(None), AssistantTaskRow.scheduled_for <= ts),
                        ),
                        and_(
                            AssistantTaskRow.status == "waiting_quota",
                            AssistantTaskRow.not_before.is_not(None),
                            AssistantTaskRow.not_before <= ts,
                        ),
                    ),
                    AssistantTaskRow.claimed_by.is_(None),
                    or_(AssistantTaskRow.not_before.is_(None), AssistantTaskRow.not_before <= ts),
                )
                .order_by(AssistantTaskRow.created_at.asc())
                .limit(8)
            )
            if kinds:
                cand_stmt = cand_stmt.where(AssistantTaskRow.worker_kind.in_(kinds))
            candidates = (await s.execute(cand_stmt)).scalars().all()
            for row in candidates:
                claim_stmt = (
                    update(AssistantTaskRow)
                    .where(
                        AssistantTaskRow.task_id == row.task_id,
                        AssistantTaskRow.status.in_(("queued", "scheduled", "waiting_quota")),
                        AssistantTaskRow.claimed_by.is_(None),
                    )
                    .values(
                        status="running",
                        claimed_by=worker_id[:64],
                        claimed_at=ts,
                        attempts=(row.attempts or 0) + 1,
                        next_retry_at=None,
                        quota_reason="",
                        updated_at=ts,
                    )
                )
                res: Any = await s.execute(claim_stmt)
                if getattr(res, "rowcount", 0) == 1:
                    await s.commit()
                    fresh = await s.get(AssistantTaskRow, row.task_id)
                    if fresh is not None:
                        data = self._to_dict(fresh)
                        self._emit(data)
                        return data
                else:
                    await s.rollback()
        return None

    async def record_step_completion(
        self,
        task_id: str,
        step_key: str,
        *,
        checkpoint_patch: dict[str, Any] | None = None,
        current_step: str | None = None,
        delta_steps: int = 1,
        delta_tokens: int = 0,
        delta_tools: int = 0,
    ) -> dict[str, Any] | None:
        """Persist idempotent step marker, checkpoint patch, and updated usage counters."""
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            keys = list(row.completed_step_keys or [])
            if step_key and step_key not in keys:
                keys.append(step_key)
                row.completed_step_keys = keys
            if checkpoint_patch:
                cp = dict(row.checkpoint_json or {})
                cp.update(checkpoint_patch)
                row.checkpoint_json = cp
            if current_step is not None:
                row.current_step = str(current_step)[:200]
            row.steps_used = max(0, int(row.steps_used or 0) + delta_steps)
            row.tokens_used = max(0, int(row.tokens_used or 0) + delta_tokens)
            row.tools_used_count = max(0, int(row.tools_used_count or 0) + delta_tools)
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def schedule_retry(
        self,
        task_id: str,
        *,
        error_info: str,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 120.0,
        jitter_fn: Callable[[float, float], float] | None = None,
    ) -> dict[str, Any] | None:
        """Re-queue with exponential backoff + jitter if attempts remain, otherwise mark failed."""
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            attempts = int(row.attempts or 0)
            max_att = max(1, int(row.max_attempts or 3))
            if attempts >= max_att:
                err_msg = f"Retry budget exhausted ({attempts}/{max_att}): {error_info}"
                row.status = "failed"
                row.error_info = err_msg
                row.current_step = "Failed after retries"
                row.claimed_by = None
                row.claimed_at = None
                row.completed_at = now
                row.updated_at = now
                meta = dict(row.meta or {})
                failed_out = ToolOutcome(
                    tool_name=str(row.worker_kind or "assistant"),
                    status=ToolStatus.FAILED.value,
                    summary="Failed after retries",
                    target=row.task_id,
                    checks=[VerificationCheck("handler_execution", False, str(error_info)[:160]).to_dict()],
                    error=err_msg,
                ).to_dict()
                meta.update({"outcome": failed_out, "outcome_status": ToolStatus.FAILED.value, "verified": False})
                row.meta = meta
            else:
                raw_delay = min(backoff_max_s, backoff_base_s * (2 ** max(0, attempts - 1)))
                rand_jit = jitter_fn(0.0, raw_delay * 0.25) if jitter_fn else random.uniform(0.0, raw_delay * 0.25)  # noqa: S311
                delay = round(raw_delay + max(0.0, rand_jit), 3)
                retry_at = now + delay
                row.status = "queued"
                row.error_info = str(error_info)
                row.current_step = f"Retry scheduled in {delay:.1f}s (attempt {attempts}/{max_att})"
                row.not_before = retry_at
                row.next_retry_at = retry_at
                row.claimed_by = None
                row.claimed_at = None
                row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def suspend_for_quota(
        self,
        task_id: str,
        *,
        reason: str,
        resume_after_s: float | None = None,
        current_step: str | None = None,
    ) -> dict[str, Any] | None:
        """Pause task in `waiting_quota` without losing progress and save a graceful partial report."""
        now = time.time()
        cur = await self.get_task(task_id)
        partial = self.build_partial_report(cur or {"task_id": task_id}, reason=reason)
        resume_ts = (now + resume_after_s) if (resume_after_s is not None and resume_after_s > 0) else None
        return await self.update_task(
            task_id,
            status="waiting_quota",
            quota_reason=reason,
            pause_reason=reason[:240],
            partial_report=partial,
            current_step=current_step or f"Paused on quota/limit: {reason[:120]}",
            not_before=resume_ts,
            clear_not_before=(resume_ts is None),
            clear_claim=True,
        )

    async def acknowledge_steer_notes(self, task_id: str) -> list[dict[str, Any]]:
        """Mark all unacknowledged steering instructions as acknowledged and return newly consumed items."""
        now = round(time.time(), 3)
        consumed: list[dict[str, Any]] = []
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return []
            notes = list(row.steer_notes or [])
            changed = False
            new_notes: list[dict[str, Any]] = []
            for item in notes:
                d = dict(item)
                if not d.get("acknowledged"):
                    d["acknowledged"] = True
                    d["acked_at"] = now
                    consumed.append(dict(d))
                    changed = True
                new_notes.append(d)
            if changed:
                row.steer_notes = new_notes
                row.updated_at = time.time()
                await s.commit()
                await s.refresh(row)
        if changed:
            self._emit(self._to_dict(row))
        return consumed

    async def append_steer(
        self,
        task_id: str,
        instruction: str,
        *,
        author: str = "owner",
        resume_if_paused: bool = False,
    ) -> dict[str, Any] | None:
        """Append live steering instruction to task; safe while running, queued, paused, or waiting_quota."""
        clean = (instruction or "").strip()
        if not clean:
            raise ValueError("Steering instruction cannot be empty")
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if row.status in TERMINAL_TASK_STATUSES:
                raise ValueError(f"Cannot steer task in terminal state '{row.status}'")
            notes = list(row.steer_notes or [])
            notes.append({"instruction": clean, "author": author, "ts": round(now, 3), "acknowledged": False})
            row.steer_notes = notes
            if resume_if_paused and row.status in ("paused", "waiting_quota"):
                row.status = "queued"
                row.not_before = None
                row.quota_reason = ""
                row.claimed_by = None
                row.claimed_at = None
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def preserve_on_shutdown(
        self,
        task_id: str,
        *,
        reason: str = "Interrupted by application shutdown; checkpoint preserved",
    ) -> dict[str, Any] | None:
        """Preserve in-flight task progress and release worker claim on shutdown without cancelling."""
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if row.status in TERMINAL_TASK_STATUSES:
                row.claimed_by = None
                row.claimed_at = None
                row.updated_at = now
            else:
                # Keep recoverable or gated status; never mark owner-cancelled or discard checkpoints.
                if row.status not in ("awaiting_permission", "awaiting_approval", "paused", "waiting_quota", "queued"):
                    row.status = "running"
                    row.current_step = str(reason or "Interrupted by shutdown")[:200]
                row.claimed_by = None
                row.claimed_at = None
                meta = dict(row.meta or {})
                meta["shutdown_interrupted"] = True
                meta["shutdown_ts"] = round(now, 3)
                row.meta = meta
                row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def release_worker_claims_on_shutdown(self, worker_id: str) -> list[str]:
        """Ensure any non-terminal tasks still held by `worker_id` release claims cleanly during shutdown."""
        async with self.db.async_session() as s:
            rows = (
                await s.execute(
                    select(AssistantTaskRow).where(
                        AssistantTaskRow.claimed_by == worker_id[:64],
                    )
                )
            ).scalars().all()
            task_ids = [r.task_id for r in rows]
        touched: list[str] = []
        for tid in task_ids:
            if await self.preserve_on_shutdown(tid) is not None:
                touched.append(tid)
        return touched

    async def recover_interrupted_on_boot(self) -> dict[str, list[str]]:
        """Classify active interrupted tasks safely on startup without auto-resuming unsafe/unapproved actions."""
        from .research import verify_checkpoint_snapshots

        now = time.time()
        report: dict[str, list[str]] = {
            "requeued": [],
            "paused_unsafe": [],
            "halted_approval": [],
            "expired": [],
            "snapshot_integrity_warnings": [],
        }
        async with self.db.async_session() as s:
            rows = (
                await s.execute(select(AssistantTaskRow).where(AssistantTaskRow.status.in_(ACTIVE_TASK_STATUSES)))
            ).scalars().all()

            for row in rows:
                # 1) Deadline expiry always stops the task cleanly.
                if row.deadline_at is not None and now > float(row.deadline_at):
                    row.status = "failed"
                    row.error_info = "Task deadline expired while offline"
                    row.current_step = "Expired prior to restart"
                    row.claimed_by = None
                    row.claimed_at = None
                    row.completed_at = now
                    row.updated_at = now
                    report["expired"].append(row.task_id)
                    continue

                # Check associated FS permission requests, active grants, and approval tokens across active tasks.
                meta = dict(row.meta or {})
                fs_reqs = (
                    await s.execute(
                        select(FilesystemPermissionRequestRow).where(FilesystemPermissionRequestRow.task_id == row.task_id)
                    )
                ).scalars().all()
                expired_grant = any(r.status == "expired" or (r.expires_at and r.expires_at <= now) for r in fs_reqs)
                pending_perm_req = any(r.status == "pending" for r in fs_reqs)
                fs_perms = (
                    await s.execute(
                        select(FilesystemPermissionRow).where(FilesystemPermissionRow.task_id == row.task_id)
                    )
                ).scalars().all()
                if any(p.status in ("expired", "revoked") or (p.expires_at and p.expires_at <= now) for p in fs_perms):
                    expired_grant = True
                app_tok = meta.get("approval_token")
                pending_approval = False
                if app_tok:
                    ap_row = await s.get(ApprovalRequestRow, str(app_tok))
                    if ap_row:
                        if ap_row.status in ("expired", "rejected", "cancelled") or (ap_row.expires_at and ap_row.expires_at <= now):
                            expired_grant = True
                        elif ap_row.status == "pending":
                            pending_approval = True

                # Reverify persisted research snapshots against stored SHA-256 on startup recovery.
                cp = dict(row.checkpoint_json or {})
                if cp.get("snapshots"):
                    valid_snaps, rejected_snaps, int_warnings = verify_checkpoint_snapshots(
                        row.task_id,
                        cp,
                        data_dir=self.data_dir,
                    )
                    if int_warnings:
                        cp_warnings = list(cp.get("integrity_warnings") or [])
                        for w in int_warnings:
                            if w not in cp_warnings:
                                cp_warnings.append(w)
                        cp["integrity_warnings"] = cp_warnings
                        cp["rejected_snapshots"] = rejected_snaps
                        row.checkpoint_json = cp
                        meta["snapshot_integrity"] = {
                            "valid_count": len(valid_snaps),
                            "rejected": rejected_snaps,
                            "warnings": int_warnings,
                        }
                        meta["integrity_warnings"] = list(dict.fromkeys([*(meta.get("integrity_warnings") or []), *int_warnings]))
                        row.meta = meta
                        report["snapshot_integrity_warnings"].append(row.task_id)

                # 2) Never auto-resume permission/approval gated or expired approval tasks.
                if (
                    row.status in ("awaiting_permission", "awaiting_approval", "paused")
                    or expired_grant
                    or pending_perm_req
                    or pending_approval
                ):
                    if row.status in ("queued", "waiting_quota", "running", "planning"):
                        row.status = "awaiting_approval" if (pending_approval or app_tok) else "awaiting_permission"
                    if expired_grant:
                        meta["approval_expired"] = True
                        row.meta = meta
                        row.current_step = "Permission or approval expired — manual re-approval required"
                    else:
                        meta["requires_manual_resume"] = True
                        row.meta = meta
                    row.claimed_by = None
                    row.claimed_at = None
                    row.updated_at = now
                    report["halted_approval"].append(row.task_id)
                    continue

                # 3) Release any stale worker claim on waiting_quota tasks cleanly.
                if row.status == "waiting_quota":
                    row.claimed_by = None
                    row.claimed_at = None
                    row.updated_at = now
                    continue

                # 4) Running/planning (or shutdown-preserved) tasks: inspect safety strictly before re-queueing.
                if row.status in ("running", "planning"):
                    unsafe = (
                        row.intent in UNSAFE_AUTO_RESUME_INTENTS
                        or bool(meta.get("requires_explicit_approval"))
                        or bool(row.permissions_requested)
                        or (row.worker_kind not in SAFE_RESUMABLE_WORKER_KINDS and not meta.get("safe_resumable"))
                    )
                    exhausted = int(row.attempts or 0) >= int(row.max_attempts or 3) and not meta.get("shutdown_interrupted")
                    if unsafe or exhausted:
                        row.status = "paused" if unsafe else "failed"
                        if unsafe:
                            row.current_step = "Interrupted on shutdown — manual confirmation required before resuming"
                            meta["blocked_auto_resume_reason"] = "unsafe_or_side_effectful_action"
                            row.meta = meta
                            report["paused_unsafe"].append(row.task_id)
                        else:
                            row.error_info = "Max retry attempts reached prior to restart"
                            row.current_step = "Failed after interrupted retries"
                            row.completed_at = now
                            report["expired"].append(row.task_id)
                        row.claimed_by = None
                        row.claimed_at = None
                        row.updated_at = now
                    else:
                        row.status = "queued"
                        row.claimed_by = None
                        row.claimed_at = None
                        row.current_step = "Recovered after restart — queued to resume from last checkpoint"
                        meta["recovered_from_boot"] = True
                        meta.pop("shutdown_interrupted", None)
                        row.meta = meta
                        row.updated_at = now
                        report["requeued"].append(row.task_id)
                elif row.status in ("queued", "scheduled"):
                    # Keep future schedule intact, clear any stale worker claim cleanly.
                    row.claimed_by = None
                    row.claimed_at = None
                    row.updated_at = now

            await s.commit()
        return report

    @staticmethod
    def _to_dict(row: AssistantTaskRow) -> dict[str, Any]:
        return {
            "task_id": row.task_id,
            "user_request": row.user_request,
            "source": row.source,
            "intent": row.intent,
            "status": row.status,
            "canonical_status": normalize_task_status(row.status).value,
            "worker_kind": row.worker_kind,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "not_before": row.not_before,
            "scheduled_for": row.scheduled_for,
            "next_retry_at": row.next_retry_at,
            "deadline_at": row.deadline_at,
            "step_budget": row.step_budget,
            "steps_used": row.steps_used,
            "token_budget": row.token_budget,
            "tokens_used": row.tokens_used,
            "tool_budget": row.tool_budget,
            "tools_used_count": row.tools_used_count,
            "quota_reason": row.quota_reason,
            "pause_reason": row.pause_reason,
            "payload": dict(row.payload or {}),
            "checkpoint_json": dict(row.checkpoint_json or {}),
            "partial_report": dict(row.partial_report or {}),
            "steer_notes": list(row.steer_notes or []),
            "completed_step_keys": list(row.completed_step_keys or []),
            "correlation_id": row.correlation_id,
            "parent_task_id": row.parent_task_id,
            "claimed_by": row.claimed_by,
            "claimed_at": row.claimed_at,
            "plan": list(row.plan or []),
            "selected_capabilities": list(row.selected_capabilities or []),
            "selected_agents": list(row.selected_agents or []),
            "tools_used": list(row.tools_used or []),
            "permissions_requested": list(row.permissions_requested or []),
            "current_step": row.current_step,
            "result": row.result,
            "generated_files": list(row.generated_files or []),
            "error_info": row.error_info,
            "meta": dict(row.meta or {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
        }

