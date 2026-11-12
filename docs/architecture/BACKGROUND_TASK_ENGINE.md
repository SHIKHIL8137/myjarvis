# Reliable Background Task Engine (P1)

## Overview

The P1 Background Task Engine provides durable, state-transition-validated, checkpointed background task execution. It supports restart recovery, live steering, step/token/tool budget enforcement, and revalidation of expired security grants upon resumption.

## Core Components

### 1. State Machine & Transitions (`jarvis.agents.tasks`)

Tasks advance through canonical states governed by `VALID_TASK_TRANSITIONS`:

```
                 ┌───────────────┐
                 │    queued     │
                 └──────┬────────┘
                        │
                        ▼
                 ┌───────────────┐
      ┌─────────►│    running    ├─────────┐
      │          └──────┬────────┘         │
      │                 │                  │
      │                 ▼                  ▼
┌─────┴──────────┐┌─────┴──────────┐┌──────┴─────────┐
│     paused     ││ waiting_quota  ││      done      │
└────────────────┘└────────────────┘└────────────────┘
        │                 │
        ▼                 ▼
 ┌─────────────┐   ┌─────────────┐
 │  cancelled  │   │   failed    │
 └─────────────┘   └─────────────┘
```

- **Canonical States**: `queued`, `scheduled`, `running`, `waiting`, `waiting_quota`, `paused`, `done`, `failed`, `cancelled`.
- **Illegal Transitions**: Attempting to transition from `paused` or `cancelled` directly to `completed` raises `TaskTransitionError` (HTTP 409).

### 2. Checkpointing and Partial Reports

- After each completed step in `_run_checkpointed_background_task`, the orchestrator writes:
  - `checkpoint_patch`: Map of step indices to step outcomes and artifacts.
  - `partial_report`: Structured JSON summarizing steps completed, budget spent, and reason for pause/suspension.
- If a task runs out of step budget, it transitions to `waiting_quota` with `quota_reason="step_budget_exhausted"` rather than failing or losing progress.

### 3. Grant Revalidation on Resume

- When `tasks.resume_task(task_id)` is invoked:
  - The engine scans `filesystem_permission_requests` and `action_pending_approvals` tied to `task_id`.
  - If any grant has expired or was revoked during pause, resumption is rejected with `TaskTransitionError`, and the task status is set to `awaiting_permission` or `awaiting_approval`.

### 4. Live Steering

- Users can submit steering instructions while a task is running or paused (`POST /v2/tasks/{id}/steer`).
- The runner consumes pending steer notes via `consume_steer_instructions()` at each step boundary and injects them into the task's context prompt.

### 5. Database Schema & Migration (`0009_p1_task_engine_fields`)

Adds the following columns to `assistant_tasks`:
- `scheduled_for` (Float, nullable, indexed): Deferred execution timestamp.
- `pause_reason` (String(240), default=""): Reason provided when pausing.
- `partial_report` (JSON, default=dict): Checkpoint report metadata.
- `correlation_id` (String(64), default=""): Request tracing identifier.
- `parent_task_id` (String(36), nullable, indexed): Delegation hierarchy tracking.

