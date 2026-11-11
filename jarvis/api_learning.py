"""API router for Automatic Knowledge Updates, Feedback Learning, Datasets, Training Jobs,
Model Evaluation, and Model Registry / Rollback (Phases 3–8)."""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_deps import make_assistant_dep
from .learning.knowledge_pipeline import KnowledgeSourceSpec
from .learning.training.training_config import TrainingJobConfig
from .search import SearchOfflineError


class KnowledgeSourceIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=150)
    kind: str = Field(..., pattern="^(local_markdown|local_repo|user_doc|public_docs|huggingface_dataset|huggingface_model|configured_api)$")
    location: str = Field(..., min_length=1, max_length=600)
    topic: str = "general"
    license: str = "unknown"
    trust_classification: str = "user_provided"
    enabled: bool = True


class KnowledgeTextIn(BaseModel):
    source_id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=150)
    text: str = Field(..., min_length=1, max_length=500_000)
    topic: str = "general"
    license: str = "user-provided"
    version: str = "1"


class KnowledgeSyncIn(BaseModel):
    topic: str | None = None
    source_id: str | None = None
    force: bool = False


class FeedbackCreateIn(BaseModel):
    input_text: str = Field(..., min_length=1, max_length=4000)
    previous_response: str = Field("", max_length=4000)
    corrected_response: str = Field("", max_length=4000)
    category: str = "correction"
    context: str = ""
    topic: str = ""
    approved: bool = False
    quality_score: float = Field(0.85, ge=0.0, le=1.0)


class DatasetImportLocalIn(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=600)
    license: str = "mit"
    name: str = ""
    expected_sha256: str = ""


class DatasetDownloadIn(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=8, max_length=800)
    license: str = "unknown"
    name: str = ""
    expected_sha256: str = ""


class DatasetProcessIn(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=100)
    version: int | None = None
    target_format: str = Field("messages", pattern="^(messages|alpaca)$")
    min_quality: float = Field(0.55, ge=0.0, le=1.0)


class TrainingJobCreateIn(BaseModel):
    dataset_build_id: str = Field(..., min_length=1, max_length=120)
    base_model: str | None = None
    hf_model_id: str = "Qwen/Qwen2.5-0.5B-Instruct"
    quantization: str = Field("4bit", pattern="^(4bit|8bit|fp16)$")
    epochs: int = Field(1, ge=1, le=20)
    max_seq_length: int = Field(512, ge=128, le=4096)


class TrainingStartIn(BaseModel):
    override: bool = False
    override_reason: str = ""
    actor: str = "ui_user"


class CandidateRegisterIn(BaseModel):
    base_model: str = Field(..., min_length=1, max_length=120)
    artifact_type: str = Field("ollama_tag", pattern="^(ollama_tag|gguf_adapter|peft_lora)$")
    ollama_tag: str = ""
    adapter_path: str = ""
    quantization: str = "Q4_K_M"
    dataset_build_id: str = ""
    sha256: str = ""
    notes: str = ""


class EvaluateIn(BaseModel):
    candidate_version_id: str | None = None


class RollbackIn(BaseModel):
    target_version_id: str | None = None


