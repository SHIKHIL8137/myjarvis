"""Clean Modular v2 API Router exposing 5-Way Memory, Decision/Gateway, Projects/Graphify,
LangGraph Workforce Runs, Approvals, Document Engine, Reminders, and 3-2-1 Disaster Recovery."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .api_deps import make_assistant_dep
from .departments.assistant.documents import parse_document
from .integrations import graphify_adapter
from .memory.legacy_migrator import migrate_legacy_to_db


class DecisionIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class PersonalMemIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    category: str = Field("explicit", pattern="^(explicit|preference|temporary)$")
    topic: str = ""
    title: str = ""
    ttl_seconds: float | None = Field(None, ge=60, le=31_536_000)


class ExperienceIn(BaseModel):
    scope: str = Field("assistant", pattern="^(assistant|dev)$")
    category: str = "correction"
    trigger_context: str = Field(..., min_length=1, max_length=4000)
    lesson_after: str = Field(..., min_length=1, max_length=4000)
    outcome_before: str = ""
    topic: str = ""
    project_id: str | None = None
    approved: bool = True


class ProjectCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str = Field(..., min_length=1, max_length=600)
    default_branch: str = "main"
    test_cmd: str = "pytest -q"
    lint_cmd: str = "ruff check"


class GraphQueryIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    budget: int = Field(2000, ge=200, le=8000)


class WorkforceRunIn(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    task_prompt: str = Field(..., min_length=3, max_length=4000)
    target_symbol: str = ""
    qa_command: list[str] | None = None
    model: str | None = None


class AssistantRunIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=4000)
    task_kind: str = Field("research", pattern="^(research|document_export|reminder|email_draft_and_send)$")
    payload: dict = Field(default_factory=dict)


class ApprovalDecideIn(BaseModel):
    approved: bool
    decided_by: str = "owner"


class DocGenerateIn(BaseModel):
    format: str = Field(..., pattern="^(pdf|docx|word|pptx|powerpoint|slides|xlsx|excel|spreadsheet)$")
    filename: str = Field(..., min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=200)
    payload: dict = Field(default_factory=dict)


class DocParseIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)


class ReminderCreateIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    due_at: float
    notes: str = ""
    channel: str = "voice+ui"


class BackupCreateIn(BaseModel):
    tier: str = Field("tier2_warm", pattern="^(tier1_hot|tier2_warm|tier3_cold)$")
    passphrase: str = Field(..., min_length=8, max_length=128)


class BackupVerifyIn(BaseModel):
    bundle_path: str = Field(..., min_length=1, max_length=600)
    passphrase: str = Field(..., min_length=8, max_length=128)


class MemoryUpdateIn(BaseModel):
    content: str | None = Field(None, min_length=1, max_length=4000)
    title: str | None = Field(None, max_length=200)
    topic: str | None = Field(None, max_length=120)
    privacy_level: str | None = Field(None, pattern="^(public|internal|private|confidential|restricted)$")
    confidence: float | None = Field(None, ge=0.0, le=1.0)


class MemorySupersedeIn(BaseModel):
    new_content: str = Field(..., min_length=1, max_length=4000)
    title: str = ""
    topic: str = ""


def create_v2_router(fallback=None) -> APIRouter:
    router = APIRouter(prefix="/api/v2")
    get_a = make_assistant_dep(fallback)

    @router.get("/system/overview")
    async def v2_overview(a=Depends(get_a)):
        o = a.orchestrator
        return {
            "db": await o.db.health(),
            "memory": o.memory.summary(),
            "gateway": o.gateway.usage_summary() if o.gateway else {},
            "projects": o.projects.list_projects(),
            "runs": o.list_runs(limit=10),
            "pending_approvals": o.approvals.list_pending(),
            "reminders": o.reminders.list_reminders(),
            "documents": o.documents.list_generated(),
            "backups": o.recovery.list_manifests(),
        }

    # ---- Decision & Gateway --------------------------------------------------------------------------
    @router.post("/decision/route")
    async def decision_route(body: DecisionIn, a=Depends(get_a)):
        plan = await a.orchestrator.decision.decide(body.query)
        return plan.to_dict()

    @router.get("/gateway/usage")
    async def gateway_usage(a=Depends(get_a)):
        return a.orchestrator.gateway.usage_summary()

    @router.get("/gateway/capabilities")
    async def gateway_capabilities(a=Depends(get_a)):
        gw = a.orchestrator.gateway
        return {
            "capabilities": gw.list_capabilities(),
            "usage": gw.usage_summary(),
            "internet_mode": a.settings.mode,
            "allow_cloud": a.settings.providers.allow_cloud,
            "allow_paid_models": a.settings.providers.allow_paid_models,
        }

    # ---- 5-Way Memory --------------------------------------------------------------------------------
    @router.get("/memory/summary")
    async def memory_summary(a=Depends(get_a)):
        return a.orchestrator.memory.summary()

    @router.get("/memory/personal")
    async def list_personal(
        category: str | None = None,
        project_id: str | None = None,
        q: str | None = None,
        include_archived: bool = False,
        a=Depends(get_a),
    ):
        m = a.orchestrator.memory
        if q:
            return {"items": m.search_personal_memory(q, k=15, project_id=project_id)}
        return {
            "items": m.list_personal_memories(
                category=category,
                project_id=project_id,
                include_archived=include_archived,
            )
        }

    @router.post("/memory/personal")
    async def add_personal(body: PersonalMemIn, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.add_personal_memory(**body.model_dump())}

    @router.put("/memory/personal/{memory_id}")
    async def update_personal(memory_id: str, body: MemoryUpdateIn, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.update_personal_memory(memory_id, **body.model_dump(exclude_none=True))}

    @router.post("/memory/personal/{memory_id}/supersede")
    async def supersede_personal(memory_id: str, body: MemorySupersedeIn, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.supersede_personal_memory(memory_id, **body.model_dump())}

    @router.post("/memory/personal/{memory_id}/archive")
    async def archive_personal(memory_id: str, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.archive_personal_memory(memory_id)}

    @router.post("/memory/personal/{memory_id}/restore")
    async def restore_personal(memory_id: str, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.restore_personal_memory(memory_id)}

    @router.delete("/memory/personal/{memory_id}")
    async def delete_personal(memory_id: str, hard: bool = False, a=Depends(get_a)):
        return a.orchestrator.memory.delete_personal_memory(memory_id, hard=hard)

    @router.post("/memory/purge-expired")
    async def purge_expired_memories(a=Depends(get_a)):
        return a.orchestrator.memory.purge_expired()

    @router.get("/memory/experience")
    async def list_experience(scope: str | None = None, project_id: str | None = None, q: str | None = None, a=Depends(get_a)):
        m = a.orchestrator.memory
        if q:
            return {"items": m.search_experience(q, scope=scope, project_id=project_id, k=10)}
        return {"items": m.list_experience_lessons(scope=scope, project_id=project_id)}

    @router.post("/memory/experience")
    async def add_experience(body: ExperienceIn, a=Depends(get_a)):
        return {"record": a.orchestrator.memory.add_experience_lesson(**body.model_dump())}

    @router.post("/memory/migrate-legacy")
    async def run_legacy_migration(a=Depends(get_a)):
        counts = migrate_legacy_to_db(a.orchestrator.db, notes_dir=a.settings.notes_path, data_dir=a.data_dir)
        return {"ok": True, "imported": counts}

    # ---- Projects, Graphify & Dev Workforce ----------------------------------------------------------
    @router.get("/projects")
    async def list_projects(a=Depends(get_a)):
        return {"projects": a.orchestrator.projects.list_projects()}

    @router.post("/projects")
    async def register_project(body: ProjectCreateIn, a=Depends(get_a)):
        return {"project": a.orchestrator.projects.register(**body.model_dump())}

    @router.post("/projects/{project_id}/graphify/sync")
    async def sync_project_graph(project_id: str, a=Depends(get_a)):
        return a.orchestrator.projects.sync_graphify(project_id, bin_name=a.settings.workforce.graphify_bin)

    @router.get("/projects/{project_id}/graph")
    async def get_project_graph(project_id: str, a=Depends(get_a)):
        proj = a.orchestrator.projects.get(project_id)
        return graphify_adapter.load_graph_json(Path(proj["root_path"]))

    @router.post("/projects/{project_id}/graphify/query")
    async def query_project_graph(project_id: str, body: GraphQueryIn, a=Depends(get_a)):
        proj = a.orchestrator.projects.get(project_id)
        return graphify_adapter.query_graph(Path(proj["root_path"]), body.question, budget=body.budget, bin_name=a.settings.workforce.graphify_bin)

    # ---- LangGraph Multi-Agent Runs & Approvals ------------------------------------------------------
    @router.get("/workforce/runs")
    async def list_agent_runs(limit: int = 25, a=Depends(get_a)):
        return {"runs": a.orchestrator.list_runs(limit=limit)}

    @router.get("/workforce/worktrees")
    async def list_worktrees(project_id: str | None = None, a=Depends(get_a)):
        return {"worktrees": a.orchestrator.workforce.list_worktrees(project_id=project_id)}

    @router.post("/workforce/runs")
    async def trigger_workforce_run(body: WorkforceRunIn, a=Depends(get_a)):
        return a.orchestrator.start_workforce_workflow(**body.model_dump())

    @router.post("/workforce/runs/{run_id}/cancel")
    async def cancel_workforce_run(run_id: str, a=Depends(get_a)):
        a.orchestrator._update_run(run_id, "cancelled", "Cancelled by owner")
        return {"ok": True, "run_id": run_id, "state": "cancelled"}

    @router.post("/assistant/runs")
    async def trigger_assistant_run(body: AssistantRunIn, a=Depends(get_a)):
        return a.orchestrator.start_assistant_workflow(**body.model_dump())

    @router.get("/approvals")
    async def list_approvals(a=Depends(get_a)):
        return {"pending": a.orchestrator.approvals.list_pending()}

    @router.post("/approvals/{token}/decide")
    async def decide_approval(token: str, body: ApprovalDecideIn, a=Depends(get_a)):
        return a.orchestrator.resume_workflow_after_approval(token=token, approved=body.approved, decided_by=body.decided_by)

    # ---- Documents (PDF / DOCX / PPTX / XLSX) --------------------------------------------------------
    @router.get("/documents")
    async def list_documents(a=Depends(get_a)):
        return {"documents": a.orchestrator.documents.list_generated()}

    @router.post("/documents/generate")
    async def generate_document(body: DocGenerateIn, a=Depends(get_a)):
        return a.orchestrator.documents.generate(body.format, filename=body.filename, title=body.title, payload=body.payload)

    @router.post("/documents/parse")
    async def parse_user_document(body: DocParseIn, a=Depends(get_a)):
        safe_path = a.filesystem.resolve_safe(body.path, must_exist=True)
        return parse_document(safe_path)

    @router.get("/documents/download/{filename}")
    async def download_document(filename: str, a=Depends(get_a)):
        out_dir = a.orchestrator.documents.output_dir
        target = (out_dir / filename).resolve()
        if target.parent != out_dir or not target.exists():
            raise HTTPException(404, "Generated document not found")
        return FileResponse(target, filename=target.name)

    # ---- Reminders & Email Drafts / Calendar ---------------------------------------------------------
    @router.get("/reminders")
    async def list_reminders(a=Depends(get_a)):
        return {"reminders": a.orchestrator.reminders.list_reminders()}

    @router.post("/reminders")
    async def create_reminder(body: ReminderCreateIn, a=Depends(get_a)):
        return {"reminder": a.orchestrator.reminders.create(**body.model_dump())}

    @router.post("/reminders/{reminder_id}/complete")
    async def complete_reminder(reminder_id: str, a=Depends(get_a)):
        return {"reminder": a.orchestrator.reminders.complete(reminder_id)}

    @router.get("/productivity/drafts")
    async def list_drafts(a=Depends(get_a)):
        return {"drafts": a.orchestrator.email.list_drafts(), "events": a.orchestrator.calendar.list_events()}

    # ---- 3-2-1 Disaster Recovery ---------------------------------------------------------------------
    @router.get("/recovery/manifests")
    async def list_backups(a=Depends(get_a)):
        return {"manifests": a.orchestrator.recovery.list_manifests()}

    @router.post("/recovery/backup")
    async def run_backup(body: BackupCreateIn, a=Depends(get_a)):
        return a.orchestrator.recovery.create_backup(
            tier=body.tier,
            passphrase=body.passphrase,
            notes_dir=a.settings.notes_path,
        )

    @router.post("/recovery/verify")
    async def verify_backup(body: BackupVerifyIn, a=Depends(get_a)):
        return a.orchestrator.recovery.verify_backup(Path(body.bundle_path), passphrase=body.passphrase)

    @router.get("/recovery/instructions")
    async def recovery_instructions(a=Depends(get_a)):
        return {
            "steps": a.orchestrator.recovery.recovery_steps_doc(),
            "db": await a.orchestrator.db.health(),
            "backup_root": str(a.orchestrator.recovery.backup_root),
        }

    return router

