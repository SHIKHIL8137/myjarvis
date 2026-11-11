"""Persistent local-first reminder & alarm engine backed by SQLAlchemy + EventBus notifications."""
import time
import uuid

from ...core.event_bus import EventBus
from ...db.engine import DatabaseManager
from ...db.models import ReminderRow


class ReminderService:
    def __init__(self, db: DatabaseManager, bus: EventBus | None = None):
        self.db = db
        self.bus = bus

    def create(self, *, title: str, due_at: float, notes: str = "", channel: str = "voice+ui", recurrence_cron: str | None = None) -> dict:
        rid = uuid.uuid4().hex
        with self.db.sync_session() as s:
            row = ReminderRow(
                id=rid,
                title=title.strip()[:300],
                notes=notes,
                due_at=float(due_at),
                channel=channel,
                recurrence_cron=recurrence_cron,
                status="scheduled",
            )
            s.add(row)
            s.commit()
            return self._to_dict(row)

    def list_reminders(self, status: str | None = None) -> list[dict]:
        with self.db.sync_session() as s:
            q = s.query(ReminderRow)
            if status:
                q = q.filter(ReminderRow.status == status)
            return [self._to_dict(r) for r in q.order_by(ReminderRow.due_at.asc()).all()]

    def complete(self, reminder_id: str) -> dict:
        with self.db.sync_session() as s:
            row = s.get(ReminderRow, reminder_id)
            if not row:
                raise KeyError(reminder_id)
            row.status = "completed"
            s.commit()
            return self._to_dict(row)

    def poll_due(self, now: float | None = None) -> list[dict]:
        ts = now if now is not None else time.time()
        fired: list[dict] = []
        with self.db.sync_session() as s:
            rows = s.query(ReminderRow).filter(ReminderRow.status == "scheduled", ReminderRow.due_at <= ts).all()
            for row in rows:
                row.status = "triggered"
                row.triggered_at = round(ts, 3)
                d = self._to_dict(row)
                fired.append(d)
                if self.bus:
                    self.bus.publish({"type": "reminder_due", "reminder": d})
            if rows:
                s.commit()
        return fired

    @staticmethod
    def _to_dict(r: ReminderRow) -> dict:
        return {
            "id": r.id,
            "title": r.title,
            "notes": r.notes,
            "due_at": r.due_at,
            "recurrence_cron": r.recurrence_cron,
            "channel": r.channel,
            "status": r.status,
            "triggered_at": r.triggered_at,
        }

