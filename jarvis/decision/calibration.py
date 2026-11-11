"""Decision Engine calibration, user correction logging, explicit owner approval & rollback pipeline.

Calibration & threshold selection runs ONLY on `train` + `val` (never `test`).
Activating a new threshold/policy requires an explicit owner approval token and preserves rollback snapshots.
"""
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING
import uuid

from .dataset import load_decision_dataset
from .schemas import ALLOWED_COMPLEXITIES, ALLOWED_DEPARTMENTS, ALLOWED_INTENTS, ALLOWED_RISK_TIERS

if TYPE_CHECKING:
    from ..agents.approvals import ApprovalService


class DecisionCalibrationService:
    """Manages corrections dataset, candidate policy calibration, approval gates, and rollback."""

    def __init__(self, *, calibration_dir: Path, dataset_dir: Path | None = None, approvals: "ApprovalService | None" = None):
        self.cal_dir = Path(calibration_dir)
        self.cal_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_dir = dataset_dir
        self.approvals = approvals
        self.corrections_path = self.cal_dir / "corrections.jsonl"
        self.active_path = self.cal_dir / "active_policy.json"
        self.history_path = self.cal_dir / "policy_history.jsonl"

    def record_correction(
        self,
        *,
        text: str,
        predicted_department: str,
        predicted_intent: str,
        corrected_department: str,
        corrected_intent: str,
        corrected_complexity: str = "standard",
        corrected_risk_tier: str = "READ",
        language: str = "en",
        actor: str = "owner",
    ) -> dict:
        cd = corrected_department.strip().lower()
        ci = corrected_intent.strip().lower()
        cc = corrected_complexity.strip().lower()
        cr = corrected_risk_tier.strip().upper()
        if cd not in ALLOWED_DEPARTMENTS or ci not in ALLOWED_INTENTS or cc not in ALLOWED_COMPLEXITIES or cr not in ALLOWED_RISK_TIERS:
            raise ValueError("Correction labels must strictly match allowed Jarvis decision enums.")
        entry = {
            "id": f"corr-{uuid.uuid4().hex[:10]}",
            "ts": round(time.time(), 3),
            "actor": actor,
            "text": text[:2000],
            "language": language,
            "predicted": {"department": predicted_department, "intent": predicted_intent},
            "corrected": {"department": cd, "intent": ci, "complexity": cc, "risk_tier": cr},
        }
        with self.corrections_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def list_corrections(self, limit: int = 50) -> list[dict]:
        if not self.corrections_path.exists():
            return []
        rows = [json.loads(ln) for ln in self.corrections_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return list(reversed(rows[-limit:]))

    def get_active_policy(self) -> dict:
        if self.active_path.exists():
            return json.loads(self.active_path.read_text(encoding="utf-8"))
        return {
            "policy_id": "baseline-default",
            "mode": "shadow",
            "confidence_threshold": 0.70,
            "abstain_enabled": True,
            "checkpoint": "multilingual",
            "activated_at": None,
            "activated_by": "system_default",
        }

    def list_history(self, limit: int = 20) -> list[dict]:
        if not self.history_path.exists():
            return []
        rows = [json.loads(ln) for ln in self.history_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return list(reversed(rows[-limit:]))

    async def prepare_candidate_calibration(
        self,
        classifier,
        *,
        candidate_threshold: float = 0.70,
        checkpoint: str = "multilingual",
        target_mode: str = "shadow",
    ) -> dict:
        """Fit/evaluate ONLY on train + val splits (strictly excluding test.jsonl!) and emit candidate policy."""
        ds = load_decision_dataset(self.dataset_dir)
        train_val = ds["train"] + ds["val"]
        correct_dept = 0
        correct_intent = 0
        abstained = 0
        for row in train_val:
            res = await classifier.classify(row["text"])
            env = res.envelope
            if env.department == row["department"]:
                correct_dept += 1
            if env.intent == row["intent"]:
                correct_intent += 1
            if env.abstained:
                abstained += 1
        n = max(1, len(train_val))
        candidate = {
            "policy_id": f"cal-{uuid.uuid4().hex[:8]}",
            "created_at": round(time.time(), 3),
            "dataset_version": ds["version"],
            "fit_split_counts": {"train": len(ds["train"]), "val": len(ds["val"]), "test_held_out": len(ds["test"])},
            "val_metrics": {
                "samples": len(train_val),
                "department_accuracy": round(correct_dept / n, 4),
                "intent_accuracy": round(correct_intent / n, 4),
                "abstain_rate": round(abstained / n, 4),
            },
            "confidence_threshold": round(float(candidate_threshold), 3),
            "checkpoint": checkpoint,
            "mode": target_mode,
        }
        cand_path = self.cal_dir / f"{candidate['policy_id']}.candidate.json"
        cand_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")

        approval = None
        if self.approvals is not None:
            approval = self.approvals.create_request(
                action_type="promote_model",
                target_resource=f"decision_policy:{candidate['policy_id']}",
                preview_diff=(
                    f"Activate Decision Policy {candidate['policy_id']}\n"
                    f"Mode: {target_mode} | Checkpoint: {checkpoint} | Threshold: {candidate_threshold}\n"
                    f"Train+Val samples={len(train_val)}, Intent Acc={candidate['val_metrics']['intent_accuracy']}"
                ),
                args={"policy_id": candidate["policy_id"], "candidate_path": str(cand_path)},
            )
        return {"candidate": candidate, "approval": approval}

    def activate_candidate(self, policy_id: str, *, approval_token: str | None = None, approved: bool = False, actor: str = "owner") -> dict:
        cand_path = self.cal_dir / f"{policy_id}.candidate.json"
        if not cand_path.exists():
            raise KeyError(f"Candidate decision policy {policy_id!r} not found.")
        if self.approvals is not None and approval_token:
            req = self.approvals.consume_approved(approval_token, expected_action_type="promote_model")
            actor = str(req.get("decided_by") or actor)
        elif not approved:
            raise PermissionError("Activating calibrated decision policy requires explicit owner approval.")

        prev = self.get_active_policy()
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(prev) + "\n")

        cand = json.loads(cand_path.read_text(encoding="utf-8"))
        cand["activated_at"] = round(time.time(), 3)
        cand["activated_by"] = actor
        cand["previous_policy_id"] = prev.get("policy_id")
        self.active_path.write_text(json.dumps(cand, indent=2), encoding="utf-8")
        return cand

    def rollback_policy(self, *, actor: str = "owner") -> dict:
        hist = self.list_history(limit=20)
        if not hist:
            raise ValueError("No prior decision policy snapshot available for rollback.")
        prev = hist[0]
        cur = self.get_active_policy()
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**cur, "rolled_back_at": round(time.time(), 3), "rolled_back_by": actor}) + "\n")
        self.active_path.write_text(json.dumps(prev, indent=2), encoding="utf-8")
        return prev
