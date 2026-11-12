# JARVIS P10.22.1 — Task Lifecycle & Graceful Shutdown Report

## 1. Root Cause Fixed (FIX 1)
Previously, `BackgroundTaskRunner.stop()` immediately cancelled `_loop_task` before active steps could finish or recognize application shutdown, causing `run_once()`'s `asyncio.CancelledError` handler to mark recoverable in-flight tasks as owner-cancelled (`status="cancelled"`).

## 2. Controlled Shutdown & Cancellation Semantics
- **Explicit Distinction ([task_runner.py](file:///d:/projects/myjarvis/jarvis/agents/task_runner.py))**:
  - `OwnerCancelledTaskError` is raised by `TaskRunContext.check_guardrails()` when `task["status"] == "cancelled"`, ensuring owner-aborted tasks stay `cancelled` with `outcome.status="cancelled"`.
  - `BackgroundTaskRunner.run_once()` also re-checks owner cancellation right before committing handler completion so mid-step owner cancellations are never overwritten by stale `completed` transitions.
  - `BackgroundTaskRunner.begin_shutdown()` sets `_shutting_down` and `_stop_event` immediately so `run_once()` stops claiming new queued work (`if self._shutting_down or self._stop_event.is_set(): return None`).
  - `BackgroundTaskRunner.stop(grace_s=...)` waits up to `grace_s` (default `5.0s`) via `asyncio.wait_for(asyncio.shield(target), timeout=wait_s)` so short active steps complete cleanly before loop exit.
- **Forced Shutdown Preservation ([task_runner.py](file:///d:/projects/myjarvis/jarvis/agents/task_runner.py), [tasks.py](file:///d:/projects/myjarvis/jarvis/agents/tasks.py))**:
  - When the grace window expires and the active worker/inflight asyncio task is cancelled, `run_once()` intercepts `asyncio.CancelledError`, runs an uninterruptible cleanup coroutine (`AssistantTaskService.preserve_on_shutdown(...)`) inside a shield loop, releases `claimed_by`/`claimed_at`, preserves existing `completed_step_keys` and `checkpoint_json`, leaves recoverable status intact (`running`), records `meta.shutdown_interrupted=True`, and re-raises `asyncio.CancelledError`.
  - `stop()` also calls `release_worker_claims_on_shutdown(worker_id)` as a final safety net and is safely idempotent when called repeatedly.

## 3. Startup Recovery Safety Rules
`AssistantTaskService.recover_interrupted_on_boot()` inspects all active tasks across `ACTIVE_TASK_STATUSES`:
1. **Deadline expired**: transitions to `failed`.
2. **Permission / approval gated (`awaiting_permission`, `awaiting_approval`, `paused`, or pending/expired requests & tokens)**: remains halted, sets `meta.approval_expired=True` when expired, and never restores expired grants or auto-resumes.
3. **Side-effectful / unsafe tasks (`UNSAFE_AUTO_RESUME_INTENTS`, `permissions_requested`, `requires_explicit_approval`, non-safe worker kinds)**: transitions to `paused` with `meta.blocked_auto_resume_reason="unsafe_or_side_effectful_action"`, never auto-resumed.
4. **Eligible idempotent tasks (`deep_research`, `research`, `knowledge_sync`, `reminder_check`, `reindex`, or `safe_resumable=True`)**: reverifies research snapshot SHA-256 digests (`verify_checkpoint_snapshots`) first, then queues (`status="queued"`) to resume from the last completed checkpoint key.

