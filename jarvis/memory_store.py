"""Complete multi-category personal memory store with duplicate detection, credential scrubbing,
inferred-memory confirmation, backup/recovery, export, and immediate indexing."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .config import DATA, MemorySpec
from .retrieval import tokens

MEMORY_CATEGORIES = ("explicit", "preference", "project", "temporary", "feedback")

_SECRET_PATTERNS = [
    (re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"), "OpenAI-style API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "Anthropic API key"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "Hugging Face token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "GitHub token"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), "Google API key"),
    (re.compile(r"\btvly-[A-Za-z0-9\-_]{12,}\b"), "Tavily API key"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"), "Private key block"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", re.I), "Bearer token"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd|api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{6,}"), "Credential/password assignment"),
]


def detect_secret(text: str) -> str | None:
    """Return description if `text` appears to contain credentials/tokens/passwords, else None."""
    s = text or ""
    for pat, label in _SECRET_PATTERNS:
        if pat.search(s):
            return label
    return None


@dataclass
class MemoryRecord:
    id: str
    category: str  # "explicit" | "preference" | "project" | "temporary" | "feedback"
    text: str
    topic: str = ""
    title: str = ""
    source: str = "explicit"  # "explicit" | "inferred" | "user_approved"
    confirmed: bool = True
    confidence: float = 1.0
    note_path: str | None = None
    created_at: float = field(default_factory=lambda: round(time.time(), 3))
    updated_at: float = field(default_factory=lambda: round(time.time(), 3))

    def to_dict(self) -> dict:
        return asdict(self)


def infer_memory_category(text: str, explicit_category: str | None = None) -> tuple[str, str]:
    """Infer structured category ('preference', 'project', 'explicit', 'temporary') + short topic."""
    if explicit_category and explicit_category in MEMORY_CATEGORIES:
        cat = explicit_category
    else:
        low = text.lower()
        if re.search(r"\b(?:i prefer|my preference|i like using|i usually use|always use|favorite|prefer\s+\w+)\b", low):
            cat = "preference"
        elif re.search(r"\b(?:project|erp|backend|frontend|module|repo|repository|architecture|my app|jarvis)\b", low):
            cat = "project"
        elif re.search(r"\b(?:for now|temporarily|this task|right now|until)\b", low):
            cat = "temporary"
        else:
            cat = "explicit"
    toks = tokens(text)
    topic = " ".join(toks[:3]) if toks else ""
    return cat, topic


class PersonalMemoryStore:
    """Thread-safe persistent personal memory repository supporting all structured categories,
    secret protection, deduplication, export, and atomic backup/restore."""

    def __init__(self, path: Path | None = None, spec: MemorySpec | None = None):
        self.spec = spec or MemorySpec()
        self.path = Path(path) if path else (DATA / "memory" / "records.jsonl")
        self.backup_dir = Path(self.spec.backup_dir) if Path(self.spec.backup_dir).is_absolute() else (self.path.parent / "backups")
        self._lock = threading.RLock()
        self.records: list[MemoryRecord] = []
        self._load()

    def _load(self):
        with self._lock:
            self.records.clear()
            if not self.path.exists():
                return
            for ln in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                    self.records.append(MemoryRecord(**{k: d[k] for k in MemoryRecord.__dataclass_fields__ if k in d}))
                except Exception:
                    pass

    def _save_all(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in self.records)
            tmp.write_text((payload + "\n") if payload else "", encoding="utf-8")
            os.replace(tmp, self.path)

    def _similarity(self, a: str, b: str) -> float:
        ta, tb = set(tokens(a)), set(tokens(b))
        if not ta or not tb:
            return 1.0 if a.strip().lower() == b.strip().lower() else 0.0
        return len(ta & tb) / len(ta | tb)

    def find_duplicate(self, text: str, category: str | None = None) -> MemoryRecord | None:
        with self._lock:
            for r in self.records:
                if category and r.category != category:
                    continue
                if r.text.strip().lower() == text.strip().lower():
                    return r
                if self._similarity(r.text, text) >= self.spec.duplicate_threshold:
                    return r
            return None

    def add(
        self,
        text: str,
        category: str | None = None,
        topic: str = "",
        title: str = "",
        source: str = "explicit",
        confirmed: bool = True,
        confidence: float = 1.0,
        note_path: str | None = None,
        allow_duplicate: bool = False,
    ) -> tuple[MemoryRecord, bool]:
        """Store a personal memory record. Raises ValueError if empty or containing secrets.
        Returns `(record, created)` where `created=False` means an existing near-duplicate matched."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Memory text must not be empty")
        secret_hit = detect_secret(cleaned)
        if secret_hit:
            raise ValueError(f"Refused to store memory containing sensitive credential ({secret_hit}).")

        inferred_cat, inferred_topic = infer_memory_category(cleaned, category)
        cat = inferred_cat if inferred_cat in MEMORY_CATEGORIES else "explicit"
        final_topic = topic.strip() or inferred_topic
        final_title = title.strip() or " ".join(cleaned.split()[:7]).strip(" .,:;!?")

        with self._lock:
            if not allow_duplicate:
                dup = self.find_duplicate(cleaned, category=cat)
                if dup is not None:
                    if note_path and not dup.note_path:
                        dup.note_path = note_path
                        dup.updated_at = round(time.time(), 3)
                        self._save_all()
                    return dup, False

            rec = MemoryRecord(
                id=uuid.uuid4().hex[:12],
                category=cat,
                text=cleaned,
                topic=final_topic,
                title=final_title,
                source=source,
                confirmed=bool(confirmed),
                confidence=float(confidence),
                note_path=note_path,
            )
            self.records.append(rec)
            # Enforce temporary item cap
            if cat == "temporary":
                temps = [r for r in self.records if r.category == "temporary"]
                if len(temps) > self.spec.max_temporary_items:
                    drop_id = temps[0].id
                    self.records = [r for r in self.records if r.id != drop_id]
            self._save_all()
            return rec, True

    def propose_inferred(self, text: str, category: str | None = None, confidence: float = 0.75) -> MemoryRecord:
        """Queue a candidate inferred memory needing confirmation before entering active recall."""
        if confidence < 0.65:
            raise ValueError("Inferred memory confidence below minimum threshold (0.65)")
        rec, _ = self.add(
            text,
            category=category,
            source="inferred",
            confirmed=not self.spec.confirm_inferred,
            confidence=confidence,
        )
        return rec

    def confirm_pending(self, rec_id: str, approved: bool = True) -> MemoryRecord | None:
        with self._lock:
            for r in list(self.records):
                if r.id == rec_id:
                    if approved:
                        r.confirmed = True
                        r.source = "user_approved"
                        r.updated_at = round(time.time(), 3)
                        self._save_all()
                        return r
                    self.records = [x for x in self.records if x.id != rec_id]
                    self._save_all()
                    return None
        raise KeyError(rec_id)

    def update(self, rec_id: str, new_text: str) -> MemoryRecord:
        cleaned = (new_text or "").strip()
        if not cleaned:
            raise ValueError("Updated memory text must not be empty")
        secret_hit = detect_secret(cleaned)
        if secret_hit:
            raise ValueError(f"Refused to store memory containing sensitive credential ({secret_hit}).")
        with self._lock:
            for r in self.records:
                if r.id == rec_id:
                    r.text = cleaned
                    r.updated_at = round(time.time(), 3)
                    self._save_all()
                    return r
        raise KeyError(rec_id)

    def delete(self, rec_id: str) -> MemoryRecord:
        with self._lock:
            for i, r in enumerate(self.records):
                if r.id == rec_id:
                    removed = self.records.pop(i)
                    self._save_all()
                    return removed
        raise KeyError(rec_id)

    def clear_temporary(self) -> int:
        with self._lock:
            before = len(self.records)
            self.records = [r for r in self.records if r.category != "temporary"]
            removed = before - len(self.records)
            if removed:
                self._save_all()
            return removed

    def list_all(self, category: str | None = None, include_pending: bool = False) -> list[MemoryRecord]:
        with self._lock:
            out = []
            for r in self.records:
                if not include_pending and not r.confirmed:
                    continue
                if category and r.category != category:
                    continue
                out.append(r)
            return list(reversed(out))

    def search(self, query: str, category: str | None = None, limit: int = 6) -> list[tuple[MemoryRecord, float]]:
        q = set(tokens(query))
        if not q:
            return []
        with self._lock:
            scored: list[tuple[MemoryRecord, float]] = []
            for r in self.records:
                if not r.confirmed:
                    continue
                if category and r.category != category:
                    continue
                doc = set(tokens(f"{r.title} {r.topic} {r.text}"))
                title_toks = set(tokens(f"{r.title} {r.topic}"))
                overlap = len(q & doc)
                if not overlap:
                    continue
                score = float(overlap) + 1.5 * len(q & title_toks)
                if r.category in ("preference", "project"):
                    score *= 1.15
                scored.append((r, round(score, 3)))
            scored.sort(key=lambda x: (-x[1], -x[0].updated_at))
            return scored[: max(1, limit)]

    def export_data(self) -> dict:
        with self._lock:
            by_cat: dict[str, int] = {}
            for r in self.records:
                by_cat[r.category] = by_cat.get(r.category, 0) + 1
            return {
                "exported_at": round(time.time(), 3),
                "total": len(self.records),
                "by_category": by_cat,
                "records": [r.to_dict() for r in self.records],
            }

    def backup(self) -> Path:
        with self._lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.backup_dir / f"memory-{stamp}.json"
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.export_data(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, dest)
            return dest

    def restore(self, backup_file: Path) -> int:
        p = Path(backup_file)
        data = json.loads(p.read_text(encoding="utf-8"))
        raw_recs = data.get("records") if isinstance(data, dict) else data
        if not isinstance(raw_recs, list):
            raise ValueError("Invalid backup format")
        restored: list[MemoryRecord] = []
        for item in raw_recs:
            txt = str(item.get("text", "")).strip()
            if not txt or detect_secret(txt):
                continue
            restored.append(MemoryRecord(**{k: item[k] for k in MemoryRecord.__dataclass_fields__ if k in item}))
        with self._lock:
            self.records = restored
            self._save_all()
        return len(self.records)

