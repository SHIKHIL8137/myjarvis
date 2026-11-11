"""Modular v2 REST API Router for the Fast Decision Engine, Laya Isolation Mode, Benchmark & Calibration."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_deps import make_assistant_dep
from .decision.benchmark import run_comparative_benchmark
from .decision.classifier import DecisionClassifier, ExistingHeuristicClassifier
from .decision.dataset import load_decision_dataset


class DecisionModeIn(BaseModel):
    enabled: bool = False
    mode: str = Field("shadow", pattern="^(disabled|shadow|laya_primary|laya_with_heuristic_fallback)$")
    checkpoint: str | None = Field(None, pattern="^(english|multilingual|typed-decisions|vegaml)$")
    abstain_enabled: bool | None = None
    confidence_threshold: float | None = Field(None, ge=0.1, le=0.99)
    cache_enabled: bool | None = None


class DecisionCorrectionIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    predicted_department: str
    predicted_intent: str
    corrected_department: str
    corrected_intent: str
    corrected_complexity: str = "standard"
    corrected_risk_tier: str = "READ"
    language: str = "en"


class DecisionBenchmarkIn(BaseModel):
    split: str = Field("test", pattern="^(train|val|test)$")
    include_laya: bool = False


class DecisionCalibrateIn(BaseModel):
    candidate_threshold: float = Field(0.70, ge=0.1, le=0.99)
    checkpoint: str = Field("multilingual", pattern="^(english|multilingual|typed-decisions|vegaml)$")
    target_mode: str = Field("shadow", pattern="^(disabled|shadow|laya_primary|laya_with_heuristic_fallback)$")


class DecisionActivateIn(BaseModel):
    policy_id: str = Field(..., min_length=2, max_length=80)
    approval_token: str | None = None
    approved: bool = False


def create_decision_subrouter(fallback=None) -> APIRouter:
    router = APIRouter()
    get_a = make_assistant_dep(fallback)

    @router.get("/decision/status")
    async def decision_status(probe: bool = False, a=Depends(get_a)):
        dec = a.orchestrator.decision
        laya_spec = a.settings.laya
        isolated_health = await dec.laya_classifier.probe_service_health() if (probe and hasattr(dec.laya_classifier, "probe_service_health")) else {}
        return {
            "enabled": bool(laya_spec.enabled),
            "mode": dec._effective_mode(),
            "laya_spec": laya_spec.model_dump(),
            "shadow": dec.shadow_summary(),
            "isolated_service": isolated_health,
            "active_policy": a.orchestrator.decision_calibration.get_active_policy(),
            "corrections_count": len(a.orchestrator.decision_calibration.list_corrections(limit=500)),
        }

    @router.post("/decision/mode")
    async def update_decision_mode(body: DecisionModeIn, a=Depends(get_a)):
        new_laya = a.settings.laya.model_copy(
            update={
                "enabled": body.enabled,
                "mode": body.mode,
                **({"checkpoint": body.checkpoint} if body.checkpoint else {}),
                **({"abstain_enabled": body.abstain_enabled} if body.abstain_enabled is not None else {}),
                **({"confidence_threshold": body.confidence_threshold} if body.confidence_threshold is not None else {}),
                **({"cache_enabled": body.cache_enabled} if body.cache_enabled is not None else {}),
            }
        )
        new_settings = a.settings.model_copy(update={"laya": new_laya})
        a.apply_settings(new_settings)
        return {"ok": True, "mode": a.orchestrator.decision._effective_mode(), "laya_spec": new_laya.model_dump()}

    @router.get("/decision/dataset")
    async def decision_dataset_info(a=Depends(get_a)):
        ds = load_decision_dataset()
        return {
            "version": ds["version"],
            "leakage_free": ds["leakage_free"],
            "counts": ds["counts"],
            "sample_test": ds["test"][:8],
            "corrections": a.orchestrator.decision_calibration.list_corrections(limit=25),
        }

    @router.post("/decision/correct")
    async def record_decision_correction(body: DecisionCorrectionIn, a=Depends(get_a)):
        try:
            rec = a.orchestrator.decision_calibration.record_correction(**body.model_dump(), actor="ui_user")
            return {"ok": True, "correction": rec}
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.post("/decision/benchmark")
    async def run_decision_benchmark(body: DecisionBenchmarkIn, a=Depends(get_a)):
        clfs: list[DecisionClassifier] = [ExistingHeuristicClassifier()]
        if body.include_laya and a.settings.laya.enabled:
            clfs.append(a.orchestrator.decision.laya_classifier)
        return await run_comparative_benchmark(clfs, split=body.split)

    @router.post("/decision/calibration/prepare")
    async def prepare_decision_calibration(body: DecisionCalibrateIn, a=Depends(get_a)):
        clf = a.orchestrator.decision.laya_classifier if a.settings.laya.enabled else ExistingHeuristicClassifier()
        return await a.orchestrator.decision_calibration.prepare_candidate_calibration(
            clf,
            candidate_threshold=body.candidate_threshold,
            checkpoint=body.checkpoint,
            target_mode=body.target_mode,
        )

    @router.post("/decision/calibration/activate")
    async def activate_decision_calibration(body: DecisionActivateIn, a=Depends(get_a)):
        try:
            pol = a.orchestrator.decision_calibration.activate_candidate(
                body.policy_id,
                approval_token=body.approval_token,
                approved=body.approved,
                actor="ui_user",
            )
            return {"ok": True, "policy": pol}
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @router.post("/decision/calibration/rollback")
    async def rollback_decision_calibration(a=Depends(get_a)):
        try:
            pol = a.orchestrator.decision_calibration.rollback_policy(actor="ui_user")
            return {"ok": True, "policy": pol}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    return router
