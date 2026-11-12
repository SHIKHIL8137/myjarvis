"""Clean Modular v2 API Router exposing 5-Way Memory, Decision/Gateway, Projects/Graphify,
LangGraph Workforce Runs, Approvals, Document Engine, Reminders, and 3-2-1 Disaster Recovery."""
import asyncio
from pathlib import Path
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import graph_live, sysinfo
from .actions import ActionPermissionError
from .api_deps import (
    apply_settings_patch,
    build_consolidated_settings,
    list_settings_devices,
    make_assistant_dep,
)
from .api_decision import create_decision_subrouter
from .api_skills import create_skills_subrouter
from .config import PROVIDER_ENV_VARS, delete_provider_api_key, has_provider_api_key, set_provider_api_key
from .departments.assistant.documents import parse_document
from .filesystem_access import ALL_OPS, FS_OPERATION_GROUPS, FilesystemSecurityError
from .gateway.gateway import CloudGatewayError
from .gateway.interface import GatewayRequest
from .integrations import graphify_adapter
from .memory.legacy_migrator import migrate_legacy_to_db


class DecisionIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class ActionExecuteIn(BaseModel):
    action: str = Field(..., min_length=1, max_length=80)
    args: dict = Field(default_factory=dict)
    confirmed_token: str | None = Field(default=None, max_length=64)


class PersonalMemIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    category: str = Field(
        "explicit",
        pattern="^(explicit|preference|temporary|user_preferences|project_memory|command_memory|conversation_memory)$",
    )
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


class CancelRunIn(BaseModel):
    reason: str = "Cancelled by owner"
    actor: str = "owner"
    authorize_cleanup: bool = False


class DocGenerateIn(BaseModel):
    format: str = Field(..., pattern="^(pdf|docx|word|pptx|powerpoint|slides|xlsx|excel|spreadsheet|md|markdown)$")
    filename: str = Field(..., min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=200)
    payload: dict = Field(default_factory=dict)


class TaskSteerIn(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=1000)
    author: str = "owner"
    resume_if_paused: bool = False


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
    include_repositories: bool = False
    include_uncommitted: bool = False


class BackupVerifyIn(BaseModel):
    bundle_path: str = Field(..., min_length=1, max_length=600)
    passphrase: str = Field(..., min_length=8, max_length=128)
    scratch_pg_url: str | None = Field(None, max_length=600)


class DbTargetRegisterIn(BaseModel):
    role: str = Field(..., pattern="^(local_sqlite|dev_pg|test_pg|scratch_pg|prod_pg)$")
    url: str = Field(..., min_length=5, max_length=600)
    label: str = ""


class ProdRestoreRequestIn(BaseModel):
    bundle_path: str = Field(..., min_length=1, max_length=600)
    target_pg_url: str = Field(..., min_length=8, max_length=600)
    passphrase: str = Field(..., min_length=8, max_length=128)
    reason: str = ""


class ProdRestoreExecuteIn(BaseModel):
    approval_token: str = Field(..., min_length=4, max_length=80)
    bundle_path: str = Field(..., min_length=1, max_length=600)
    target_pg_url: str = Field(..., min_length=8, max_length=600)
    passphrase: str = Field(..., min_length=8, max_length=128)


class SandboxChatIn(BaseModel):
    model: str | None = None
    messages: list[dict]
    max_tokens: int = Field(512, ge=16, le=8192)
    temperature: float = Field(0.6, ge=0.0, le=2.0)


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


class FsPermissionDecideIn(BaseModel):
    decision: str = Field(..., pattern="^(allow_once|allow_session|always_read_only|allow_persistent|deny)$")
    decided_by: str = Field("owner", min_length=1, max_length=64)
    duration_seconds: int | None = Field(None, ge=30, le=86_400)
    allowed_operations: list[str] | None = None
    execute_deferred: bool = True


