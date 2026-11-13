"""Unified Routine Scheduler for P4: Cron, one-time, interval, and conditional routines.

Supports:
- APScheduler CronTrigger, DateTrigger, IntervalTrigger integration
- Persistent quiet hours evaluation (PreferencesSpec.quiet_hours)
- Late / expired policy handling (skip, run_once_if_within_grace, run_immediately)
- Restart recovery & idempotency key execution journal
- ReminderService backwards-compatibility
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
import logging
import time
from typing import Any
import uuid

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from ...core.contracts import QuietHoursSpec
from ...core.event_bus import EventBus
from ...db.engine import DatabaseManager
from ...db.models import RoutineRow, RoutineRunRow

log = logging.getLogger("jarvis.routines")


def is_in_quiet_hours(quiet_hours: dict[str, Any] | QuietHoursSpec, check_dt: datetime | None = None) -> bool:
    """Evaluate whether check_dt falls inside configured persistent quiet hours."""
    if isinstance(quiet_hours, dict):
        if not quiet_hours.get("enabled", False):
            return False
        if "start_hour" in quiet_hours and "start" not in quiet_hours:
            start_str = f"{int(quiet_hours.get('start_hour', 22)):02d}:00"
            end_str = f"{int(quiet_hours.get('end_hour', 7)):02d}:00"
        else:
            start_str = str(quiet_hours.get("start", "22:00")).strip()
            end_str = str(quiet_hours.get("end", "07:00")).strip()
    else:
        if not quiet_hours.enabled:
            return False
        start_str = f"{quiet_hours.start_hour:02d}:00"
        end_str = f"{quiet_hours.end_hour:02d}:00"

    try:
        sh, sm = [int(x) for x in start_str.split(":", 1)]
        eh, em = [int(x) for x in end_str.split(":", 1)]
        st = dt_time(sh, sm)
        et = dt_time(eh, em)
    except Exception:
        return False

    now_t = (check_dt or datetime.now()).time()
    if st <= et:
        return st <= now_t <= et
    # Wraps around midnight (e.g. 22:00 -> 07:00)
    return now_t >= st or now_t <= et


def compute_next_run(
    trigger_type: str,
    trigger_spec: dict[str, Any],
    from_time: float,
    *,
    allow_past_once: bool = False,
) -> float | None:
    """Compute the next UNIX timestamp for a routine trigger using APScheduler triggers."""
    base_dt = datetime.fromtimestamp(from_time)
    tt = str(trigger_type).lower()

    if tt == "once":
        due = float(trigger_spec.get("due_at") or trigger_spec.get("timestamp") or 0.0)
        if allow_past_once:
            return due if due > 0 else None
        return due if due > from_time else None

    if tt == "every_n_minutes":
        mins = max(1, int(trigger_spec.get("minutes") or 15))
        return from_time + (mins * 60)

    if tt == "daily":
        hour = int(trigger_spec.get("hour") or 9)
        minute = int(trigger_spec.get("minute") or 0)
        trig = CronTrigger(hour=hour, minute=minute)
        nxt = trig.get_next_fire_time(None, base_dt)
        return nxt.timestamp() if nxt else None

    if tt == "weekly":
        day_of_week = str(trigger_spec.get("day_of_week") or "mon")
        hour = int(trigger_spec.get("hour") or 9)
        minute = int(trigger_spec.get("minute") or 0)
        trig = CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute)
        nxt = trig.get_next_fire_time(None, base_dt)
        return nxt.timestamp() if nxt else None

    if tt == "monthly":
        day = int(trigger_spec.get("day") or 1)
        hour = int(trigger_spec.get("hour") or 9)
        minute = int(trigger_spec.get("minute") or 0)
        trig = CronTrigger(day=day, hour=hour, minute=minute)
        nxt = trig.get_next_fire_time(None, base_dt)
        return nxt.timestamp() if nxt else None

    if tt in ("startup", "conditional"):
        return None

    cron_expr = str(
        trigger_spec.get("cron_expression")
        or trigger_spec.get("cron")
        or trigger_spec.get("expression")
        or ""
    ).strip()
    if cron_expr:
        try:
            trig = CronTrigger.from_crontab(cron_expr)
            nxt = trig.get_next_fire_time(None, base_dt)
            return nxt.timestamp() if nxt else None
        except Exception:
            return None

    return None


class RoutineSchedulerService:
    """Manages persistent routines, execution journals, quiet hours, and late-recovery execution."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        bus: EventBus | None = None,
        quiet_hours_fn: Any = None,
        task_runner: Any = None,
    ):
        self.db = db
        self.bus = bus
        self.quiet_hours_fn = quiet_hours_fn or (lambda: {"enabled": False, "start": "22:00", "end": "07:00"})
        self.task_runner = task_runner

    async def create_routine(
        self,
        *,
        title: str,
        trigger_type: str,
        trigger_spec: dict[str, Any],
        action_type: str = "notification",
        action_payload: dict[str, Any] | None = None,
        description: str = "",
        channel: str = "voice+ui",
        late_policy: str = "run_once_if_within_grace",
        grace_period_s: int = 3600,
        enabled: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rid = uuid.uuid4().hex
        now = time.time()
        nxt = compute_next_run(trigger_type, trigger_spec, now, allow_past_once=True)
        row = RoutineRow(
            id=rid,
            title=title.strip()[:300],
            description=description.strip()[:600],
            trigger_type=trigger_type,
            trigger_spec=trigger_spec,
            action_type=action_type,
            action_payload=action_payload or {},
            enabled=enabled,
            late_policy=late_policy,
            grace_period_s=grace_period_s,
            channel=channel,
            status="active" if enabled else "paused",
            next_run_at=nxt,
            run_count=0,
            meta=meta or {},
            created_at=now,
            updated_at=now,
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

    async def list_routines(self, *, enabled: bool | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            q = select(RoutineRow)
            if enabled is not None:
                q = q.where(RoutineRow.enabled == enabled)
            rows = (await s.execute(q.order_by(RoutineRow.created_at.desc()).limit(limit))).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def get_routine(self, routine_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            r = await s.get(RoutineRow, routine_id)
            return self._to_dict(r) if r else None

    async def toggle_routine(self, routine_id: str, enabled: bool) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            r = await s.get(RoutineRow, routine_id)
            if not r:
                return None
            r.enabled = enabled
            r.status = "active" if enabled else "paused"
            if enabled and r.next_run_at is None:
                r.next_run_at = compute_next_run(r.trigger_type, r.trigger_spec, time.time())
            r.updated_at = time.time()
            await s.commit()
            await s.refresh(r)
            return self._to_dict(r)

    async def poll_due_routines(
        self,
        *,
        now: float | None = None,
        include_startup: bool = False,
        metrics: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate and trigger due routines respecting late policies, quiet hours, and idempotency."""
        cur = now if now is not None else time.time()
        executed_runs: list[dict[str, Any]] = []

        async with self.db.async_session() as s:
            due_rows = list(
                (
                    await s.execute(
                        select(RoutineRow).where(
                            RoutineRow.enabled == True,  # noqa: E712
                            RoutineRow.status == "active",
                            RoutineRow.next_run_at != None,  # noqa: E711
                            RoutineRow.next_run_at <= cur,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if include_startup:
                startup_rows = (
                    await s.execute(
                        select(RoutineRow).where(
                            RoutineRow.enabled == True,  # noqa: E712
                            RoutineRow.status == "active",
                            RoutineRow.trigger_type == "startup",
                            RoutineRow.run_count == 0,
                        )
                    )
                ).scalars().all()
                due_rows.extend(startup_rows)

            if metrics:
                cond_rows = (
                    await s.execute(
                        select(RoutineRow).where(
                            RoutineRow.enabled == True,  # noqa: E712
                            RoutineRow.status == "active",
                            RoutineRow.trigger_type == "conditional",
                        )
                    )
                ).scalars().all()
                for cr in cond_rows:
                    spec = dict(cr.trigger_spec or {})
                    mkey = str(spec.get("metric") or "")
                    thresh = float(spec.get("threshold") or 0.0)
                    op = str(spec.get("op") or ">=")
                    cooldown = float(spec.get("cooldown_s") or 300.0)
                    if cr.last_run_at and (cur - cr.last_run_at) < cooldown:
                        continue
                    if mkey and mkey in metrics:
                        val = float(metrics[mkey])
                        hit = (
                            (op == ">=" and val >= thresh)
                            or (op == ">" and val > thresh)
                            or (op == "<=" and val <= thresh)
                            or (op == "<" and val < thresh)
                            or (op == "==" and val == thresh)
                        )
                        if hit:
                            due_rows.append(cr)

            qh_spec = self.quiet_hours_fn()
            in_qh = is_in_quiet_hours(qh_spec, datetime.fromtimestamp(cur))

            for r in due_rows:
                sched_for = float(r.next_run_at if r.next_run_at is not None else cur)
                idempotency_key = f"routine:{r.id}:{int(sched_for)}"
                delay = max(0.0, cur - sched_for)

                # Check existing run
                existing_run = (
                    await s.execute(select(RoutineRunRow).where(RoutineRunRow.idempotency_key == idempotency_key))
                ).scalar_one_or_none()
                if existing_run is not None:
                    r.next_run_at = compute_next_run(r.trigger_type, r.trigger_spec, cur)
                    continue

                # Late / expired evaluation
                if delay > r.grace_period_s and r.late_policy in ("skip", "run_once_if_within_grace"):
                    run_row = RoutineRunRow(
                        routine_id=r.id,
                        idempotency_key=idempotency_key,
                        status="skipped_late",
                        scheduled_for=sched_for,
                        started_at=cur,
                        completed_at=cur,
                        summary=f"Skipped late run (delayed by {round(delay, 1)}s > grace {r.grace_period_s}s)",
                    )
                    s.add(run_row)
                    if r.trigger_type == "once":
                        r.status = "completed"
                        r.next_run_at = None
                    else:
                        r.next_run_at = compute_next_run(r.trigger_type, r.trigger_spec, cur)
                    executed_runs.append({"routine_id": r.id, "status": "skipped_late", "reason": run_row.summary})
                    continue

                # Quiet hours check
                if in_qh and r.channel in ("voice", "voice+ui"):
                    run_row = RoutineRunRow(
                        routine_id=r.id,
                        idempotency_key=idempotency_key,
                        status="skipped_quiet_hours",
                        scheduled_for=sched_for,
                        started_at=cur,
                        completed_at=cur,
                        summary="Suppressed during quiet hours",
                    )
                    s.add(run_row)
                    r.next_run_at = compute_next_run(r.trigger_type, r.trigger_spec, cur)
                    executed_runs.append({"routine_id": r.id, "status": "skipped_quiet_hours", "reason": "Quiet hours active"})
                    continue

                # Execute action
                run_row = RoutineRunRow(
                    routine_id=r.id,
                    idempotency_key=idempotency_key,
                    status="completed",
                    scheduled_for=sched_for,
                    started_at=cur,
                    completed_at=cur,
                    summary=f"Triggered {r.action_type}: {r.title}",
                )
                r.last_run_at = cur
                r.run_count += 1
                if r.trigger_type == "once":
                    r.status = "completed"
                    r.next_run_at = None
                else:
                    r.next_run_at = compute_next_run(r.trigger_type, r.trigger_spec, cur)

                s.add(run_row)
                ev = {
                    "type": "routine_triggered",
                    "routine": self._to_dict(r),
                    "run": {
                        "id": run_row.id,
                        "status": run_row.status,
                        "summary": run_row.summary,
                    },
                }
                if self.bus:
                    self.bus.publish(ev)
                executed_runs.append(ev)

            await s.commit()
        return executed_runs

    async def run_now(self, routine_id: str) -> dict[str, Any] | None:
        """Manually trigger a routine immediately regardless of schedule."""
        cur = time.time()
        async with self.db.async_session() as s:
            r = await s.get(RoutineRow, routine_id)
            if r is None:
                return None
            idem = f"manual:{r.id}:{uuid.uuid4().hex[:8]}"
            run_row = RoutineRunRow(
                routine_id=r.id,
                idempotency_key=idem,
                status="completed",
                scheduled_for=cur,
                started_at=cur,
                completed_at=cur,
                summary=f"Manual trigger {r.action_type}: {r.title}",
            )
            r.last_run_at = cur
            r.run_count += 1
            r.updated_at = cur
            s.add(run_row)
            await s.commit()
            await s.refresh(r)
            ev = {
                "type": "routine_triggered",
                "routine": self._to_dict(r),
                "run": {"id": run_row.id, "status": run_row.status, "summary": run_row.summary},
            }
            if self.bus:
                self.bus.publish(ev)
            return ev

    async def list_runs(self, routine_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            q = select(RoutineRunRow)
            if routine_id:
                q = q.where(RoutineRunRow.routine_id == routine_id)
            rows = (await s.execute(q.order_by(RoutineRunRow.started_at.desc()).limit(limit))).scalars().all()
            return [
                {
                    "id": r.id,
                    "routine_id": r.routine_id,
                    "idempotency_key": r.idempotency_key,
                    "status": r.status,
                    "scheduled_for": r.scheduled_for,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "summary": r.summary,
                    "error": r.error,
                    "task_id": r.task_id,
                }
                for r in rows
            ]

    async def ensure_default_routines(self) -> None:
        """Seed default daily briefing and end-of-day review routines if not present."""
        existing = await self.list_routines()
        titles = {r.get("title") for r in existing}
        if "Daily Morning Briefing" not in titles:
            await self.create_routine(
                title="Daily Morning Briefing",
                description="Summarizes today's schedule, pending tasks, deadlined items, and blockers.",
                trigger_type="daily",
                trigger_spec={"hour": 8, "minute": 30},
                action_type="morning_briefing",
                action_payload={"briefing_type": "morning"},
                channel="hud",
            )
        if "End-of-Day Review" not in titles:
            await self.create_routine(
                title="End-of-Day Review",
                description="Summarizes completed work today, pending blockers, and tomorrow's priorities.",
                trigger_type="daily",
                trigger_spec={"hour": 18, "minute": 0},
                action_type="end_of_day_review",
                action_payload={"briefing_type": "end_of_day"},
                channel="hud",
            )

    async def generate_briefing(self, *, kind: str = "morning", assistant: Any = None) -> dict[str, Any]:
        """Synthesize a truthful, data-driven daily briefing or review."""
        now = datetime.now()
        date_str = now.strftime("%A, %B %d, %Y")
        in_quiet = is_in_quiet_hours(self.quiet_hours_fn(), now)

        events: list[dict] = []
        pending_tasks: list[dict] = []
        pending_approvals: list[dict] = []
        drafts: list[dict] = []

        if assistant is not None:
            cal = getattr(assistant, "calendar", None) or getattr(getattr(assistant, "orchestrator", None), "calendar", None)
            if cal and hasattr(cal, "list_events"):
                try:
                    all_ev = cal.list_events()
                    today_prefix = now.strftime("%Y-%m-%d")
                    events = [e for e in all_ev if str(e.get("start", "")).startswith(today_prefix)]
                except Exception:
                    pass

            task_svc = getattr(assistant, "tasks", None) or getattr(getattr(assistant, "orchestrator", None), "tasks", None)
            if task_svc and hasattr(task_svc, "list_tasks"):
                try:
                    all_t = await task_svc.list_tasks(limit=30)
                    if kind == "morning":
                        pending_tasks = [t for t in all_t if t.get("status") in ("running", "planning", "awaiting_approval", "awaiting_permission")]
                    else:
                        pending_tasks = [t for t in all_t if t.get("status") == "completed"]
                except Exception:
                    pass

            appr_svc = getattr(assistant, "approvals", None) or getattr(getattr(assistant, "orchestrator", None), "approvals", None)
            if appr_svc and hasattr(appr_svc, "list_pending"):
                try:
                    pending_approvals = appr_svc.list_pending()
                except Exception:
                    pass

            email_svc = getattr(assistant, "email", None) or getattr(getattr(assistant, "orchestrator", None), "email", None)
            if email_svc and hasattr(email_svc, "list_drafts"):
                try:
                    drafts = [d for d in email_svc.list_drafts() if d.get("status") == "draft"]
                except Exception:
                    pass

        lines = [f"### {kind.replace('_', ' ').title()} — {date_str}"]
        if in_quiet:
            lines.append("> [!NOTE] Quiet hours currently active.")

        if events:
            lines.append(f"\n**Calendar Events Today ({len(events)})**:")
            for e in events[:5]:
                lines.append(f"- **{e.get('title')}** at {e.get('start', '').split('T')[-1][:5]} ({e.get('duration_minutes', 30)} min)")
        else:
            lines.append("\n**Calendar**: No scheduled events today.")

        if pending_tasks:
            label = "Completed Today" if kind == "end_of_day" else "Active Tasks"
            lines.append(f"\n**{label} ({len(pending_tasks)})**:")
            for t in pending_tasks[:5]:
                lines.append(f"- Task `{t.get('task_id', '')[:8]}`: {t.get('instruction', '')[:80]} ({t.get('status')})")
        else:
            lines.append("\n**Tasks**: No active tasks.")

        if pending_approvals:
            lines.append(f"\n**Pending Approvals ({len(pending_approvals)})**:")
            for a in pending_approvals[:3]:
                lines.append(f"- Approval `{a.get('token', '')[:8]}`: {a.get('action_type', '')} on `{a.get('target_resource', '')}`")

        if drafts:
            lines.append(f"\n**Pending Outbox Drafts ({len(drafts)})**:")
            for d in drafts[:3]:
                lines.append(f"- To `{d.get('to', '')}`: {d.get('subject', '')[:50]}")

        summary_text = "\n".join(lines)
        return {
            "kind": kind,
            "date": date_str,
            "quiet_hours_active": in_quiet,
            "summary": summary_text,
            "events_count": len(events),
            "tasks_count": len(pending_tasks),
            "approvals_count": len(pending_approvals),
            "drafts_count": len(drafts),
        }

    @staticmethod
    def _to_dict(row: RoutineRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "description": row.description,
            "trigger_type": row.trigger_type,
            "trigger_spec": dict(row.trigger_spec or {}),
            "action_type": row.action_type,
            "action_payload": dict(row.action_payload or {}),
            "enabled": row.enabled,
            "late_policy": row.late_policy,
            "grace_period_s": row.grace_period_s,
            "channel": row.channel,
            "status": row.status,
            "last_run_at": row.last_run_at,
            "next_run_at": row.next_run_at,
            "run_count": row.run_count,
            "meta": dict(row.meta or {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
