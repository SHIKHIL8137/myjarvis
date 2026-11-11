"""Modular v2 REST API Router for the provider-independent Agent Skills & Capabilities Manager."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_deps import make_assistant_dep
from .skills.discovery import discover_curated_catalog, discover_local_skills
from .skills.registry import SkillValidationError


class SkillDiscoverIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=600)


class SkillValidateIn(BaseModel):
    source_dir: str = Field(..., min_length=1, max_length=600)
    skill_id: str | None = None
    category: str = "general"
    source_repo: str = ""
    source_commit: str = ""
    license_override: str = ""
    expected_sha256: str = ""


class SkillInstallIn(SkillValidateIn):
    compatible_roles: list[str] | None = None
    approved: bool = False
    approval_token: str | None = None


class SkillUpdateIn(BaseModel):
    source_dir: str = Field(..., min_length=1, max_length=600)
    version: str = Field(..., min_length=1, max_length=64)
    source_commit: str = ""
    expected_sha256: str = ""
    approved: bool = False
    approval_token: str | None = None


class SkillAssignIn(BaseModel):
    agent_role: str = Field(..., min_length=2, max_length=64)
    project_id: str | None = None
    priority: int = Field(10, ge=0, le=100)


class SkillRollbackIn(BaseModel):
    target_version: str | None = None


class SkillSelectPreviewIn(BaseModel):
    task_prompt: str = Field(..., min_length=1, max_length=2000)
    agent_role: str = "coder_agent"
    department: str = "dev"
    runtime: str = "jarvis_native"
    project_id: str | None = None


def create_skills_subrouter(fallback=None) -> APIRouter:
    router = APIRouter()
    get_a = make_assistant_dep(fallback)

    @router.get("/skills")
    async def list_skills(category: str | None = None, q: str | None = None, a=Depends(get_a)):
        reg = a.orchestrator.skills
        items = reg.search_skills(q) if q else reg.list_skills(category=category)
        metrics = reg.skill_metrics_summary()
        assignments = reg.list_assignments()
        by_skill_assign: dict[str, list[dict]] = {}
        for asn in assignments:
            by_skill_assign.setdefault(asn["skill_id"], []).append(asn)
        for it in items:
            it["metrics"] = metrics.get(it["skill_id"]) or {"invocations": 0, "quality_assessment": "inconclusive"}
            it["assignments"] = by_skill_assign.get(it["skill_id"]) or []
        return {
            "skills": items,
            "catalog": discover_curated_catalog(),
            "audit": reg.recent_audit(limit=30),
        }

    @router.get("/skills/catalog/{source_id}")
    async def get_catalog_audit(source_id: str):
        for item in discover_curated_catalog():
            if item["source_id"] == source_id:
                return item
        raise HTTPException(404, f"Unknown catalog source {source_id!r}")

    @router.post("/skills/discover")
    async def discover_skills(body: SkillDiscoverIn, a=Depends(get_a)):
        return {
            "discovered": discover_local_skills(
                [Path(body.path)],
                allowed_licenses={x.lower() for x in a.settings.skills.allowed_licenses},
            )
        }

    @router.post("/skills/validate")
    async def validate_skill(body: SkillValidateIn, a=Depends(get_a)):
        try:
            manifest, report = a.orchestrator.skills.validate_candidate(
                Path(body.source_dir),
                skill_id=body.skill_id,
                category=body.category,
                source_repo=body.source_repo,
                source_commit=body.source_commit,
                license_override=body.license_override,
                expected_sha256=body.expected_sha256,
            )
            return {"manifest": manifest.model_dump(), "validation_report": report}
        except SkillValidationError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/skills/install/request")
    async def request_skill_install(body: SkillInstallIn, a=Depends(get_a)):
        try:
            return a.orchestrator.skills.request_install(
                Path(body.source_dir),
                skill_id=body.skill_id,
                category=body.category,
                source_repo=body.source_repo,
                source_commit=body.source_commit,
                license_override=body.license_override,
                compatible_roles=body.compatible_roles,
                expected_sha256=body.expected_sha256,
            )
        except SkillValidationError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/skills/install")
    async def install_skill(body: SkillInstallIn, a=Depends(get_a)):
        try:
            return a.orchestrator.skills.install_skill(
                Path(body.source_dir),
                skill_id=body.skill_id,
                category=body.category,
                source_repo=body.source_repo,
                source_commit=body.source_commit,
                license_override=body.license_override,
                compatible_roles=body.compatible_roles,
                expected_sha256=body.expected_sha256,
                approved=body.approved,
                approval_token=body.approval_token,
                actor="ui_user",
            )
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except SkillValidationError as e:
            raise HTTPException(400, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/skills/{skill_id}/enable")
    async def enable_skill(skill_id: str, a=Depends(get_a)):
        try:
            return {"skill": a.orchestrator.skills.enable_skill(skill_id, actor="ui_user")}
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")

    @router.post("/skills/{skill_id}/disable")
    async def disable_skill(skill_id: str, a=Depends(get_a)):
        try:
            return {"skill": a.orchestrator.skills.disable_skill(skill_id, actor="ui_user")}
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")

    @router.post("/skills/{skill_id}/update")
    async def update_skill(skill_id: str, body: SkillUpdateIn, a=Depends(get_a)):
        try:
            return a.orchestrator.skills.update_skill(
                skill_id,
                Path(body.source_dir),
                version=body.version,
                source_commit=body.source_commit,
                expected_sha256=body.expected_sha256,
                approved=body.approved,
                approval_token=body.approval_token,
                actor="ui_user",
            )
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except SkillValidationError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/skills/{skill_id}/rollback")
    async def rollback_skill(skill_id: str, body: SkillRollbackIn | None = None, a=Depends(get_a)):
        b = body or SkillRollbackIn()
        try:
            return {"skill": a.orchestrator.skills.rollback_skill(skill_id, target_version=b.target_version, actor="ui_user")}
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")
        except (ValueError, SkillValidationError) as e:
            raise HTTPException(400, str(e)) from e

    @router.delete("/skills/{skill_id}")
    async def uninstall_skill(skill_id: str, a=Depends(get_a)):
        try:
            return a.orchestrator.skills.uninstall_skill(skill_id, actor="ui_user")
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")

    @router.post("/skills/{skill_id}/assign")
    async def assign_skill(skill_id: str, body: SkillAssignIn, a=Depends(get_a)):
        try:
            return {"assignment": a.orchestrator.skills.assign_skill(skill_id, agent_role=body.agent_role, project_id=body.project_id, priority=body.priority, actor="ui_user")}
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")
        except SkillValidationError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/skills/{skill_id}/unassign")
    async def unassign_skill(skill_id: str, body: SkillAssignIn, a=Depends(get_a)):
        return a.orchestrator.skills.unassign_skill(skill_id, agent_role=body.agent_role, project_id=body.project_id, actor="ui_user")

    @router.get("/skills/{skill_id}/history")
    async def get_skill_history(skill_id: str, a=Depends(get_a)):
        try:
            sk = a.orchestrator.skills.get_skill(skill_id)
            return {"skill": sk, "history": a.orchestrator.skills.skill_history(skill_id)}
        except KeyError:
            raise HTTPException(404, f"Skill {skill_id!r} not found")

    @router.post("/skills/select-preview")
    async def preview_skill_selection(body: SkillSelectPreviewIn, a=Depends(get_a)):
        return a.orchestrator.capability_router.select_skills_for_task(
            task_prompt=body.task_prompt,
            department=body.department,
            agent_role=body.agent_role,
            runtime=body.runtime,
            project_id=body.project_id,
        )

    return router

