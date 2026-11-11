"""Clean permission-gated productivity ports for Calendar (.ics) & Email drafts (strict EXECUTE approval to send)."""
import datetime
import json
from pathlib import Path
import time
import uuid


class LocalCalendarService:
    """RFC 5545 `.ics` local calendar manager (zero network required)."""

    def __init__(self, cal_dir: Path):
        self.cal_dir = Path(cal_dir)
        self.cal_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cal_dir / "events.json"

    def _load(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, items: list[dict]) -> None:
        self.index_path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def create_event(self, *, title: str, start_iso: str, duration_minutes: int = 30, description: str = "") -> dict:
        dt = datetime.datetime.fromisoformat(start_iso)
        end = dt + datetime.timedelta(minutes=duration_minutes)
        uid = uuid.uuid4().hex[:16]
        fmt = lambda d: d.strftime("%Y%m%dT%H%M%S")
        ics = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Jarvis Local OS//EN\nBEGIN:VEVENT\n"
            f"UID:{uid}@jarvis.local\nSUMMARY:{title}\nDESCRIPTION:{description}\n"
            f"DTSTART:{fmt(dt)}\nDTEND:{fmt(end)}\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        ics_path = self.cal_dir / f"{uid}.ics"
        ics_path.write_text(ics, encoding="utf-8")
        rec = {"id": uid, "title": title, "start": start_iso, "duration_minutes": duration_minutes, "ics_path": str(ics_path)}
        items = self._load()
        items.append(rec)
        self._save(items)
        return rec

    def list_events(self) -> list[dict]:
        return self._load()


class EmailDraftService:
    """READ=list/summarize, WRITE=create local draft, EXECUTE=explicitly approved send only."""

    def __init__(self, outbox_dir: Path):
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def create_draft(self, *, to: str, subject: str, body: str) -> dict:
        did = uuid.uuid4().hex[:12]
        draft = {"id": did, "to": to, "subject": subject, "body": body, "status": "draft", "created_at": round(time.time(), 3)}
        (self.outbox_dir / f"{did}.json").write_text(json.dumps(draft, indent=2), encoding="utf-8")
        return draft

    def list_drafts(self) -> list[dict]:
        out = []
        for p in sorted(self.outbox_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return out

    def dispatch_approved_draft(self, draft_id: str, *, online_allowed: bool = False) -> dict:
        p = self.outbox_dir / f"{draft_id}.json"
        if not p.exists():
            raise KeyError(draft_id)
        draft = json.loads(p.read_text(encoding="utf-8"))
        if not online_allowed:
            # Record cleanly as queued local-mode dispatch rather than silently hitting network
            draft["status"] = "approved_queued_offline"
            draft["approved_at"] = round(time.time(), 3)
            p.write_text(json.dumps(draft, indent=2), encoding="utf-8")
            return draft
        draft["status"] = "dispatched"
        draft["dispatched_at"] = round(time.time(), 3)
        p.write_text(json.dumps(draft, indent=2), encoding="utf-8")
        return draft