class FsPermissionCreateIn(BaseModel):
    folder_path: str = Field(..., min_length=1, max_length=800)
    operations: list[str] = Field(..., min_length=1)
    scope: str = Field("persistent", pattern="^(once|session|persistent)$")
    granted_by: str = Field("owner", min_length=1, max_length=64)
    purpose: str = "Configured in Settings"
    expires_in_seconds: int | None = Field(None, ge=30, le=2_592_000)


class FsPermissionUpdateIn(BaseModel):
    granted_operations: list[str] | None = None
    scope: str | None = Field(None, pattern="^(once|session|persistent)$")
    actor: str = Field("owner", min_length=1, max_length=64)


class FsPolicyUpdateIn(BaseModel):
    sensitive_file_policy: str | None = Field(None, pattern="^(always_ask|block|warn|metadata_only)$")
    cloud_data_policy: str | None = Field(None, pattern="^(local_only|redact_only|redact_and_confirm|never|redacted|approved_only)$")
    allow_always_read_only: bool | None = None
    max_read_bytes: int | None = Field(None, ge=10_000, le=5_000_000)
    max_file_bytes: int | None = Field(None, ge=10_000, le=5_000_000)
    allowed_extensions: list[str] | None = None


class FsAliasCreateIn(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100)
    folder_path: str = Field(..., min_length=1, max_length=800)
    description: str = Field("", max_length=400)
    actor: str = Field("owner", min_length=1, max_length=64)


class ProviderKeyIn(BaseModel):
    api_key: str = Field(..., min_length=4, max_length=512)


