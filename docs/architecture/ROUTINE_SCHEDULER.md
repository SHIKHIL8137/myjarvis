# Unified Routine Scheduler & Proactive Assistance (P4)

## Overview

The P4 Routine Scheduler integrates recurring, one-time, startup, and conditional triggers into a unified service (`RoutineSchedulerService`). It enforces persistent quiet hours, late policy handling, and idempotent execution tracking while persisting proactive suggestion dismissals across restarts.

## Trigger Types & Recurrence Engine

The scheduler supports five trigger paradigms powered by APScheduler:
1. `cron`: Standard 5-field cron expression (e.g., `0 9 * * 1-5` for weekdays at 9am).
2. `every_n_minutes` / `interval`: Recurring at fixed interval seconds.
3. `daily` / `weekly` / `monthly`: User-friendly schedule definitions with hour and minute settings.
4. `startup`: Triggered once when JARVIS initializes (`run_count == 0`).
5. `conditional`: Metric-based threshold triggers (e.g., alert when disk free space < 10 GB).

## Quiet Hours Enforcement

Quiet hours are defined in `PreferencesSpec.quiet_hours`:
```json
{
  "enabled": true,
  "start_time": "22:00",
  "end_time": "07:00",
  "timezone": "local"
}
```
- `is_in_quiet_hours(qh, dt)` handles overnight midnight boundaries (e.g., `22:00` to `07:00`).
- During quiet hours, routine runs with auditory speech or notification actions are recorded as `status="skipped_quiet_hours"`, and next run timestamps are advanced without disturbing the user.

## Late Policy Handling

When the system was offline and a routine's `next_run_at` passed:
- `skip`: Skip the missed execution and calculate the next future run.
- `run_once_if_within_grace`: If within `grace_period_s` (default 3600s), execute once immediately, then advance schedule.
- `run_immediately`: Execute immediately regardless of elapsed time.

## Idempotency Journal

- Every routine execution generates an idempotency key: `routine:{id}:{timestamp_slot}`.
- Second polls or overlapping event triggers check `routine_runs` table before firing to prevent duplicate execution.

## Persistent Proactive Dismissals

- `ProactiveDismissalRow` in SQLite records user dismissals with an expiration timestamp (`dismissed_until`).
- When JARVIS restarts or when a new `ProactiveSuggestionEngine` is initialized, dismissed suggestion IDs are restored from the database, preventing re-prompting for recently dismissed suggestions.

