"""P10.22 / P10.22.1 Persistent Background Task Runner & Outcome Contract Enforcer."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Awaitable, Callable
import uuid

from jarvis.core.logging_ctx import correlation_scope
from jarvis.security.outcomes import (
    SIDE_EFFECTFUL_INTENTS,
    SIDE_EFFECTFUL_WORKER_KINDS,
    ToolOutcome,
    ToolStatus,
    VerificationCheck,
    parse_task_outcome,
)

from .tasks import AssistantTaskService

log = logging.getLogger("jarvis.task_runner")


class TransientTaskError(RuntimeError):
    """Raised by handlers when a transient failure should trigger exponential backoff retry."""


class QuotaSuspendedError(RuntimeError):
    """Raised when rate limits or resource quotas suspend a task into `waiting_quota`."""

    def __init__(self, reason: str, resume_after_s: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.resume_after_s = resume_after_s


class ApprovalRequiredTaskError(RuntimeError):
    """Raised when a task hits a permission or approval gate and must stop cleanly without auto-retry."""

    def __init__(self, reason: str, status: str = "awaiting_approval"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class OwnerCancelledTaskError(RuntimeError):
    """Raised strictly when a task was cancelled by the owner rather than application shutdown."""


@dataclass
class TaskExecutionResult:
    summary: str = ""
    result: str = ""
    generated_files: list[Any] | None = None
    outcome: dict[str, Any] | ToolOutcome | None = None
    artifact_outcome: dict[str, Any] | ToolOutcome | None = None
    side_effect_expected: bool = False
    meta_patch: dict[str, Any] | None = None


TaskHandler = Callable[["TaskRunContext", dict[str, Any]], Awaitable[TaskExecutionResult | dict[str, Any] | str]]


class TaskRunContext:
    """Checkpoint, budget, cancellation, shutdown, and steering helper passed into durable task handlers."""

    def __init__(self, runner: "BackgroundTaskRunner", task: dict[str, Any]):
        self.runner = runner
        self.tasks = runner.tasks
        self.task_id = task["task_id"]
        self._snapshot = dict(task)

    @property
    def is_shutting_down(self) -> bool:
        return self.runner.is_shutting_down

    @property
    def checkpoint(self) -> dict[str, Any]:
        return dict(self._snapshot.get("checkpoint_json") or {})

    @property
    def completed_step_keys(self) -> set[str]:
        return set(self._snapshot.get("completed_step_keys") or [])

    def is_step_done(self, step_key: str) -> bool:
        return step_key in self.completed_step_keys

    async def refresh(self) -> dict[str, Any]:
        latest = await self.tasks.get_task(self.task_id)
        if latest is not None:
            self._snapshot = latest
        return self._snapshot

    async def check_guardrails(self) -> list[dict[str, Any]]:
        """Check owner cancellation, deadline, step/token/tool budgets, and return pending steering notes."""
        snap = await self.refresh()
        if snap["status"] == "cancelled":
            raise OwnerCancelledTaskError("Task cancelled by owner")
        now = time.time()
        if snap.get("deadline_at") is not None and now > float(snap["deadline_at"]):
            raise TimeoutError("Task deadline exceeded")
        if int(snap.get("steps_used") or 0) >= int(snap.get("step_budget") or 20):
            raise QuotaSuspendedError("step_budget exhausted")
        if int(snap.get("tokens_used") or 0) >= int(snap.get("token_budget") or 120000):
            raise QuotaSuspendedError("token_budget exhausted")
        if int(snap.get("tools_used_count") or 0) >= int(snap.get("tool_budget") or 40):
            raise QuotaSuspendedError("tool_budget exhausted")
        return [n for n in (snap.get("steer_notes") or []) if not n.get("acknowledged")]

    async def complete_step(
        self,
        step_key: str,
        *,
        checkpoint_patch: dict[str, Any] | None = None,
        current_step: str | None = None,
        delta_steps: int = 1,
        delta_tokens: int = 0,
        delta_tools: int = 0,
    ) -> dict[str, Any]:
        """Idempotently mark a step key completed and persist checkpoint + budget usage."""
        updated = await self.tasks.record_step_completion(
            self.task_id,
            step_key,
            checkpoint_patch=checkpoint_patch,
            current_step=current_step,
            delta_steps=delta_steps,
            delta_tokens=delta_tokens,
            delta_tools=delta_tools,
        )
        if updated is not None:
            self._snapshot = updated
        return self._snapshot

    async def latest_steer_instructions(self) -> list[str]:
        notes = await self.check_guardrails()
        return [str(n.get("instruction") or "") for n in notes if n.get("instruction")]

    async def consume_steer_instructions(self) -> list[str]:
        """Check guardrails and atomically mark pending steering notes as acknowledged."""
        await self.check_guardrails()
        consumed = await self.tasks.acknowledge_steer_notes(self.task_id)
        await self.refresh()
        return [str(n.get("instruction") or "") for n in consumed if n.get("instruction")]


def _map_outcome_to_task_status(outcome: ToolOutcome | None) -> str:
    if outcome is None:
        return "completed"
    mapping = {
        ToolStatus.DONE.value: "completed",
        ToolStatus.UNVERIFIED.value: "unverified",
        ToolStatus.STARTED.value: "started",
        ToolStatus.FAILED.value: "failed",
        ToolStatus.AWAITING_APPROVAL.value: "awaiting_approval",
        ToolStatus.CANCELLED.value: "cancelled",
    }
    return mapping.get(outcome.status, "unverified")


class BackgroundTaskRunner:
    """Durable async worker executing queued `assistant_tasks` with graceful shutdown & outcome enforcement."""

    def __init__(
        self,
        tasks: AssistantTaskService,
        *,
        worker_id: str | None = None,
        poll_interval_s: float = 0.5,
        backoff_base_s: float = 1.5,
        backoff_max_s: float = 60.0,
        shutdown_grace_s: float = 5.0,
    ):
        self.tasks = tasks
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_max_s = float(backoff_max_s)
        self.shutdown_grace_s = max(0.0, float(shutdown_grace_s))
        self._handlers: dict[str, TaskHandler] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight_task: asyncio.Task[Any] | None = None
        self._active_task_id: str | None = None
        self._shutting_down = False
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self.last_recovery_report: dict[str, list[str]] | None = None

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def register_handler(self, worker_kind: str, handler: TaskHandler) -> None:
        self._handlers[str(worker_kind)] = handler

    def wake(self) -> None:
        self._wake_event.set()

    def begin_shutdown(self) -> None:
        """Signal controlled shutdown so no new tasks are claimed while active work drains."""
        self._shutting_down = True
        self._stop_event.set()
        self._wake_event.set()

    async def enqueue(
        self,
        user_request: str,
        *,
        worker_kind: str,
        intent: str = "background_task",
        source: str = "background",
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
        step_budget: int = 20,
        token_budget: int = 120000,
        tool_budget: int = 40,
        deadline_at: float | None = None,
        scheduled_for: float | None = None,
        correlation_id: str | None = None,
        parent_task_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        is_future = scheduled_for is not None and float(scheduled_for) > time.time()
        init_status = "scheduled" if is_future else "queued"
        task = await self.tasks.create_task(
            user_request=user_request,
            source=source,
            intent=intent,
            status=init_status,
            worker_kind=worker_kind,
            max_attempts=max_attempts,
            scheduled_for=scheduled_for,
            step_budget=step_budget,
            token_budget=token_budget,
            tool_budget=tool_budget,
            deadline_at=deadline_at,
            payload=payload,
            correlation_id=correlation_id,
            parent_task_id=parent_task_id,
            current_step=f"Scheduled for {worker_kind}" if is_future else f"Queued for {worker_kind}",
            meta=meta,
        )
        self.wake()
        return task

    async def start(self) -> dict[str, list[str]]:
        """Recover safe interrupted tasks cleanly and start the async worker loop."""
        self._shutting_down = False
        self._stop_event.clear()
        self.last_recovery_report = await self.tasks.recover_interrupted_on_boot()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop(), name=f"jarvis-bg-runner-{self.worker_id}")
        return self.last_recovery_report

    async def stop(self, *, grace_s: float | None = None) -> None:
        """Gracefully stop claiming new tasks, wait up to grace period, then preserve checkpoints on cancel."""
        self.begin_shutdown()
        wait_s = self.shutdown_grace_s if grace_s is None else max(0.0, float(grace_s))

        target: asyncio.Task[Any] | None = None
        if self._loop_task is not None and not self._loop_task.done():
            target = self._loop_task
        elif self._inflight_task is not None and not self._inflight_task.done():
            target = self._inflight_task

        if target is not None:
            if wait_s > 0.0:
                try:
                    await asyncio.wait_for(asyncio.shield(target), timeout=wait_s)
                except asyncio.TimeoutError:
                    log.info("Shutdown grace period (%.2fs) expired; cancelling active worker task cleanly", wait_s)
                    target.cancel()
                    try:
                        await target
                    except asyncio.CancelledError:
                        pass
                except asyncio.CancelledError:
                    pass
            else:
                target.cancel()
                try:
                    await target
                except asyncio.CancelledError:
                    pass

        self._loop_task = None
        self._inflight_task = None
        try:
            await self.tasks.release_worker_claims_on_shutdown(self.worker_id)
        except Exception as e:
            log.debug("Worker claim cleanup during shutdown skipped: %s", e)

    async def run_once(self) -> dict[str, Any] | None:
        """Attempt to claim and execute a single eligible queued task with strict outcome enforcement."""
        if self._shutting_down or self._stop_event.is_set():
            return None

        supported = list(self._handlers.keys()) if self._handlers else None
        task = await self.tasks.claim_next_task(self.worker_id, worker_kinds=supported)
        if task is None:
            return None

        kind = str(task.get("worker_kind") or "assistant")
        handler = self._handlers.get(kind)
        if handler is None:
            await self.tasks.schedule_retry(task["task_id"], error_info=f"No handler registered for worker_kind={kind!r}")
            return await self.tasks.get_task(task["task_id"])

        self._active_task_id = task["task_id"]
        current_async_task = asyncio.current_task()
        if self._loop_task is None or current_async_task is not self._loop_task:
            self._inflight_task = current_async_task

        ctx = TaskRunContext(self, task)
        try:
            with correlation_scope(
                correlation_id=(str(task.get("correlation_id") or "") or None),
                task_id=task["task_id"],
            ):
                await ctx.check_guardrails()
                raw_res = await handler(ctx, task)

            # Respect owner cancellation even if issued mid-step without another check_guardrails() call.
            latest_before_finish = await self.tasks.get_task(task["task_id"])
            if latest_before_finish and latest_before_finish["status"] == "cancelled":
                c_out = ToolOutcome(
                    tool_name=kind,
                    status=ToolStatus.CANCELLED.value,
                    summary="Task cancelled by owner",
                    target=task["task_id"],
                ).to_dict()
                return await self.tasks.update_task(
                    task["task_id"],
                    clear_claim=True,
                    meta_patch={"outcome": c_out, "outcome_status": ToolStatus.CANCELLED.value, "verified": False},
                )

            raw_outcome: Any = None
            raw_artifact_outcome: Any = None
            raw_side_effect = False
            if isinstance(raw_res, TaskExecutionResult):
                summary = raw_res.summary or raw_res.result or "Completed"
                result_txt = raw_res.result or raw_res.summary
                files = raw_res.generated_files
                raw_outcome = raw_res.outcome
                raw_artifact_outcome = raw_res.artifact_outcome
                raw_side_effect = bool(raw_res.side_effect_expected)
                meta_patch = dict(raw_res.meta_patch or {})
            elif isinstance(raw_res, dict):
                result_txt = str(raw_res.get("result") or raw_res.get("summary") or "")
                summary = str(raw_res.get("current_step") or raw_res.get("summary") or "Completed")
                files = raw_res.get("generated_files")
                raw_outcome = raw_res.get("outcome")
                raw_artifact_outcome = raw_res.get("artifact_outcome")
                raw_side_effect = bool(raw_res.get("side_effect_expected", False))
                meta_patch = dict(raw_res.get("meta_patch") or {})
            else:
                result_txt = str(raw_res or "")
                summary = "Completed"
                files = None
                meta_patch = {}

            # Remove any stale/unverified spoofed verification keys injected via handler meta_patch
            for reserved_key in ("outcome", "outcome_status", "verified", "artifact_outcome", "artifact_status", "partial_failure"):
                meta_patch.pop(reserved_key, None)

            task_meta = dict(task.get("meta") or {})
            side_effect_expected = (
                raw_side_effect
                or bool(task_meta.get("side_effectful"))
                or str(task.get("intent") or "") in SIDE_EFFECTFUL_INTENTS
                or kind in SIDE_EFFECTFUL_WORKER_KINDS
            )

            primary_outcome = parse_task_outcome(
                raw_outcome,
                tool_name=kind,
                side_effect_expected=side_effect_expected,
                fallback_summary=summary,
            )
            art_outcome = (
                parse_task_outcome(
                    raw_artifact_outcome,
                    tool_name=f"{kind}_artifact",
                    side_effect_expected=False,
                    fallback_summary="Artifact generation status",
                )
                if raw_artifact_outcome is not None
                else None
            )

            final_status = _map_outcome_to_task_status(primary_outcome)
            error_msg = ""

            if primary_outcome is not None:
                meta_patch["outcome"] = primary_outcome.to_dict()
                meta_patch["outcome_status"] = primary_outcome.status
                meta_patch["verified"] = primary_outcome.verified
                if primary_outcome.status == ToolStatus.FAILED.value:
                    error_msg = primary_outcome.error or primary_outcome.summary or "Side-effect outcome failed"
                    summary = primary_outcome.summary or summary
                elif primary_outcome.status == ToolStatus.UNVERIFIED.value:
                    summary = primary_outcome.summary or "Outcome could not be verified"
                elif primary_outcome.status == ToolStatus.STARTED.value:
                    summary = primary_outcome.summary or "Action started; completion unverified"
                elif primary_outcome.status == ToolStatus.AWAITING_APPROVAL.value:
                    summary = primary_outcome.summary or "Awaiting explicit approval"
                elif primary_outcome.status == ToolStatus.CANCELLED.value:
                    error_msg = primary_outcome.error or primary_outcome.summary or "Cancelled"
                    summary = error_msg

            if art_outcome is not None:
                meta_patch["artifact_outcome"] = art_outcome.to_dict()
                meta_patch["artifact_status"] = art_outcome.status
                meta_patch["artifact_verified"] = art_outcome.verified
                partial_fail = final_status == "completed" and art_outcome.status != ToolStatus.DONE.value
                meta_patch["partial_failure"] = partial_fail
                if partial_fail:
                    art_detail = art_outcome.error or art_outcome.summary or f"artifact {art_outcome.status}"
                    error_msg = f"Artifact generation {art_outcome.status}: {art_detail}"
                    summary = f"{summary} [export {art_outcome.status}]"

            return await self.tasks.update_task(
                task["task_id"],
                status=final_status,
                current_step=summary[:200],
                result=result_txt,
                generated_files=files,
                error_info=error_msg or None,
                meta_patch=meta_patch or None,
                clear_claim=True,
            )
        except OwnerCancelledTaskError as oce:
            c_out = ToolOutcome(
                tool_name=kind,
                status=ToolStatus.CANCELLED.value,
                summary=str(oce) or "Cancelled by owner",
                target=task["task_id"],
                error=str(oce) or "Cancelled by owner",
            ).to_dict()
            return await self.tasks.update_task(
                task["task_id"],
                status="cancelled",
                current_step=str(oce)[:200] or "Cancelled by owner",
                error_info=str(oce) or "Cancelled by owner",
                meta_patch={"outcome": c_out, "outcome_status": ToolStatus.CANCELLED.value, "verified": False},
                clear_claim=True,
            )
        except asyncio.CancelledError:
            async def _persist_cancel_state() -> None:
                latest = await self.tasks.get_task(task["task_id"])
                if latest and latest["status"] == "cancelled":
                    c_out = ToolOutcome(
                        tool_name=kind,
                        status=ToolStatus.CANCELLED.value,
                        summary="Cancelled by owner",
                        target=task["task_id"],
                    ).to_dict()
                    await self.tasks.update_task(
                        task["task_id"],
                        clear_claim=True,
                        meta_patch={"outcome": c_out, "outcome_status": ToolStatus.CANCELLED.value, "verified": False},
                    )
                else:
                    await self.tasks.preserve_on_shutdown(
                        task["task_id"],
                        reason=(
                            "Interrupted by application shutdown; checkpoint preserved"
                            if (self._shutting_down or self._stop_event.is_set())
                            else "Interrupted unexpectedly; checkpoint preserved"
                        ),
                    )

            cleanup_task = asyncio.create_task(_persist_cancel_state())
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            await cleanup_task
            raise
        except QuotaSuspendedError as qe:
            return await self.tasks.suspend_for_quota(
                task["task_id"],
                reason=qe.reason,
                resume_after_s=qe.resume_after_s,
            )
        except ApprovalRequiredTaskError as ae:
            ap_out = ToolOutcome(
                tool_name=kind,
                status=ToolStatus.AWAITING_APPROVAL.value,
                summary=ae.reason,
                target=task["task_id"],
            ).to_dict()
            return await self.tasks.update_task(
                task["task_id"],
                status=ae.status if ae.status in ("awaiting_permission", "awaiting_approval") else "awaiting_approval",
                current_step=ae.reason[:200],
                meta_patch={"outcome": ap_out, "outcome_status": ToolStatus.AWAITING_APPROVAL.value, "verified": False},
                clear_claim=True,
            )
        except TimeoutError as te:
            snap_latest = await self.tasks.get_task(task["task_id"]) or task
            partial = self.tasks.build_partial_report(snap_latest, reason=f"deadline_exceeded: {te}")
            to_out = ToolOutcome(
                tool_name=kind,
                status=ToolStatus.FAILED.value,
                summary="Deadline exceeded",
                target=task["task_id"],
                checks=[VerificationCheck("deadline_within_budget", False, str(te)).to_dict()],
                error=str(te),
            ).to_dict()
            return await self.tasks.update_task(
                task["task_id"],
                status="failed",
                current_step="Deadline exceeded",
                error_info=str(te),
                partial_report=partial,
                meta_patch={"outcome": to_out, "outcome_status": ToolStatus.FAILED.value, "verified": False},
                clear_claim=True,
            )
        except TransientTaskError as te:
            return await self.tasks.schedule_retry(
                task["task_id"],
                error_info=str(te),
                backoff_base_s=self.backoff_base_s,
                backoff_max_s=self.backoff_max_s,
            )
        except Exception as e:
            log.exception("Unhandled error in task %s (%s)", task["task_id"], kind)
            return await self.tasks.schedule_retry(
                task["task_id"],
                error_info=f"{type(e).__name__}: {e}",
                backoff_base_s=self.backoff_base_s,
                backoff_max_s=self.backoff_max_s,
            )
        finally:
            self._active_task_id = None
            if self._inflight_task is current_async_task:
                self._inflight_task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set() and not self._shutting_down:
            try:
                ran = await self.run_once()
                if ran is not None:
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("BackgroundTaskRunner tick failed: %s", e)

            if self._stop_event.is_set() or self._shutting_down:
                break
            self._wake_event.clear()
            if self._stop_event.is_set() or self._shutting_down:
                break
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