class RouteTestIn(BaseModel):
    role: str = Field("fast", pattern="^(fast|advanced|coding|embedding)$")
    require_tools: bool = False
    require_vision: bool = False
    json_mode: bool = False
    project_privacy_level: str = Field("private", pattern="^(public|internal|private|confidential|restricted)$")


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
            "skills": {"installed": len(o.skills.list_skills()), "enabled": len(o.skills.list_skills(enabled_only=True))},
        }

    # ---- Unified live knowledge graph (deltas arrive as `graph_event` on /api/events) ----------------
    @router.get("/graph")
    async def unified_graph(types: str = "", limit: int = Query(300, ge=1, le=2000), a=Depends(get_a)):
        wanted = {t.strip() for t in types.split(",") if t.strip()} or None
        return await asyncio.to_thread(graph_live.build_graph, a, wanted, limit)

    @router.get("/graph/events")
    async def replay_graph_events(since_seq: int = Query(0, ge=0), a=Depends(get_a)):
        events, gap = a.bus.events_since(since_seq)
        return {"schema_version": 1, "latest_seq": a.bus.latest_seq, "gap": gap, "events": events}

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

    @router.get("/memory/categories")
    async def memory_categories(a=Depends(get_a)):
        return a.orchestrator.memory.category_summary()

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
            return {"items": m.search_personal_memory(q, k=15, project_id=project_id, category=category)}
        return {
            "items": m.list_personal_memories(
                category=category,
                project_id=project_id,
                include_archived=include_archived,
            )
        }

    @router.post("/memory/personal")
    async def add_personal(body: PersonalMemIn, a=Depends(get_a)):
        try:
            rec = a.orchestrator.memory.add_personal_memory(**body.model_dump())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        graph_live.emit(a.bus, "memory.created", nodes=[graph_live.memory_node(rec)], links=graph_live.memory_links(rec))
        return {"record": rec}

    @router.put("/memory/personal/{memory_id}")
    async def update_personal(memory_id: str, body: MemoryUpdateIn, a=Depends(get_a)):
        try:
            rec = a.orchestrator.memory.update_personal_memory(memory_id, **body.model_dump(exclude_none=True))
        except KeyError as e:
            raise HTTPException(404, f"Memory {memory_id} not found") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        graph_live.emit(a.bus, "memory.updated", nodes=[graph_live.memory_node(rec)], links=graph_live.memory_links(rec))
        return {"record": rec}

    @router.post("/memory/personal/{memory_id}/supersede")
    async def supersede_personal(memory_id: str, body: MemorySupersedeIn, a=Depends(get_a)):
        rec = a.orchestrator.memory.supersede_personal_memory(memory_id, **body.model_dump())
        graph_live.emit(a.bus, "memory.updated", resync=True)
        return {"record": rec}

    @router.post("/memory/personal/{memory_id}/archive")
    async def archive_personal(memory_id: str, a=Depends(get_a)):
        rec = a.orchestrator.memory.archive_personal_memory(memory_id)
        graph_live.emit(a.bus, "memory.deleted", removed=[f"memory:{memory_id}"])
        return {"record": rec}

    @router.post("/memory/personal/{memory_id}/restore")
    async def restore_personal(memory_id: str, a=Depends(get_a)):
        rec = a.orchestrator.memory.restore_personal_memory(memory_id)
        graph_live.emit(a.bus, "memory.created", nodes=[graph_live.memory_node(rec)], links=graph_live.memory_links(rec))
        return {"record": rec}

    @router.delete("/memory/personal/{memory_id}")
    async def delete_personal(memory_id: str, hard: bool = False, a=Depends(get_a)):
        res = a.orchestrator.memory.delete_personal_memory(memory_id, hard=hard)
        graph_live.emit(a.bus, "memory.deleted", removed=[f"memory:{memory_id}"])
        return res

    @router.post("/memory/purge-expired")
    async def purge_expired_memories(a=Depends(get_a)):
        return a.orchestrator.memory.purge_expired()

    @router.post("/memory/clear-conversation")
    async def clear_conversation_memories(a=Depends(get_a)):
        res = a.orchestrator.memory.clear_conversation_memory()
        legacy_cleared = a.memory_store.clear_temporary() if hasattr(a, "memory_store") else 0
        graph_live.emit(a.bus, "memory.deleted", resync=True)
        return {"ok": True, "cleared_count": res["cleared_count"] + legacy_cleared, "db_cleared": res["cleared_count"], "legacy_cleared": legacy_cleared}

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
        return await asyncio.to_thread(a.orchestrator.start_workforce_workflow, **body.model_dump())

    @router.post("/workforce/runs/{run_id}/cancel")
    async def cancel_workforce_run(run_id: str, body: CancelRunIn | None = None, a=Depends(get_a)):
        b = body or CancelRunIn()
        try:
            return await asyncio.to_thread(
                a.orchestrator.cancel_workforce_run,
                run_id,
                reason=b.reason,
                actor=b.actor,
                authorize_cleanup=b.authorize_cleanup,
            )
        except KeyError:
            raise HTTPException(404, f"Run {run_id} not found")

    @router.post("/assistant/runs")
    async def trigger_assistant_run(body: AssistantRunIn, a=Depends(get_a)):
        return await asyncio.to_thread(a.orchestrator.start_assistant_workflow, **body.model_dump())

    @router.get("/approvals")
    async def list_approvals(a=Depends(get_a)):
        return {"pending": a.orchestrator.approvals.list_pending()}

    @router.post("/approvals/{token}/decide")
    async def decide_approval(token: str, body: ApprovalDecideIn, a=Depends(get_a)):
        try:
            row_before = await asyncio.to_thread(a.orchestrator.approvals.get, token)
            res = await asyncio.to_thread(
                a.orchestrator.resume_workflow_after_approval,
                token=token,
                approved=body.approved,
                decided_by=body.decided_by,
            )
            task_id = ((row_before or {}).get("args") or {}).get("task_id") if isinstance(row_before, dict) else None
            if task_id:
                status = "completed" if body.approved else "cancelled"
                reply = (
                    f"Owner approved `{token}`. Action authorized."
                    if body.approved
                    else f"Owner denied `{token}`. Privileged action aborted safely."
                )
                task = await a.orchestrator.tasks.update_task(
                    task_id,
                    status=status,
                    current_step=reply,
                    result=reply,
                )
                a.history.add("assistant", reply)
                a.bus.publish(
                    {
                        "type": "assistant_message",
                        "text": reply,
                        "approval_token": token,
                        "granted": body.approved,
                        "task_id": task_id,
                        "task": task,
                        "paths": [],
                    }
                )
                res = {**res, "task": task, "reply": reply}
            return res
        except KeyError:
            raise HTTPException(404, f"Approval {token} not found")
        except TimeoutError as e:
            raise HTTPException(410, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

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
        return await asyncio.to_thread(
            a.orchestrator.recovery.create_backup,
            tier=body.tier,
            passphrase=body.passphrase,
            notes_dir=a.settings.notes_path,
            include_repositories=body.include_repositories,
            include_uncommitted=body.include_uncommitted,
        )

    @router.post("/recovery/verify")
    async def verify_backup(body: BackupVerifyIn, a=Depends(get_a)):
        try:
            eff_scratch = body.scratch_pg_url or (a.settings.database.scratch_target_url or None)
            return await asyncio.to_thread(
                a.orchestrator.recovery.verify_backup,
                Path(body.bundle_path),
                passphrase=body.passphrase,
                scratch_pg_url=eff_scratch,
            )
        except ValueError as e:
            raise HTTPException(403, str(e)) from e

    @router.get("/recovery/targets")
    async def list_db_targets(a=Depends(get_a)):
        return {"targets": a.orchestrator.recovery.list_database_targets()}

    @router.post("/recovery/targets")
    async def register_db_target(body: DbTargetRegisterIn, a=Depends(get_a)):
        try:
            return {"target": a.orchestrator.recovery.register_database_target(body.role, body.url, label=body.label)}
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.post("/recovery/restore/request-production")
    async def request_prod_restore(body: ProdRestoreRequestIn, a=Depends(get_a)):
        try:
            req = await asyncio.to_thread(
                a.orchestrator.recovery.request_production_restore,
                Path(body.bundle_path),
                target_pg_url=body.target_pg_url,
                passphrase=body.passphrase,
                requested_by="ui_user",
                reason=body.reason,
            )
            return {"approval": req}
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/recovery/restore/execute")
    async def execute_prod_restore(body: ProdRestoreExecuteIn, a=Depends(get_a)):
        try:
            return await asyncio.to_thread(
                a.orchestrator.recovery.execute_production_restore,
                approval_token=body.approval_token,
                target_pg_url=body.target_pg_url,
                bundle_path=Path(body.bundle_path),
                passphrase=body.passphrase,
            )
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(403, str(e)) from e
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/recovery/instructions")
    async def recovery_instructions(a=Depends(get_a)):
        return {
            "steps": a.orchestrator.recovery.recovery_steps_doc(),
            "db": await a.orchestrator.db.health(),
            "backup_root": str(a.orchestrator.recovery.backup_root),
            "targets": a.orchestrator.recovery.list_database_targets(),
        }

    # ---- Scoped Container Model Gateway Proxy (OpenAI-compatible; short-lived bearer token only) --------
    @router.get("/sandbox/v1/models")
    async def sandbox_models(request: Request, a=Depends(get_a)):
        try:
            tok = a.orchestrator.sandbox_tokens.validate(request.headers.get("authorization", ""))
        except PermissionError as e:
            raise HTTPException(401, str(e)) from e
        caps = a.orchestrator.gateway.list_capabilities() if a.orchestrator.gateway else []
        models = list(tok.allowed_models) if tok.allowed_models else [f"{c['provider']}:{c['model_id']}" if not c["local"] else c["model_id"] for c in caps]
        return {"object": "list", "data": [{"id": m, "object": "model"} for m in models]}

    @router.post("/sandbox/v1/chat/completions")
    async def sandbox_chat_completions(body: SandboxChatIn, request: Request, a=Depends(get_a)):
        auth = request.headers.get("authorization", "")
        try:
            tok = a.orchestrator.sandbox_tokens.validate(auth, requested_model=body.model)
        except PermissionError as e:
            raise HTTPException(401, str(e)) from e
        gw = a.orchestrator.gateway
        if gw is None:
            raise HTTPException(503, "ModelGateway unavailable")
        model_key = body.model or (tok.allowed_models[0] if tok.allowed_models else a.settings.boot_brain)
        if not tok.allow_ollama and (model_key in a.settings.brains or model_key.startswith("ollama:")):
            raise HTTPException(403, "Local Ollama access is disabled for this sandbox network policy.")
        t0 = time.perf_counter()
        chunks: list[str] = []
        usage: dict = {}
        try:
            req = GatewayRequest(
                model_key=model_key,
                messages=body.messages,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
                run_id=tok.run_id,
                decision_layer=3,
            )
            async for ev in gw.stream_with_fallback(req):
                if ev.type == "token":
                    chunks.append(ev.text)
                elif ev.type == "done":
                    usage = ev.usage or {}
            latency_ms = (time.perf_counter() - t0) * 1000.0
            a.orchestrator.sandbox_tokens.record_request_metadata(
                run_id=tok.run_id,
                model=model_key,
                status="ok",
                latency_ms=latency_ms,
                token_count=int(usage.get("completion_tokens") or 0),
            )
            return {
                "id": f"chatcmpl-{tok.run_id}",
                "object": "chat.completion",
                "model": model_key,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(chunks)}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0),
                },
            }
        except CloudGatewayError as e:
            a.orchestrator.sandbox_tokens.record_request_metadata(
                run_id=tok.run_id,
                model=model_key,
                status=e.category,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            raise HTTPException(403 if e.category in ("paid_blocked", "secret_blocked", "privacy_blocked") else 400, str(e)) from e

    # ---- P10.20 Scoped Filesystem Permissions & Security Audit ---------------------------------------
    @router.get("/filesystem/permissions")
    async def fs_list_permissions(status: str | None = None, a=Depends(get_a)):
        fs = a.orchestrator.fs_access
        return {
            "permissions": await fs.list_permissions(status=status),
            "pending_requests": await fs.list_requests(status="pending"),
            "operation_groups": FS_OPERATION_GROUPS,
            "all_operations": ALL_OPS,
            "policy": a.settings.filesystem.model_dump(),
        }

    @router.post("/filesystem/permissions")
    async def fs_grant_permission(body: FsPermissionCreateIn, a=Depends(get_a)):
        try:
            perm = await a.orchestrator.fs_access.grant_direct(
                body.folder_path,
                body.operations,
                scope=body.scope,
                granted_by=body.granted_by,
                purpose=body.purpose,
                expires_in_seconds=body.expires_in_seconds,
            )
            a.bus.publish({"type": "filesystem_permissions_changed"})
            return {"ok": True, "permission": perm}
        except FilesystemSecurityError as e:
            raise HTTPException(403, str(e)) from e

    @router.put("/filesystem/permissions/{permission_id}")
    async def fs_update_permission(permission_id: str, body: FsPermissionUpdateIn, a=Depends(get_a)):
        try:
            perm = await a.orchestrator.fs_access.update_permission(
                permission_id,
                granted_operations=body.granted_operations,
                scope=body.scope,
                actor=body.actor,
            )
            a.bus.publish({"type": "filesystem_permissions_changed"})
            return {"ok": True, "permission": perm}
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except FilesystemSecurityError as e:
            raise HTTPException(403, str(e)) from e

    @router.post("/filesystem/permissions/{permission_id}/revoke")
    async def fs_revoke_permission(permission_id: str, actor: str = "owner", a=Depends(get_a)):
        try:
            perm = await a.orchestrator.fs_access.revoke_permission(permission_id, actor=actor)
            a.bus.publish({"type": "filesystem_permissions_changed"})
            return {"ok": True, "permission": perm}
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/filesystem/requests")
    async def fs_list_requests(status: str | None = None, a=Depends(get_a)):
        return {"requests": await a.orchestrator.fs_access.list_requests(status=status)}

    @router.post("/filesystem/requests/{request_id}/decide")
    async def fs_decide_request(request_id: str, body: FsPermissionDecideIn, a=Depends(get_a)):
        try:
            res = await a.orchestrator.fs_access.decide_request(
                request_id,
                body.decision,
                decided_by=body.decided_by,
                duration_seconds=body.duration_seconds,
                allowed_operations=body.allowed_operations,
            )
            a.bus.publish({"type": "filesystem_permissions_changed", "request_id": request_id})
            if body.execute_deferred:
                resumed = await a.resume_after_permission_decision(request_id, res)
                return {**res, **resumed}
            return res
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except FilesystemSecurityError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/filesystem/pick-folder")
    async def fs_pick_folder(a=Depends(get_a)):
        return await asyncio.to_thread(a.orchestrator.fs_access.pick_folder_dialog)

    @router.get("/filesystem/audit")
    async def fs_audit_events(limit: int = Query(80, ge=1, le=500), a=Depends(get_a)):
        return {"events": await a.orchestrator.fs_access.list_audit_events(limit=limit)}

    @router.put("/filesystem/policy")
    async def fs_update_policy(body: FsPolicyUpdateIn, a=Depends(get_a)):
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        if "max_read_bytes" in patch and "max_file_bytes" not in patch:
            patch["max_file_bytes"] = patch.pop("max_read_bytes")
        else:
            patch.pop("max_read_bytes", None)
        try:
            snap = apply_settings_patch(a, {"filesystem": patch}, allow_gateway_and_prefs=True)
            return {"ok": True, "filesystem": snap["filesystem"]}
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.get("/filesystem/aliases")
    async def fs_list_aliases(a=Depends(get_a)):
        return {"aliases": await a.orchestrator.fs_access.list_aliases()}

    @router.post("/filesystem/aliases")
    async def fs_upsert_alias(body: FsAliasCreateIn, a=Depends(get_a)):
        try:
            rec = await a.orchestrator.fs_access.upsert_alias(
                body.alias,
                body.folder_path,
                description=body.description,
                actor=body.actor,
            )
            a.bus.publish({"type": "filesystem_permissions_changed"})
            return {"ok": True, "alias": rec}
        except FilesystemSecurityError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.delete("/filesystem/aliases/{alias_name}")
    async def fs_delete_alias(alias_name: str, actor: str = "owner", a=Depends(get_a)):
        try:
            ok = await a.orchestrator.fs_access.delete_alias(alias_name, actor=actor)
        except FilesystemSecurityError as e:
            raise HTTPException(403, str(e)) from e
        if not ok:
            raise HTTPException(404, f"Alias '{alias_name}' not found")
        a.bus.publish({"type": "filesystem_permissions_changed"})
        return {"ok": True}

    # ---- P10.20 Centralized Assistant Tasks ----------------------------------------------------------
    @router.get("/tasks")
    async def list_assistant_tasks(
        status: str | None = None,
        active_only: bool = False,
        limit: int = Query(40, ge=1, le=200),
        a=Depends(get_a),
    ):
        return {"tasks": await a.orchestrator.tasks.list_tasks(status=status, active_only=active_only, limit=limit)}

    @router.get("/tasks/{task_id}")
    async def get_assistant_task(task_id: str, a=Depends(get_a)):
        t = await a.orchestrator.tasks.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"Task {task_id} not found")
        return t

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_assistant_task(task_id: str, reason: str = "Cancelled by owner", a=Depends(get_a)):
        t = await a.orchestrator.tasks.cancel_task(task_id, reason=reason)
        if t is None:
            raise HTTPException(404, f"Task {task_id} not found")
        await a.orchestrator.fs_access.consume_task_once_permissions(task_id)
        return {"ok": True, "task": t}

    @router.post("/tasks/{task_id}/steer")
    async def steer_assistant_task(task_id: str, body: TaskSteerIn, a=Depends(get_a)):
        try:
            t = await a.orchestrator.tasks.append_steer(
                task_id,
                body.instruction,
                author=body.author,
                resume_if_paused=body.resume_if_paused,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if t is None:
            raise HTTPException(404, f"Task {task_id} not found")
        a.orchestrator.task_runner.wake()
        return {"ok": True, "task": t}

    @router.post("/tasks/{task_id}/resume")
    async def resume_assistant_task(task_id: str, a=Depends(get_a)):
        t = await a.orchestrator.tasks.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"Task {task_id} not found")
        if t["status"] in ("awaiting_permission", "awaiting_approval"):
            raise HTTPException(409, "Task requires explicit permission or approval decision before resuming")
        if t["status"] not in ("paused", "waiting_quota", "queued"):
            raise HTTPException(409, f"Task cannot be resumed from status '{t['status']}'")
        updated = await a.orchestrator.tasks.update_task(
            task_id,
            status="queued",
            current_step="Manually resumed by owner",
            clear_not_before=True,
            clear_claim=True,
        )
        a.orchestrator.task_runner.wake()
        return {"ok": True, "task": updated}

    @router.get("/commands/history")
    async def list_command_history(
        limit: int = 25,
        status: str | None = None,
        q: str | None = None,
        a=Depends(get_a),
    ):
        items = await a.orchestrator.command_history.list_recent(limit=limit, status=status, query=q)
        return {"commands": items, "count": len(items)}

    @router.post("/commands/history/{cmd_id}/replay")
    async def replay_command_history_item(cmd_id: str, a=Depends(get_a)):
        cmd = await a.orchestrator.command_history.get_by_id(cmd_id)
        if cmd is None:
            raise HTTPException(404, f"Command history item {cmd_id} not found")
        if not cmd.get("replayable"):
            raise HTTPException(409, "This meta/continuity command is not marked replayable")
        await a.orchestrator.command_history.increment_replay(cmd_id)
        ev = await a.chat(cmd["raw_text"], speak=False, source="replay", _in_replay=True)
        return {"ok": bool(ev.get("ok", True)), "replayed_from": cmd, "result": ev}

    @router.get("/actions/registry")
    async def get_action_registry(limit: int = 20, a=Depends(get_a)):
        return {
            "actions": a.actions.list_action_registry(),
            "pending": a.actions.pending_approvals(),
            "history": a.actions.history(limit=limit),
        }

    @router.post("/actions/execute")
    async def execute_desktop_action(body: ActionExecuteIn, a=Depends(get_a)):
        try:
            return a.actions.request_or_execute(
                body.action,
                body.args,
                confirmed_token=body.confirmed_token,
            )
        except ActionPermissionError as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"Unknown confirmation token: {e}") from e
        except TimeoutError as e:
            raise HTTPException(408, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/actions/{token}/confirm")
    async def confirm_desktop_action(token: str, a=Depends(get_a)):
        try:
            return a.actions.confirm(token)
        except KeyError as e:
            raise HTTPException(404, "Unknown confirmation token") from e
        except TimeoutError as e:
            raise HTTPException(408, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except ActionPermissionError as e:
            raise HTTPException(403, str(e)) from e

    @router.post("/actions/{token}/reject")
    async def reject_desktop_action(token: str, a=Depends(get_a)):
        try:
            return a.actions.reject(token)
        except KeyError as e:
            raise HTTPException(404, "Unknown confirmation token") from e

    @router.post("/research")
    async def start_verified_deep_research(
        body: AssistantRunIn,
        run_now: bool = False,
        a=Depends(get_a),
    ):
        task = await a.orchestrator.task_runner.enqueue(
            body.goal,
            worker_kind="deep_research",
            intent="web_research",
            source="api",
            payload={"question": body.goal, **(body.payload or {})},
            meta={"safe_resumable": True},
        )
        if run_now:
            await a.orchestrator.task_runner.run_once()
            task = await a.orchestrator.tasks.get_task(task["task_id"]) or task
        return {"ok": True, "task": task}

    # ---- Stage E System Monitoring & Proactive Suggestions ------------------------------------------
    @router.get("/system/health")
    async def get_system_health(processes: bool = True, history_limit: int = 20, a=Depends(get_a)):
        snap = await asyncio.to_thread(sysinfo.snapshot, a.settings.monitoring, include_processes=processes)
        recent_tasks = await a.orchestrator.tasks.list_tasks(limit=10)
        recent_cmds = await a.orchestrator.command_history.list_recent(limit=25)
        suggestions = a.proactive.collect(sys_snapshot=snap, recent_tasks=recent_tasks, recent_commands=recent_cmds)
        return {
            **snap,
            "history": sysinfo.monitor_history(limit=history_limit),
            "suggestions": suggestions,
            "jarvis": {
                "os_state": a.os_state,
                "mode": a.settings.mode,
                "active_brain": a.settings.boot_brain,
                "active_provider": a.settings.providers.active_provider,
                "queued_tasks": len([t for t in recent_tasks if t["status"] in ("queued", "running", "planning")]),
                "failed_tasks": len([t for t in recent_tasks if t["status"] == "failed"]),
            },
        }

    @router.get("/system/suggestions")
    async def get_proactive_suggestions(a=Depends(get_a)):
        snap = await asyncio.to_thread(sysinfo.snapshot, a.settings.monitoring, include_processes=False)
        recent_tasks = await a.orchestrator.tasks.list_tasks(limit=10)
        recent_cmds = await a.orchestrator.command_history.list_recent(limit=25)
        items = a.proactive.collect(sys_snapshot=snap, recent_tasks=recent_tasks, recent_commands=recent_cmds)
        return {"enabled": a.settings.proactive.enabled, "suggestions": items, "count": len(items)}

    @router.post("/system/suggestions/{suggestion_id}/dismiss")
    async def dismiss_proactive_suggestion(suggestion_id: str, a=Depends(get_a)):
        return {"ok": True, **a.proactive.dismiss(suggestion_id)}

    # ---- P10.20 Consolidated Settings & Secure Provider Gateway --------------------------------------
    @router.get("/settings")
    async def get_consolidated_settings(a=Depends(get_a)):
        return build_consolidated_settings(a)

    @router.put("/settings")
    async def put_consolidated_settings(request: Request, a=Depends(get_a)):
        patch = await request.json()
        try:
            return {"ok": True, "settings": apply_settings_patch(a, patch, allow_gateway_and_prefs=True)}
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except Exception as e:
            raise HTTPException(422, str(e)) from e

    @router.get("/settings/devices")
    async def get_settings_devices(a=Depends(get_a)):
        return await asyncio.to_thread(list_settings_devices, a)

    @router.post("/gateway/providers/{provider}/key")
    async def save_provider_key(provider: str, body: ProviderKeyIn, a=Depends(get_a)):
        prov = provider.strip().lower()
        if prov not in PROVIDER_ENV_VARS:
            raise HTTPException(404, f"Unsupported provider '{prov}'")
        try:
            set_provider_api_key(prov, body.api_key)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        a.bus.publish({"type": "config_updated", "keys": [f"provider_key:{prov}"]})
        return {"ok": True, "provider": prov, "configured": has_provider_api_key(prov)}

    @router.delete("/gateway/providers/{provider}/key")
    async def remove_provider_key(provider: str, a=Depends(get_a)):
        prov = provider.strip().lower()
        if prov not in PROVIDER_ENV_VARS:
            raise HTTPException(404, f"Unsupported provider '{prov}'")
        delete_provider_api_key(prov)
        a.bus.publish({"type": "config_updated", "keys": [f"provider_key:{prov}"]})
        return {"ok": True, "provider": prov, "configured": has_provider_api_key(prov)}

    @router.post("/gateway/providers/{provider}/health")
    async def probe_provider_health(provider: str, a=Depends(get_a)):
        gw = a.orchestrator.gateway
        if gw is None:
            raise HTTPException(503, "ModelGateway unavailable")
        return await gw.health_check_provider(provider)

    @router.post("/gateway/route-test")
    async def gateway_route_test(body: RouteTestIn, a=Depends(get_a)):
        gw = a.orchestrator.gateway
        if gw is None:
            raise HTTPException(503, "ModelGateway unavailable")
        return await gw.test_route(
            role=body.role,
            require_tools=body.require_tools,
            require_vision=body.require_vision,
            json_mode=body.json_mode,
            project_privacy_level=body.project_privacy_level,
        )

    router.include_router(create_skills_subrouter(fallback))
    router.include_router(create_decision_subrouter(fallback))
    return router

