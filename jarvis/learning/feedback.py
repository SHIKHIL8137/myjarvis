"""Feedback-based learning engine (Phase 4): captures corrections, preferences, and workflow lessons
without automatically treating arbitrary chat messages or unverified hallucinations as training examples."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from ..config import DATA
from ..memory_store import detect_secret
from ..retrieval import tokens

FEEDBACK_CATEGORIES = (
    "positive", "negative", "correction", "style", "pattern", "task_success", "task_failure", "workflow",
)


@dataclass
class FeedbackRecord:
    id: str
    input_text: str
    previous_response: str
    corrected_response: str
    category: str
    context: str = ""
    topic: str = ""
    timestamp: float = field(default_factory=lambda: round(time.time(), 3))
    source: str = "explicit_user"  # "explicit_user" | "confirmed_correction" | "task_outcome"
    approval_status: str = "pending"  # "pending" | "approved" | "rejected"
    quality_score: float = 0.8
    privacy_classification: str = "private_safe"

    def to_dict(self) -> dict:
        return asdict(self)


def is_correction_statement(text: str) -> bool:
    """Detect user statements correcting Jarvis's last reply."""
    low = (text or "").strip().lower()
    return bool(re.search(
        r"\b(?:that(?:'s| is)\s+(?:wrong|incorrect)|actually[,\s]+(?:use|it(?:'s| is)|the correct)|"
        r"no[,\s]+the\s+correct|instead\s+of\s+that|correct\s+implementation\s+uses|remember\s+this\s+correction)\b",
        low,
    ))


class FeedbackStore:
    """Persistent store for curated user feedback and approved behavioral lessons."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (DATA / "learning" / "feedback" / "records.jsonl")
        self._lock = threading.RLock()
        self.records: list[FeedbackRecord] = []
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
                    self.records.append(FeedbackRecord(**{k: d[k] for k in FeedbackRecord.__dataclass_fields__ if k in d}))
                except Exception:
                    pass

    def _save_all(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in self.records)
            tmp.write_text((payload + "\n") if payload else "", encoding="utf-8")
            os.replace(tmp, self.path)

    def record(
        self,
        *,
        input_text: str,
        previous_response: str = "",
        corrected_response: str = "",
        category: str = "correction",
        context: str = "",
        topic: str = "",
        source: str = "explicit_user",
        approved: bool = False,
        quality_score: float = 0.85,
    ) -> FeedbackRecord:
        cat = category if category in FEEDBACK_CATEGORIES else "correction"
        for field_val in (input_text, previous_response, corrected_response, context):
            sec = detect_secret(field_val)
            if sec:
                raise ValueError(f"Refused feedback entry containing sensitive credential ({sec}).")
        toks = tokens(f"{input_text} {corrected_response} {topic}")
        final_topic = topic.strip() or (" ".join(toks[:3]) if toks else "general")
        rec = FeedbackRecord(
            id=uuid.uuid4().hex[:12],
            input_text=(input_text or "").strip(),
            previous_response=(previous_response or "").strip(),
            corrected_response=(corrected_response or "").strip(),
            category=cat,
            context=(context or "").strip()[:500],
            topic=final_topic,
            source=source,
            approval_status="approved" if approved else "pending",
            quality_score=max(0.0, min(1.0, float(quality_score))),
        )
        with self._lock:
            self.records.append(rec)
            self._save_all()
        return rec

    def approve(self, feedback_id: str, quality_score: float | None = None) -> FeedbackRecord:
        with self._lock:
            for r in self.records:
                if r.id == feedback_id:
                    r.approval_status = "approved"
                    if quality_score is not None:
                        r.quality_score = max(0.0, min(1.0, float(quality_score)))
                    self._save_all()
                    return r
        raise KeyError(feedback_id)

    def reject(self, feedback_id: str) -> FeedbackRecord:
        with self._lock:
            for r in self.records:
                if r.id == feedback_id:
                    r.approval_status = "rejected"
                    self._save_all()
                    return r
        raise KeyError(feedback_id)

    def delete(self, feedback_id: str) -> bool:
        with self._lock:
            before = len(self.records)
            self.records = [r for r in self.records if r.id != feedback_id]
            if len(self.records) != before:
                self._save_all()
                return True
        return False

    def list_records(self, status: str | None = None, category: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            out = []
            for r in reversed(self.records):
                if status and r.approval_status != status:
                    continue
                if category and r.category != category:
                    continue
                out.append(r.to_dict())
                if len(out) >= max(1, limit):
                    break
            return out

    def retrieve_lessons(self, query: str, limit: int = 3) -> list[dict]:
        """Retrieve only APPROVED high-signal corrections/preferences matching the current request."""
        q = set(tokens(query))
        if not q:
            return []
        with self._lock:
            scored = []
            for r in self.records:
                if r.approval_status != "approved" or r.category in ("negative", "task_failure"):
                    continue
                doc = set(tokens(f"{r.topic} {r.input_text} {r.corrected_response}"))
                overlap = len(q & doc)
                if not overlap:
                    continue
                score = overlap * (0.5 + r.quality_score)
                scored.append((score, r.to_dict()))
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[: max(1, limit)]]

    def export_training_examples(self, min_quality: float = 0.7) -> list[dict]:
        """Export strictly APPROVED positive/correction examples formatted cleanly for dataset curation."""
        with self._lock:
            examples = []
            for r in self.records:
                if r.approval_status != "approved" or r.category in ("negative", "task_failure") or r.quality_score < min_quality:
                    continue
                if not r.input_text or not r.corrected_response:
                    continue
                if detect_secret(r.input_text) or detect_secret(r.corrected_response):
                    continue
                examples.append({
                    "id": r.id,
                    "instruction": r.input_text,
                    "input": r.context,
                    "output": r.corrected_response,
                    "category": r.category,
                    "topic": r.topic,
                    "quality_score": r.quality_score,
                })
            return examples

    def summary(self) -> dict:
        with self._lock:
            by_status = {"pending": 0, "approved": 0, "rejected": 0}
            by_cat: dict[str, int] = {}
            for r in self.records:
                by_status[r.approval_status] = by_status.get(r.approval_status, 0) + 1
                by_cat[r.category] = by_cat.get(r.category, 0) + 1
            return {"total": len(self.records), "by_status": by_status, "by_category": by_cat}
