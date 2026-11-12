"""P10.20 / P10.22 Centralized Assistant Task Lifecycle Service (backed by AssistantTaskRow)."""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable

from sqlalchemy import and_, or_, select, update

from ..db.engine import DatabaseManager
from ..db.models import ApprovalRequestRow, AssistantTaskRow, FilesystemPermissionRequestRow, FilesystemPermissionRow

TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "planning",
    "awaiting_permission",
    "awaiting_approval",
    "running",
    "paused",
    "waiting_quota",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
ACTIVE_TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "planning",
    "awaiting_permission",
    "awaiting_approval",
    "running",
    "paused",
    "waiting_quota",
)

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

    def __init__(self, db: DatabaseManager, event_publisher: Callable[[dict[str, Any]], None] | None = None):
        self.db = db
        self._publish = event_publisher

    def set_publisher(self, publisher: Callable[[dict[str, Any]], None] | None) -> None:
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
        deadline_at: float | None = None,
        step_budget: int = 20,
        token_budget: int = 120000,
        tool_budget: int = 40,
        payload: dict[str, Any] | None = None,
        checkpoint_json: dict[str, Any] | None = None,
        plan: Iterable[Any] | None = None,
        selected_capabilities: Iterable[str] | None = None,
        selected_agents: Iterable[str] | None = None,
        current_step: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        eff_status = status if status in TASK_STATUSES else "queued"
        now = time.time()
        row = AssistantTaskRow(
            user_request=str(user_request or ""),
            source=str(source or "text")[:24],
            intent=str(intent or "general_chat")[:64],
            status=eff_status,
            worker_kind=str(worker_kind or "assistant")[:48],
            attempts=0,
            max_attempts=max(1, int(max_attempts)),
            not_before=not_before,
            next_retry_at=None,
            deadline_at=deadline_at,
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
            result="",
            generated_files=[],
            error_info="",
            quota_reason="",
            payload=dict(payload or {}),
            checkpoint_json=dict(checkpoint_json or {}),
            steer_notes=[],
            completed_step_keys=[],
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
        result: str | None = None,
        generated_files: Iterable[Any] | None = None,
        error_info: str | None = None,
        quota_reason: str | None = None,
        checkpoint_json: dict[str, Any] | None = None,
        checkpoint_patch: dict[str, Any] | None = None,
        payload_patch: dict[str, Any] | None = None,
        not_before: float | None = None,
        clear_not_before: bool = False,
        next_retry_at: float | None = None,
        claimed_by: str | None = None,
        clear_claim: bool = False,
        meta_patch: dict[str, Any] | None = None,
        append_tool: str | None = None,
        append_file: str | dict[str, Any] | None = None,
        append_permission: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not task_id:
            return None
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if status is not None and status in TASK_STATUSES:
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
            if payload_patch:
                merged_p = dict(row.payload or {})
                merged_p.update(payload_patch)
                row.payload = merged_p
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
                stmt = stmt.where(AssistantTaskRow.status == status)
            elif active_only:
                stmt = stmt.where(AssistantTaskRow.status.in_(ACTIVE_TASK_STATUSES))
            rows = (await s.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def cancel_task(self, task_id: str, *, reason: str = "Cancelled by owner") -> dict[str, Any] | None:
        return await self.update_task(
            task_id,
            status="cancelled",
            current_step=reason,
            error_info=reason,
            clear_claim=True,
        )

    async def claim_next_task(
        self,
        worker_id: str,
        *,
        worker_kinds: Iterable[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest runnable queued (or cooled-down waiting_quota) task."""
        ts = now if now is not None else time.time()
        kinds = tuple(worker_kinds) if worker_kinds else None
        async with self.db.async_session() as s:
            cand_stmt = (
                select(AssistantTaskRow)
                .where(
                    or_(
                        AssistantTaskRow.status == "queued",
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
                        AssistantTaskRow.status.in_(("queued", "waiting_quota")),
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
                row.status = "failed"
                row.error_info = f"Retry budget exhausted ({attempts}/{max_att}): {error_info}"
                row.current_step = "Failed after retries"
                row.claimed_by = None
                row.claimed_at = None
                row.completed_at = now
                row.updated_at = now
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
        """Pause task in `waiting_quota` without losing progress when step/token/tool/rate limit hits."""
        now = time.time()
        resume_ts = (now + resume_after_s) if (resume_after_s is not None and resume_after_s > 0) else None
        return await self.update_task(
            task_id,
            status="waiting_quota",
            quota_reason=reason,
            current_step=current_step or f"Paused on quota/limit: {reason[:120]}",
            not_before=resume_ts,
            clear_not_before=(resume_ts is None),
            clear_claim=True,
        )

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

    async def recover_interrupted_on_boot(self) -> dict[str, list[str]]:
        """Classify active interrupted tasks safely on startup without auto-resuming unsafe/unapproved actions."""
        now = time.time()
        report: dict[str, list[str]] = {"requeued": [], "paused_unsafe": [], "halted_approval": [], "expired": []}
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

                # 2) Never auto-resume permission/approval gated or expired approval tasks.
                if row.status in ("awaiting_permission", "awaiting_approval", "paused"):
                    meta = dict(row.meta or {})
                    # Check associated FS permission requests or approval requests for expiry
                    fs_reqs = (
                        await s.execute(
                            select(FilesystemPermissionRequestRow).where(FilesystemPermissionRequestRow.task_id == row.task_id)
                        )
                    ).scalars().all()
                    expired_grant = any(r.status == "expired" or (r.expires_at and r.expires_at <= now) for r in fs_reqs)
                    fs_perms = (
                        await s.execute(
                            select(FilesystemPermissionRow).where(FilesystemPermissionRow.task_id == row.task_id)
                        )
                    ).scalars().all()
                    if any(p.status in ("expired", "revoked") or (p.expires_at and p.expires_at <= now) for p in fs_perms):
                        expired_grant = True
                    app_tok = meta.get("approval_token")
                    if app_tok:
                        ap_row = await s.get(ApprovalRequestRow, str(app_tok))
                        if ap_row and (ap_row.status in ("expired", "rejected", "cancelled") or (ap_row.expires_at and ap_row.expires_at <= now)):
                            expired_grant = True
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

                # 3) Running/planning tasks interrupted mid-flight: inspect safety strictly.
                if row.status in ("running", "planning"):
                    meta = dict(row.meta or {})
                    unsafe = (
                        row.intent in UNSAFE_AUTO_RESUME_INTENTS
                        or bool(meta.get("requires_explicit_approval"))
                        or bool(row.permissions_requested)
                        or (row.worker_kind not in SAFE_RESUMABLE_WORKER_KINDS and not meta.get("safe_resumable"))
                    )
                    if unsafe or (int(row.attempts or 0) >= int(row.max_attempts or 3)):
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
                        row.meta = meta
                        row.updated_at = now
                        report["requeued"].append(row.task_id)

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
            "worker_kind": row.worker_kind,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "not_before": row.not_before,
            "next_retry_at": row.next_retry_at,
            "deadline_at": row.deadline_at,
            "step_budget": row.step_budget,
            "steps_used": row.steps_used,
            "token_budget": row.token_budget,
            "tokens_used": row.tokens_used,
            "tool_budget": row.tool_budget,
            "tools_used_count": row.tools_used_count,
            "quota_reason": row.quota_reason,
            "payload": dict(row.payload or {}),
            "checkpoint_json": dict(row.checkpoint_json or {}),
            "steer_notes": list(row.steer_notes or []),
            "completed_step_keys": list(row.completed_step_keys or []),
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