def create_learning_router(fallback=None) -> APIRouter:
    router = APIRouter()
    get_a = make_assistant_dep(fallback)

    @router.get("/api/learning/overview")
    async def overview(a=Depends(get_a)):
        return a.learning_overview()

    # ---- Knowledge Updates (Phase 3) -----------------------------------------------------------------
    @router.get("/api/learning/knowledge/sources")
    async def list_knowledge_sources(a=Depends(get_a)):
        return {"sources": a.knowledge.list_sources(), "freshness": a.knowledge.freshness()}

    @router.post("/api/learning/knowledge/sources")
    async def add_knowledge_source(body: KnowledgeSourceIn, a=Depends(get_a)):
        src = KnowledgeSourceSpec(**body.model_dump())
        a.knowledge.register_source(src)
        return {"ok": True, "source": src.to_dict()}

    @router.post("/api/learning/knowledge/ingest")
    async def ingest_knowledge_text(body: KnowledgeTextIn, a=Depends(get_a)):
        res = a.knowledge.ingest_text(
            body.source_id,
            body.name,
            body.text,
            topic=body.topic,
            license_name=body.license,
            doc_version=body.version,
        )
        return res

    @router.post("/api/learning/knowledge/sync")
    async def sync_knowledge(body: KnowledgeSyncIn, a=Depends(get_a)):
        return await a.knowledge.sync(topic_filter=body.topic, source_id=body.source_id, force=body.force)

    @router.post("/api/learning/knowledge/{source_id}/rollback")
    async def rollback_knowledge(source_id: str, version: int | None = None, a=Depends(get_a)):
        return a.knowledge.rollback_source(source_id, target_version=version)

    @router.get("/api/learning/knowledge/search")
    async def search_knowledge(q: str, k: int = 5, a=Depends(get_a)):
        return {"results": a.knowledge.search(q, k=k)}

    # ---- Feedback Engine (Phase 4) -------------------------------------------------------------------
    @router.get("/api/learning/feedback")
    async def list_feedback(status: str | None = None, category: str | None = None, limit: int = 50, a=Depends(get_a)):
        return {"summary": a.feedback.summary(), "records": a.feedback.list_records(status=status, category=category, limit=limit)}

    @router.post("/api/learning/feedback")
    async def create_feedback(body: FeedbackCreateIn, a=Depends(get_a)):
        rec = a.feedback.record(**body.model_dump())
        return {"record": rec.to_dict()}

    @router.post("/api/learning/feedback/{feedback_id}/approve")
    async def approve_feedback(feedback_id: str, a=Depends(get_a)):
        return {"record": a.feedback.approve(feedback_id).to_dict()}

    @router.post("/api/learning/feedback/{feedback_id}/reject")
    async def reject_feedback(feedback_id: str, a=Depends(get_a)):
        return {"record": a.feedback.reject(feedback_id).to_dict()}

    @router.delete("/api/learning/feedback/{feedback_id}")
    async def delete_feedback(feedback_id: str, a=Depends(get_a)):
        ok = a.feedback.delete(feedback_id)
        if not ok:
            raise HTTPException(404, f"Feedback record '{feedback_id}' not found")
        return {"deleted": feedback_id}

    # ---- Datasets (Phase 5) --------------------------------------------------------------------------
    @router.get("/api/learning/datasets")
    async def list_datasets(a=Depends(get_a)):
        return {
            "sources": a.dataset_sources.list_sources(),
            "datasets": a.dataset_manager.list_datasets(),
            "builds": a.dataset_builder.list_builds(),
        }

    @router.get("/api/learning/datasets/discover")
    async def discover_datasets(q: str = "instruction", limit: int = 8, a=Depends(get_a)):
        try:
            items = await a.dataset_sources.discover_huggingface(q, limit=limit)
            return {"results": items}
        except SearchOfflineError as e:
            raise HTTPException(503, str(e)) from e

    @router.post("/api/learning/datasets/import-local")
    async def import_local_dataset(body: DatasetImportLocalIn, a=Depends(get_a)):
        snap = a.dataset_manager.import_local_file(
            body.path,
            dataset_id=body.dataset_id,
            license_name=body.license,
            name=body.name,
            expected_sha256=body.expected_sha256,
        )
        return {"snapshot": snap}

    @router.post("/api/learning/datasets/download")
    async def download_remote_dataset(body: DatasetDownloadIn, a=Depends(get_a)):
        snap = await a.dataset_manager.download_remote(
            body.url,
            dataset_id=body.dataset_id,
            license_name=body.license,
            name=body.name,
            expected_sha256=body.expected_sha256,
        )
        return {"snapshot": snap}

    @router.post("/api/learning/datasets/prepare-feedback")
    async def prepare_feedback_dataset(target_format: str = "messages", a=Depends(get_a)):
        return a.prepare_training_dataset_from_feedback(target_format=target_format)

    @router.post("/api/learning/datasets/process")
    async def process_dataset(body: DatasetProcessIn, a=Depends(get_a)):
        ver_info = a.dataset_manager.get_version(body.dataset_id, body.version)
        if not ver_info:
            raise HTTPException(404, f"Dataset '{body.dataset_id}' not found")
        vnum = ver_info["version"]
        rep, recs = a.dataset_validator.validate_file(
            Path(ver_info["file"]),
            dataset_id=body.dataset_id,
            version=vnum,
            license_name=ver_info["license"],
        )
        a.dataset_manager.update_stage_report(body.dataset_id, vnum, "validation", rep.to_dict())
        if not rep.approved:
            a.learning_audit.record("dataset_rejected", details={"dataset_id": body.dataset_id, "reasons": rep.reasons})
            return {"approved": False, "validation": rep.to_dict()}

        clean_rep, cleaned = a.dataset_cleaner.clean(
            recs,
            dataset_id=body.dataset_id,
            version=vnum,
            min_quality=body.min_quality,
        )
        a.dataset_manager.update_stage_report(body.dataset_id, vnum, "cleaning", clean_rep.to_dict())
        built = a.dataset_builder.build(
            cleaned,
            build_id=f"build-{body.dataset_id}-v{vnum}",
            dataset_ids=[f"{body.dataset_id}:v{vnum}"],
            target_format=body.target_format,
        )
        return {"approved": True, "validation": rep.to_dict(), "cleaning": clean_rep.to_dict(), "build": built.to_dict()}

    # ---- Training Jobs (Phase 5) ---------------------------------------------------------------------
    @router.get("/api/learning/training/jobs")
    async def list_training_jobs(a=Depends(get_a)):
        h = await a.brain.health()
        return {
            "hardware": a.training_manager.hardware_profile(ollama_ps=h.get("loaded") or []),
            "jobs": a.training_manager.list_jobs(),
            "audit": a.learning_audit.recent(limit=40),
        }

    @router.post("/api/learning/training/jobs")
    async def create_training_job(body: TrainingJobCreateIn, a=Depends(get_a)):
        h = await a.brain.health()
        cfg = TrainingJobConfig(
            base_model=body.base_model or a.brain.spec.model,
            hf_model_id=body.hf_model_id,
            dataset_build_id=body.dataset_build_id,
            quantization=body.quantization,
            epochs=body.epochs,
            max_seq_length=body.max_seq_length,
        )
        return a.training_manager.create_job(cfg, ollama_ps=h.get("loaded") or [])

    @router.post("/api/learning/training/jobs/{job_id}/approve")
    async def approve_training_job(job_id: str, a=Depends(get_a)):
        return a.training_manager.approve_job(job_id, approver="ui_user")

    @router.post("/api/learning/training/jobs/{job_id}/start")
    async def start_training_job(job_id: str, body: TrainingStartIn | None = None, a=Depends(get_a)):
        b = body or TrainingStartIn()
        try:
            h = await a.brain.health()
            return a.training_manager.start_job(
                job_id,
                force_spawn_even_if_blocked=False,
                override=b.override,
                override_reason=b.override_reason,
                actor=b.actor,
                ollama_ps=h.get("loaded") or [],
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/api/learning/training/jobs/{job_id}/pause")
    async def pause_training_job(job_id: str, a=Depends(get_a)):
        return a.training_manager.pause_job(job_id)

    @router.post("/api/learning/training/jobs/{job_id}/resume")
    async def resume_training_job(job_id: str, a=Depends(get_a)):
        return a.training_manager.resume_job(job_id)

    @router.post("/api/learning/training/jobs/{job_id}/cancel")
    async def cancel_training_job(job_id: str, a=Depends(get_a)):
        return a.training_manager.cancel_job(job_id)

    # ---- Evaluation & Model Registry / Rollback (Phases 6 & 7) ---------------------------------------
    @router.post("/api/learning/evaluate")
    async def evaluate_models(body: EvaluateIn, a=Depends(get_a)):
        return await a.run_model_evaluation(candidate_version_id=body.candidate_version_id)

    @router.get("/api/learning/evaluations")
    async def list_evaluations(a=Depends(get_a)):
        return {"evaluations": a.evaluator.list_reports(limit=20)}

    @router.get("/api/learning/registry")
    async def get_registry(a=Depends(get_a)):
        return a.registry.summary()

    @router.post("/api/learning/registry/candidate")
    async def register_candidate(body: CandidateRegisterIn, a=Depends(get_a)):
        return a.registry.register_candidate(**body.model_dump())

    @router.post("/api/learning/registry/{version_id}/promote")
    async def promote_version(version_id: str, a=Depends(get_a)):
        try:
            installed = await a.brain.installed()
            return a.registry.promote(version_id, actor="ui_user", installed_ollama_models=installed)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e

    @router.post("/api/learning/registry/rollback")
    async def rollback_version(body: RollbackIn, a=Depends(get_a)):
        try:
            return a.rollback_model(target_version_id=body.target_version_id)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e

    return router
