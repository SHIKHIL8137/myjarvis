# JARVIS P10.22 — Durable Background Task Runner & Safe Recovery Report

## 1. Durable Task Schema & Migration `0005`
Extended `AssistantTaskRow` in [models.py](file:///d:/projects/myjarvis/jarvis/db/models.py), `_P10_COLUMN_DDLS` in [engine.py](file:///d:/projects/myjarvis/jarvis/db/engine.py), and Alembic migration [0005_p10_22_durable_tasks.py](file:///d:/projects/myjarvis/jarvis/db/migrations/versions/0005_p10_22_durable_tasks.py) (revising `0004_p10_20_filesystem_and_tasks`):
- Scheduling & Claiming: `worker_kind`, `attempts`, `max_attempts`, `not_before`, `next_retry_at`, `deadline_at`, `claimed_by`, `claimed_at`.
- Resource Budgets & Quota: `step_budget`, `steps_used`, `token_budget`, `tokens_used`, `tool_budget`, `tools_used_count`, `quota_reason`.
- Checkpoint & Steering State: `payload`, `checkpoint_json`, `steer_notes`, `completed_step_keys`.

Supports offline SQL compilation for PostgreSQL (`alembic upgrade 0004_p10_20_filesystem_and_tasks:head --sql`) and SQLite batch table alteration (`upgrade` / `downgrade` / `upgrade`).

## 2. Background Task Lifecycle ([tasks.py](file:///d:/projects/myjarvis/jarvis/agents/tasks.py), [task_runner.py](file:///d:/projects/myjarvis/jarvis/agents/task_runner.py))
- **Atomic Worker Claims**: `AssistantTaskService.claim_next_task(...)` performs a conditional compare-and-set `UPDATE` (`status IN ('queued', 'waiting_quota') AND claimed_by IS NULL`) so concurrent workers cannot execute the same task.
- **Checkpointing & Idempotent Steps**: `record_step_completion(...)` and `TaskRunContext.complete_step(...)` persist `completed_step_keys` and `checkpoint_json`, skipping already-finished stages cleanly on resume.
- **Retries & Quotas**: `schedule_retry(...)` applies bounded exponential backoff with jitter (`next_retry_at` / `not_before`) before transitioning exhausted tasks to `failed`. Rate-limit and budget limits (`step_budget`, `token_budget`, `tool_budget`, `QuotaSuspendedError`) transition tasks to `waiting_quota` rather than discarding progress.
- **Live Steering & Manual Resume API**: [api_v2.py](file:///d:/projects/myjarvis/jarvis/api_v2.py) exposes `POST /api/v2/tasks/{task_id}/steer` and `POST /api/v2/tasks/{task_id}/resume` (strictly rejecting `409` on permission/approval-gated tasks). [AssistantHome.tsx](file:///d:/projects/myjarvis/apps/frontend/src/features/AssistantHome.tsx) surfaces `waiting_quota` status badges and `quota_reason` inside the existing Central Task Manager drawer without adding navigation tabs.

## 3. Safe Boot Recovery Rules
`Assistant.start()` invokes `orchestrator.task_runner.start()`, calling `recover_interrupted_on_boot()` on startup:
- **Re-queued automatically**: only safe idempotent background kinds (`SAFE_RESUMABLE_WORKER_KINDS` such as `deep_research`, `knowledge_sync`, `reminder_check`, or `meta.safe_resumable=True`) with no pending/expired permissions or approvals.
- **Blocked from automatic resume**:
  - `awaiting_permission`, `awaiting_approval`, or `paused` tasks remain halted and flag `meta.approval_expired=True` if linked filesystem requests/grants or approval tokens expired.
  - Interrupted `running`/`planning` tasks with side-effectful intents (`filesystem_ops`, `shell_exec`, `docker_ops`, `recovery`, `git_push`, `worktree_merge`, `dev_autonomous`, `email_send`, `db_restore`) transition to `paused` with `meta.blocked_auto_resume_reason="unsafe_or_side_effectful_action"`.
  - Deadline-expired tasks transition cleanly to `failed`.

