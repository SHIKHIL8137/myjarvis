# P0–P6 Database Migrations & Upgrade Guide

## Overview

Phases P0 through P6 introduce four ordered Alembic migrations (`0008` through `0011`) that expand JARVIS's SQLite database schema without breaking backward compatibility.

All migrations support:
- Standard upgrade via Alembic CLI or synchronous bootstrap.
- Online batch alter table operations for SQLite.
- Clean offline and online downgrades with safety checks.

---

## Migration Lineage

```
0007_p11_project_aliases
           │
           ▼
0008_p11_command_history  (Unified command history & task replay)
           │
           ▼
0009_p1_task_engine_fields  (Durable tasks, scheduling, checkpoints, steering)
           │
           ▼
0010_p4_routines_and_proactive  (Routines, routine runs, proactive dismissals)
           │
           ▼
0011_p5_undo_journal  (Undo entries, reversible snapshots, action audit executions)
```

---

## Detailed Schema Additions

### 1. `0008_p11_command_history`
- Table: `command_history`
- Columns:
  - `id` (VARCHAR(36), PK)
  - `raw_text` (TEXT)
  - `normalized_command` (VARCHAR(400), INDEX)
  - `task_category` (VARCHAR(48), INDEX)
  - `intent` (VARCHAR(64), INDEX)
  - `department` (VARCHAR(32))
  - `task_id` (VARCHAR(36), INDEX, NULLABLE)
  - `project_alias` (VARCHAR(120), NULLABLE)
  - `target_path` (VARCHAR(800), NULLABLE)
  - `status` (VARCHAR(32), INDEX)
  - `tools_used` (JSON)
  - `paths_touched` (JSON)
  - `result_summary` (TEXT)
  - `error_info` (TEXT)
  - `replayable` (BOOLEAN, INDEX)
  - `replay_count` (INTEGER)
  - `source` (VARCHAR(24))
  - `created_at` (FLOAT, INDEX)
  - `updated_at` (FLOAT)
  - `completed_at` (FLOAT, NULLABLE)

### 2. `0009_p1_task_engine_fields`
- Alter Table: `assistant_tasks`
- New Columns:
  - `scheduled_for` (FLOAT, NULLABLE, INDEX): Time for deferred task execution.
  - `pause_reason` (VARCHAR(240), DEFAULT ""): User/system reason when pausing.
  - `partial_report` (JSON, DEFAULT dict): Checkpoint and budget report.
  - `correlation_id` (VARCHAR(64), DEFAULT "", INDEX): Unified tracing ID.
  - `parent_task_id` (VARCHAR(36), NULLABLE, INDEX): Workforce delegation hierarchy.

### 3. `0010_p4_routines_and_proactive`
- Table: `routines`
  - Columns: `id`, `title`, `description`, `trigger_type`, `trigger_spec`, `action_type`, `action_payload`, `enabled`, `late_policy`, `grace_period_s`, `channel`, `status`, `last_run_at`, `next_run_at`, `run_count`, `meta`, `created_at`, `updated_at`.
- Table: `routine_runs`
  - Columns: `id`, `routine_id`, `idempotency_key`, `status`, `scheduled_for`, `started_at`, `completed_at`, `summary`, `error`.
- Table: `proactive_dismissals`
  - Columns: `id` (PK, suggestion_id), `dismissed_until` (FLOAT), `created_at` (FLOAT).

### 4. `0011_p5_undo_journal`
- Table: `undo_entries`
  - Columns: `id` (PK), `action_type`, `target_path`, `resource_id`, `status`, `reversible`, `before_sha256`, `after_sha256`, `snapshot_data`, `reason`, `created_at`, `expires_at`, `reverted_at`.
- Table: `action_executions`
  - Columns: `id` (PK), `action_type`, `target`, `permission_level`, `status`, `verified`, `summary`, `checks`, `evidence`, `undo_id`, `error`, `created_at`.

---

## Upgrade Instructions

```powershell
# Activate Python environment
.venv\Scripts\Activate.ps1

# Run migrations to head
.venv\Scripts\python.exe -m alembic upgrade head

# Verify table schema integrity
.venv\Scripts\python.exe -c "from jarvis.db.engine import DatabaseManager; from sqlalchemy import inspect; db = DatabaseManager('sqlite:///data/jarvis.db'); print(inspect(db.sync_engine).get_table_names())"
```

## Downgrade Instructions

```powershell
# Rollback P5 undo journal
.venv\Scripts\python.exe -m alembic downgrade 0010_p4_routines_and_proactive

# Rollback P4 routines and proactive
.venv\Scripts\python.exe -m alembic downgrade 0009_p1_task_engine_fields

# Rollback P1 task engine fields
.venv\Scripts\python.exe -m alembic downgrade 0008_p11_command_history
```

