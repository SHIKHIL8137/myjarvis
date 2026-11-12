"""P10.20 Centralized Assistant Task Lifecycle Service (backed by AssistantTaskRow)."""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from sqlalchemy import select

from ..db.engine import DatabaseManager
from ..db.models import AssistantTaskRow

TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "planning",
    "awaiting_permission",
    "awaiting_approval",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
ACTIVE_TASK_STATUSES: tuple[str, ...] = ("queued", "planning", "awaiting_permission", "awaiting_approval", "running", "paused")


class AssistantTaskService:
    """Manages persistent end-to-end tasks routed through the central Jarvis assistant."""

    def __init__(self, db: DatabaseManager, event_publisher: Callable[[dict[str, Any]], None] | None = None):
        self.db = db
        self._publish = event_publisher

    def set_publisher(self, publisher: Callable[[dict[str, Any]], None] | None) -> None:
        self._publish = publisher

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish({"type": "task_updated", "task": payload})
        except Exception:
            pass

    async def create_task(
        self,
        user_request: str,
        *,
        source: str = "text",
        intent: str = "general_chat",
        status: str = "planning",
        plan: Iterable[Any] | None = None,
        selected_capabilities: Iterable[str] | None = None,
        selected_agents: Iterable[str] | None = None,
        current_step: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        eff_status = status if status in TASK_STATUSES else "queued"
        now = time.time()
        row = AssistantTaskRow(
            user_request=str(user_request or ""),
            source=str(source or "text")[:24],
            intent=str(intent or "general_chat")[:64],
            status=eff_status,
            plan=list(plan or []),
            selected_capabilities=list(selected_capabilities or []),
            selected_agents=list(selected_agents or []),
            tools_used=[],
            permissions_requested=[],
            current_step=str(current_step or "")[:200],
            result="",
            generated_files=[],
            error_info="",
            meta=dict(meta or {}),
            created_at=now,
            updated_at=now,
            completed_at=now if eff_status in TERMINAL_TASK_STATUSES else None,
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        intent: str | None = None,
        plan: Iterable[Any] | None = None,
        selected_capabilities: Iterable[str] | None = None,
        selected_agents: Iterable[str] | None = None,
        tools_used: Iterable[str] | None = None,
        permissions_requested: Iterable[Any] | None = None,
        current_step: str | None = None,
        result: str | None = None,
        generated_files: Iterable[Any] | None = None,
        error_info: str | None = None,
        meta_patch: dict[str, Any] | None = None,
        append_tool: str | None = None,
        append_file: str | dict[str, Any] | None = None,
        append_permission: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not task_id:
            return None
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            if row is None:
                return None
            if status is not None and status in TASK_STATUSES:
                row.status = status
                if status in TERMINAL_TASK_STATUSES and row.completed_at is None:
                    row.completed_at = now
                elif status not in TERMINAL_TASK_STATUSES:
                    row.completed_at = None
            if intent is not None:
                row.intent = str(intent)[:64]
            if plan is not None:
                row.plan = list(plan)
            if selected_capabilities is not None:
                row.selected_capabilities = list(selected_capabilities)
            if selected_agents is not None:
                row.selected_agents = list(selected_agents)
            if tools_used is not None:
                row.tools_used = list(tools_used)
            if append_tool:
                cur_tools = list(row.tools_used or [])
                if append_tool not in cur_tools:
                    cur_tools.append(append_tool)
                row.tools_used = cur_tools
            if permissions_requested is not None:
                row.permissions_requested = list(permissions_requested)
            if append_permission is not None:
                cur_perms = list(row.permissions_requested or [])
                if append_permission not in cur_perms:
                    cur_perms.append(append_permission)
                row.permissions_requested = cur_perms
            if current_step is not None:
                row.current_step = str(current_step)[:200]
            if result is not None:
                row.result = str(result)
            if generated_files is not None:
                row.generated_files = list(generated_files)
            if append_file is not None:
                cur_files = list(row.generated_files or [])
                if append_file not in cur_files:
                    cur_files.append(append_file)
                row.generated_files = cur_files
            if error_info is not None:
                row.error_info = str(error_info)
            if meta_patch:
                merged = dict(row.meta or {})
                merged.update(meta_patch)
                row.meta = merged
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
        data = self._to_dict(row)
        self._emit(data)
        return data

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = await s.get(AssistantTaskRow, task_id)
            return self._to_dict(row) if row is not None else None

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            stmt = select(AssistantTaskRow).order_by(AssistantTaskRow.created_at.desc()).limit(max(1, min(limit, 200)))
            if status:
                stmt = stmt.where(AssistantTaskRow.status == status)
            elif active_only:
                stmt = stmt.where(AssistantTaskRow.status.in_(ACTIVE_TASK_STATUSES))
            rows = (await s.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def cancel_task(self, task_id: str, *, reason: str = "Cancelled by owner") -> dict[str, Any] | None:
        return await self.update_task(
            task_id,
            status="cancelled",
            current_step=reason,
            error_info=reason,
        )

    @staticmethod
    def _to_dict(row: AssistantTaskRow) -> dict[str, Any]:
        return {
            "task_id": row.task_id,
            "user_request": row.user_request,
            "source": row.source,
            "intent": row.intent,
            "status": row.status,
            "plan": list(row.plan or []),
            "selected_capabilities": list(row.selected_capabilities or []),
            "selected_agents": list(row.selected_agents or []),
            "tools_used": list(row.tools_used or []),
            "permissions_requested": list(row.permissions_requested or []),
            "current_step": row.current_step,
            "result": row.result,
            "generated_files": list(row.generated_files or []),
            "error_info": row.error_info,
            "meta": dict(row.meta or {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
        }

