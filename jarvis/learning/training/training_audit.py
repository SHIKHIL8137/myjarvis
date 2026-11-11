"""Audit logger for training, dataset, evaluation, and model lifecycle events."""
import json
from pathlib import Path
import threading
import time

from ...config import DATA, ROOT

DEFAULT_AUDIT_PATHS = (
    DATA / "learning" / "training" / "audit.jsonl",
    ROOT / "logs" / "training_audit.jsonl",
)


class TrainingAuditLog:
    """Append-only structured audit log for dataset and model training lifecycles."""

    def __init__(self, primary_path: Path | None = None, mirror_path: Path | None = None):
        self.paths: list[Path] = []
        if primary_path is not None:
            self.paths.append(Path(primary_path))
            if mirror_path is not None:
                self.paths.append(Path(mirror_path))
        else:
            self.paths.extend(DEFAULT_AUDIT_PATHS)
        self._lock = threading.RLock()

    def record(self, event: str, *, actor: str = "system", details: dict | None = None) -> dict:
        entry = {
            "t": round(time.time(), 3),
            "event": str(event),
            "actor": str(actor),
            "details": details or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            for p in self.paths:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        return entry

    def recent(self, limit: int = 50) -> list[dict]:
        path = self.paths[0]
        if not path.exists():
            return []
        rows: list[dict] = []
        with self._lock:
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
        return list(reversed(rows[-max(1, limit):]))

