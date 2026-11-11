"""API router for personal memory management, disk history search, safe filesystem search/docs,
permission-gated computer actions, task tracking, and Piper runtime voice controls."""
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .actions import ActionPermissionError
from .config import save_overrides
from .filesystem import FilesystemPermissionError
from .memory_store import MEMORY_CATEGORIES, infer_memory_category


class MemoryRecordIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    category: str | None = None
    topic: str = ""
    title: str = ""
    confirm_inferred: bool = False


class MemoryUpdateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class MemoryRestoreIn(BaseModel):
    backup_file: str = Field(..., min_length=1)


class VoiceSwitchIn(BaseModel):
    voice: str = Field(..., min_length=1, max_length=120)


class VoiceRateIn(BaseModel):
    speed: float = Field(..., ge=0.6, le=1.8)


class FileWriteIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=400)
    content: str = Field(..., max_length=512_000)
    overwrite: bool = False
    confirmed_token: str | None = None


class ActionExecuteIn(BaseModel):
    action: str = Field(..., min_length=1, max_length=80)
    args: dict = Field(default_factory=dict)
    confirmed_token: str | None = None


def create_workspace_router(a) -> APIRouter:
    router = APIRouter()

    # ---- Voices (Phase 13) ---------------------------------------------------------------------------
    @router.get("/api/voices")
    async def list_voices():
        voices = a.tts.list_voices()
        return {
            "active": a.settings.tts.voice,
            "speed": a.settings.tts.speed,
            "enabled": a.settings.tts.enabled,
            "health": a.tts.health(),
            "voices": voices,
        }

    @router.post("/api/voices/switch")
    async def switch_voice(body: VoiceSwitchIn):
        switched = a.tts.switch_voice(body.voice)
        a.settings.tts.voice = switched["name"]
        save_overrides({"tts": {"voice": switched["name"]}})
        a.bus.publish({"type": "voice_changed", **switched})
        return {"ok": True, "voice": switched}

    @router.post("/api/voices/rate")
    async def set_voice_rate(body: VoiceRateIn):
        applied = a.tts.set_rate(body.speed)
        a.settings.tts.speed = applied
        save_overrides({"tts": {"speed": applied}})
        return {"ok": True, "speed": applied}

    # ---- Structured Personal Memory & Full History Search (Phase 2) ----------------------------------
    @router.get("/api/memory/items")
    async def memory_items(category: str | None = None, include_pending: bool = True, q: str | None = None):
        if category and category not in MEMORY_CATEGORIES:
            raise HTTPException(422, f"Unsupported memory category '{category}'")
        if q:
            hits = a.memory_store.search(q, category=category, limit=25)
            return {"items": [{**r.to_dict(), "score": s} for r, s in hits]}
        return {"items": [r.to_dict() for r in a.memory_store.list_all(category=category, include_pending=include_pending)]}

    @router.post("/api/memory/items")
    async def create_memory_item(body: MemoryRecordIn):
        inferred_cat, inferred_topic = infer_memory_category(body.text)
        cat = body.category or inferred_cat
        if cat not in MEMORY_CATEGORIES:
            raise HTTPException(422, f"Unsupported memory category '{cat}'")
        if body.confirm_inferred:
            rec = a.memory_store.propose_inferred(body.text, category=cat)
            return {"record": rec.to_dict(), "created": True}
        rec, created = a.memory_store.add(body.text, category=cat, topic=body.topic or inferred_topic, title=body.title)
        return {"record": rec.to_dict(), "created": created}

    @router.put("/api/memory/items/{rec_id}")
    async def update_memory_item(rec_id: str, body: MemoryUpdateIn):
        rec = a.memory_store.update(rec_id, body.text)
        return {"record": rec.to_dict()}

    @router.post("/api/memory/items/{rec_id}/confirm")
    async def confirm_memory_item(rec_id: str, approved: bool = True):
        rec = a.memory_store.confirm_pending(rec_id, approved=approved)
        return {"ok": True, "record": rec.to_dict() if rec else None}

    @router.delete("/api/memory/items/{rec_id}")
    async def delete_memory_item(rec_id: str):
        rec = a.memory_store.delete(rec_id)
        return {"deleted": rec.to_dict()}

    @router.post("/api/memory/backup")
    async def backup_memory():
        p = a.memory_store.backup()
        return {"ok": True, "backup_file": str(p)}

    @router.get("/api/memory/export")
    async def export_memory():
        return a.memory_store.export_data()

    @router.post("/api/memory/restore")
    async def restore_memory(body: MemoryRestoreIn):
        count = a.memory_store.restore(Path(body.backup_file))
        return {"ok": True, "restored": count}

    @router.get("/api/history/search")
    async def history_search(q: str = "", days: int | None = None, limit: int = 15):
        return {"results": a.history.search_disk(q, days=days, limit=limit)}

    # ---- Safe Filesystem & Codebase Inspection (Phase 11 & 12) ---------------------------------------
    @router.get("/api/files/roots")
    async def file_roots():
        return {"roots": [str(p) for p in a.filesystem.allowed_roots()], "allow_write": a.settings.filesystem.allow_write}

    @router.get("/api/files/search")
    async def files_search(q: str = "", ext: str | None = None, content: str | None = None, folders_only: bool = False):
        return {"matches": a.filesystem.search(q, ext=ext, content_query=content, folders_only=folders_only)}

    @router.get("/api/files/read")
    async def files_read(path: str, max_chars: int = 8000):
        return a.filesystem.read_document(path, max_chars=max_chars)

    @router.get("/api/files/inspect")
    async def files_inspect(path: str | None = None):
        return a.filesystem.inspect_project(path)

    @router.post("/api/files/write")
    async def files_write(body: FileWriteIn):
        action = "modify_file" if body.overwrite else "create_file"
        return a.actions.request_or_execute(
            action,
            {"path": body.path, "content": body.content},
            confirmed_token=body.confirmed_token,
        )

    # ---- Permission-Gated Actions & Task Tracking (Phase 14 & 15) ------------------------------------
    @router.get("/api/actions/pending")
    async def actions_pending():
        return {"pending": a.actions.pending_approvals()}

    @router.post("/api/actions/execute")
    async def actions_execute(body: ActionExecuteIn):
        try:
            return a.actions.request_or_execute(body.action, body.args, confirmed_token=body.confirmed_token)
        except ActionPermissionError as e:
            raise HTTPException(403, str(e)) from e
        except FilesystemPermissionError as e:
            raise HTTPException(403, str(e)) from e

    @router.post("/api/actions/{token}/confirm")
    async def actions_confirm(token: str):
        return a.actions.confirm(token)

    @router.post("/api/actions/{token}/reject")
    async def actions_reject(token: str):
        return a.actions.reject(token)

    @router.get("/api/tasks")
    async def list_tasks(limit: int = 20):
        return {"tasks": a.actions.tasks.list_recent(limit=limit)}

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        t = a.actions.tasks.cancel(task_id)
        return {"task": t.to_dict()}

    return router
