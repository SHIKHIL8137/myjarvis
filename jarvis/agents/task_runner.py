"""P10.22 P0.2 Persistent Background Task Runner & Worker Loop."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Awaitable, Callable
import uuid

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


@dataclass
class TaskExecutionResult:
    summary: str = ""
    result: str = ""
    generated_files: list[Any] | None = None
    outcome: dict[str, Any] | None = None
    meta_patch: dict[str, Any] | None = None


TaskHandler = Callable[["TaskRunContext", dict[str, Any]], Awaitable[TaskExecutionResult | dict[str, Any] | str]]


class TaskRunContext:
    """Checkpoint, budget, cancellation, and steering helper passed into durable task handlers."""

    def __init__(self, runner: "BackgroundTaskRunner", task: dict[str, Any]):
        self.runner = runner
        self.tasks = runner.tasks
        self.task_id = task["task_id"]
        self._snapshot = dict(task)

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
        """Check cancellation, deadline, step/token/tool budgets, and return pending steering notes."""
        snap = await self.refresh()
        if snap["status"] == "cancelled":
            raise asyncio.CancelledError("Task cancelled by owner")
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


class BackgroundTaskRunner:
    """Durable async worker executing queued `assistant_tasks` with safe startup recovery."""

    def __init__(
        self,
        tasks: AssistantTaskService,
        *,
        worker_id: str | None = None,
        poll_interval_s: float = 0.5,
        backoff_base_s: float = 1.5,
        backoff_max_s: float = 60.0,
    ):
        self.tasks = tasks
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_max_s = float(backoff_max_s)
        self._handlers: dict[str, TaskHandler] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self.last_recovery_report: dict[str, list[str]] | None = None

    def register_handler(self, worker_kind: str, handler: TaskHandler) -> None:
        self._handlers[str(worker_kind)] = handler

    def wake(self) -> None:
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
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = await self.tasks.create_task(
            user_request=user_request,
            source=source,
            intent=intent,
            status="queued",
            worker_kind=worker_kind,
            max_attempts=max_attempts,
            step_budget=step_budget,
            token_budget=token_budget,
            tool_budget=tool_budget,
            deadline_at=deadline_at,
            payload=payload,
            current_step=f"Queued for {worker_kind}",
            meta=meta,
        )
        self.wake()
        return task

    async def start(self) -> dict[str, list[str]]:
        """Recover safe interrupted tasks cleanly and start the async worker loop."""
        self.last_recovery_report = await self.tasks.recover_interrupted_on_boot()
        if self._loop_task is None or self._loop_task.done():
            self._stop_event.clear()
            self._loop_task = asyncio.create_task(self._run_loop(), name=f"jarvis-bg-runner-{self.worker_id}")
        return self.last_recovery_report

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None

    async def run_once(self) -> dict[str, Any] | None:
        """Attempt to claim and execute a single eligible queued task (deterministic for tests)."""
        supported = list(self._handlers.keys()) if self._handlers else None
        task = await self.tasks.claim_next_task(self.worker_id, worker_kinds=supported)
        if task is None:
            return None

        kind = str(task.get("worker_kind") or "assistant")
        handler = self._handlers.get(kind)
        if handler is None:
            await self.tasks.schedule_retry(task["task_id"], error_info=f"No handler registered for worker_kind={kind!r}")
            return await self.tasks.get_task(task["task_id"])

        ctx = TaskRunContext(self, task)
        try:
            await ctx.check_guardrails()
            raw_res = await handler(ctx, task)
            if isinstance(raw_res, TaskExecutionResult):
                summary = raw_res.summary or raw_res.result or "Completed"
                result_txt = raw_res.result or raw_res.summary
                files = raw_res.generated_files
                meta_patch = dict(raw_res.meta_patch or {})
                if raw_res.outcome is not None:
                    meta_patch["outcome"] = raw_res.outcome
            elif isinstance(raw_res, dict):
                result_txt = str(raw_res.get("result") or raw_res.get("summary") or "")
                summary = str(raw_res.get("current_step") or raw_res.get("summary") or "Completed")
                files = raw_res.get("generated_files")
                meta_patch = dict(raw_res.get("meta_patch") or {})
                if raw_res.get("outcome") is not None:
                    meta_patch["outcome"] = raw_res["outcome"]
            else:
                result_txt = str(raw_res or "")
                summary = "Completed"
                files = None
                meta_patch = {}

            return await self.tasks.update_task(
                task["task_id"],
                status="completed",
                current_step=summary[:200],
                result=result_txt,
                generated_files=files,
                meta_patch=meta_patch or None,
                clear_claim=True,
            )
        except asyncio.CancelledError as ce:
            latest = await self.tasks.get_task(task["task_id"])
            if latest and latest["status"] == "cancelled":
                await self.tasks.update_task(task["task_id"], clear_claim=True)
                return await self.tasks.get_task(task["task_id"])
            return await self.tasks.cancel_task(task["task_id"], reason=str(ce) or "Cancelled")
        except QuotaSuspendedError as qe:
            return await self.tasks.suspend_for_quota(
                task["task_id"],
                reason=qe.reason,
                resume_after_s=qe.resume_after_s,
            )
        except ApprovalRequiredTaskError as ae:
            return await self.tasks.update_task(
                task["task_id"],
                status=ae.status if ae.status in ("awaiting_permission", "awaiting_approval") else "awaiting_approval",
                current_step=ae.reason[:200],
                clear_claim=True,
            )
        except TimeoutError as te:
            return await self.tasks.update_task(
                task["task_id"],
                status="failed",
                current_step="Deadline exceeded",
                error_info=str(te),
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

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ran = await self.run_once()
                if ran is not None:
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("BackgroundTaskRunner tick failed: %s", e)

            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

