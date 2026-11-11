"""Central versioned model & adapter registry (Phase 7):
tracks baseline + candidate versions, evaluation history, hashes, active pointer, and rollback eligibility."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import threading
import time
import uuid

from ...config import LearningSpec
from .adapter_manager import check_adapter_compatibility
from .training_audit import TrainingAuditLog


@dataclass
class ModelVersionRecord:
    version_id: str
    base_model: str
    artifact_type: str = "ollama_tag"  # "ollama_tag" | "gguf_adapter" | "peft_lora"
    ollama_tag: str = ""
    adapter_path: str = ""
    quantization: str = "Q4_K_M"
    dataset_build_id: str = ""
    sha256: str = ""
    status: str = "candidate"  # "baseline" | "candidate" | "promoted" | "rejected" | "rolled_back"
    evaluation_id: str = ""
    evaluation_score: float | None = None
    evaluation_passed: bool = False
    notes: str = ""
    created_at: float = field(default_factory=lambda: round(time.time(), 3))

    def to_dict(self) -> dict:
        return asdict(self)


class ModelRegistry:
    """Thread-safe atomic model version registry with evaluation & compatibility enforcement."""

    def __init__(
        self,
        spec: LearningSpec | None = None,
        *,
        registry_path: Path | None = None,
        audit_log: TrainingAuditLog | None = None,
    ):
        from . import REGISTRY_PATH, TRAINING_DIR
        self.spec = spec or LearningSpec()
        self.path = Path(registry_path) if registry_path else REGISTRY_PATH
        self.audit = audit_log or TrainingAuditLog(TRAINING_DIR / "audit.jsonl")
        self._lock = threading.RLock()
        self._state: dict = {
            "active_version_id": "",
            "baseline_version_id": "",
            "previous_version_id": "",
            "versions": {},
        }
        self._load()

    def _load(self):
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "versions" in data:
                    self._state.update(data)
            except Exception:
                pass

    def _save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def ensure_baseline(self, base_model: str) -> dict:
        with self._lock:
            if self._state["baseline_version_id"] and self._state["baseline_version_id"] in self._state["versions"]:
                return self._state["versions"][self._state["baseline_version_id"]]
            vid = "baseline-v1"
            rec = ModelVersionRecord(
                version_id=vid,
                base_model=base_model,
                artifact_type="ollama_tag",
                ollama_tag=base_model,
                status="baseline",
                evaluation_passed=True,
                notes="Configured unmodified local baseline model",
            ).to_dict()
            self._state["versions"][vid] = rec
            self._state["baseline_version_id"] = vid
            if not self._state["active_version_id"]:
                self._state["active_version_id"] = vid
            self._save()
            return rec

    def register_candidate(
        self,
        *,
        base_model: str,
        artifact_type: str = "ollama_tag",
        ollama_tag: str = "",
        adapter_path: str = "",
        quantization: str = "Q4_K_M",
        dataset_build_id: str = "",
        sha256: str = "",
        notes: str = "",
    ) -> dict:
        vid = f"ver-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:5]}"
        rec = ModelVersionRecord(
            version_id=vid,
            base_model=base_model,
            artifact_type=artifact_type,
            ollama_tag=ollama_tag or base_model,
            adapter_path=adapter_path,
            quantization=quantization,
            dataset_build_id=dataset_build_id,
            sha256=sha256,
            status="candidate",
            notes=notes,
        ).to_dict()
        with self._lock:
            self._state["versions"][vid] = rec
            self._save()
        self.audit.record("candidate_registered", details={"version_id": vid, "artifact_type": artifact_type, "base_model": base_model})
        return rec

    def attach_evaluation(self, version_id: str, *, evaluation_id: str, score: float, passed: bool, reason: str = "") -> dict:
        with self._lock:
            rec = self._state["versions"].get(version_id)
            if not rec:
                raise KeyError(f"Model version '{version_id}' not found.")
            rec["evaluation_id"] = evaluation_id
            rec["evaluation_score"] = round(float(score), 4)
            rec["evaluation_passed"] = bool(passed)
            if not passed:
                rec["status"] = "rejected"
                if reason:
                    rec["notes"] = reason
            self._save()
        return rec

    def promote(
        self,
        version_id: str,
        *,
        actor: str = "user",
        installed_ollama_models: list[str] | None = None,
    ) -> dict:
        with self._lock:
            rec = self._state["versions"].get(version_id)
            if not rec:
                raise KeyError(f"Model version '{version_id}' not found.")
            if actor == "auto" and not self.spec.auto_promote:
                raise PermissionError("Automatic unattended model promotion is disabled by learning policy (auto_promote=false).")
            if self.spec.evaluation_required and not rec.get("evaluation_passed"):
                raise PermissionError(
                    f"Cannot promote version '{version_id}': evaluation is required and has not passed."
                )
            compat = check_adapter_compatibility(
                artifact_type=rec.get("artifact_type", "ollama_tag"),
                base_model=rec.get("base_model", ""),
                adapter_path=rec.get("adapter_path", ""),
                ollama_tag=rec.get("ollama_tag", ""),
                installed_ollama_models=installed_ollama_models,
            )
            if not compat["compatible_with_ollama"]:
                raise ValueError("; ".join(compat["reasons"]))

            prev = self._state.get("active_version_id") or self._state.get("baseline_version_id")
            if prev and prev != version_id:
                self._state["previous_version_id"] = prev
            self._state["active_version_id"] = version_id
            rec["status"] = "promoted"
            self._save()
        self.audit.record("model_promoted", actor=actor, details={"version_id": version_id, "previous": prev})
        return rec

    def reject(self, version_id: str, *, reason: str = "Rejected after evaluation", actor: str = "system") -> dict:
        with self._lock:
            rec = self._state["versions"].get(version_id)
            if not rec:
                raise KeyError(f"Model version '{version_id}' not found.")
            rec["status"] = "rejected"
            rec["notes"] = reason
            self._save()
        self.audit.record("model_rejected", actor=actor, details={"version_id": version_id, "reason": reason})
        return rec

    def summary(self) -> dict:
        with self._lock:
            versions = sorted(self._state["versions"].values(), key=lambda v: v.get("created_at", 0), reverse=True)
            active_id = self._state.get("active_version_id", "")
            return {
                "active_version_id": active_id,
                "active": self._state["versions"].get(active_id),
                "baseline_version_id": self._state.get("baseline_version_id", ""),
                "previous_version_id": self._state.get("previous_version_id", ""),
                "versions": versions,
            }

