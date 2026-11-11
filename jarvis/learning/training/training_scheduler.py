"""Automatic training scheduler & policy advisor (Phase 5):
tracks dataset builds and approved example thresholds without silently modifying active weights."""
import json
from pathlib import Path
import time

from ...config import LearningSpec
from ..feedback import FeedbackStore
from .dataset_builder import DatasetBuilder
from .training_audit import TrainingAuditLog


class TrainingScheduler:
    """Evaluates trigger rules (manual, dataset change, minimum examples) and records proposals."""

    def __init__(
        self,
        spec: LearningSpec | None = None,
        *,
        state_path: Path | None = None,
        audit_log: TrainingAuditLog | None = None,
    ):
        from . import TRAINING_DIR
        self.spec = spec or LearningSpec()
        self.state_path = Path(state_path) if state_path else (TRAINING_DIR / "scheduler_state.json")
        self.audit = audit_log or TrainingAuditLog(TRAINING_DIR / "audit.jsonl")

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"last_trained_sha": "", "last_checked_at": None, "pending_proposal": None}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"last_trained_sha": "", "last_checked_at": None, "pending_proposal": None}

    def _save(self, state: dict):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def evaluate_triggers(self, feedback: FeedbackStore, builder: DatasetBuilder) -> dict:
        state = self._load()
        fb_sum = feedback.summary()
        approved_count = int((fb_sum.get("by_status") or {}).get("approved") or 0)
        builds = builder.list_builds()
        latest_build = builds[0] if builds else None
        latest_sha = latest_build.get("sha256", "") if latest_build else ""
        dataset_changed = bool(latest_sha and latest_sha != state.get("last_trained_sha"))
        enough_examples = approved_count >= int(self.spec.minimum_examples)

        should_propose = bool(
            self.spec.enabled
            and (enough_examples or (latest_build and int(latest_build.get("train_count") or 0) >= self.spec.minimum_examples))
            and (dataset_changed or not state.get("last_trained_sha"))
        )

        proposal = None
        if should_propose:
            proposal = {
                "reason": f"{approved_count} approved feedback examples / build {latest_build['build_id'] if latest_build else 'pending'}",
                "requires_approval": self.spec.require_training_approval,
                "auto_run_permitted": bool(self.spec.automatic_training and not self.spec.require_training_approval),
                "proposed_at": round(time.time(), 3),
            }
            self.audit.record("training_proposed", details=proposal)

        state["last_checked_at"] = round(time.time(), 3)
        state["pending_proposal"] = proposal
        self._save(state)
        return {
            "automatic_training_enabled": self.spec.automatic_training,
            "require_training_approval": self.spec.require_training_approval,
            "minimum_examples": self.spec.minimum_examples,
            "approved_feedback_examples": approved_count,
            "enough_examples": enough_examples,
            "latest_build_id": latest_build.get("build_id") if latest_build else None,
            "dataset_changed": dataset_changed,
            "proposal": proposal,
        }

    def mark_trained(self, build_sha256: str):
        state = self._load()
        state["last_trained_sha"] = build_sha256
        state["pending_proposal"] = None
        self._save(state)
