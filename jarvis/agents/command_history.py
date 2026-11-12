"""Unified persisted Command History & Continuity Service backed by CommandHistoryRow."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import desc, select

from ..db.engine import DatabaseManager
from ..db.models import CommandHistoryRow


class CommandHistoryService:
    """Records executed user commands, outcomes, tools/paths used, and supports safe replay & continuity queries."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(str(text or "").strip().lower().split())[:400]

    @staticmethod
    def _to_dict(row: CommandHistoryRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "raw_text": row.raw_text,
            "normalized_command": row.normalized_command,
            "task_category": row.task_category,
            "intent": row.intent,
            "department": row.department,
            "task_id": row.task_id,
            "project_alias": row.project_alias,
            "target_path": row.target_path,
            "status": row.status,
            "tools_used": list(row.tools_used or []),
            "paths_touched": list(row.paths_touched or []),
            "result_summary": row.result_summary or "",
            "error_info": row.error_info or "",
            "replayable": bool(row.replayable),
            "replay_count": int(row.replay_count or 0),
            "source": row.source,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
        }

    async def record(
        self,
        raw_text: str,
        *,
        task_category: str = "informational",
        intent: str = "general_chat",
        department: str = "assistant",
        task_id: str | None = None,
        project_alias: str | None = None,
        target_path: str | None = None,
        status: str = "completed",
        tools_used: list[str] | None = None,
        paths_touched: list[str] | None = None,
        result_summary: str = "",
        error_info: str = "",
        replayable: bool = True,
        source: str = "text",
    ) -> dict[str, Any]:
        now = time.time()
        done_flag = status in ("completed", "failed", "cancelled")
        row = CommandHistoryRow(
            raw_text=str(raw_text or "")[:4000],
            normalized_command=self._normalize(raw_text),
            task_category=str(task_category or "informational")[:48],
            intent=str(intent or "general_chat")[:64],
            department=str(department or "assistant")[:32],
            task_id=task_id,
            project_alias=(str(project_alias)[:120] if project_alias else None),
            target_path=(str(target_path)[:800] if target_path else None),
            status=str(status or "completed")[:32],
            tools_used=list(tools_used or [])[:30],
            paths_touched=list(paths_touched or [])[:40],
            result_summary=str(result_summary or "")[:2000],
            error_info=str(error_info or "")[:1000],
            replayable=bool(replayable),
            replay_count=0,
            source=str(source or "text")[:24],
            created_at=now,
            updated_at=now,
            completed_at=now if done_flag else None,
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return self._to_dict(row)

    async def update_by_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        tools_used: list[str] | None = None,
        paths_touched: list[str] | None = None,
        result_summary: str | None = None,
        error_info: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = (
                await s.execute(
                    select(CommandHistoryRow).where(CommandHistoryRow.task_id == task_id).order_by(desc(CommandHistoryRow.created_at)).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if status is not None:
                row.status = str(status)[:32]
                if status in ("completed", "failed", "cancelled"):
                    row.completed_at = time.time()
            if tools_used is not None:
                row.tools_used = list(tools_used)[:30]
            if paths_touched is not None:
                row.paths_touched = list(paths_touched)[:40]
            if result_summary is not None:
                row.result_summary = str(result_summary)[:2000]
            if error_info is not None:
                row.error_info = str(error_info)[:1000]
            row.updated_at = time.time()
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

    async def list_recent(self, *, limit: int = 25, status: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 200))
        async with self.db.async_session() as s:
            stmt = select(CommandHistoryRow).order_by(desc(CommandHistoryRow.created_at)).limit(cap * 2 if query else cap)
            if status:
                stmt = stmt.where(CommandHistoryRow.status == status)
            rows = (await s.execute(stmt)).scalars().all()
        items = [self._to_dict(r) for r in rows]
        if query:
            q_low = query.strip().lower()
            items = [it for it in items if q_low in it["raw_text"].lower() or q_low in it["intent"].lower() or q_low in it["result_summary"].lower()]
        return items[:cap]

    async def get_by_id(self, cmd_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = await s.get(CommandHistoryRow, cmd_id)
            return self._to_dict(row) if row is not None else None

    async def get_last_replayable(self) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = (
                await s.execute(
                    select(CommandHistoryRow)
                    .where(CommandHistoryRow.replayable.is_(True))
                    .order_by(desc(CommandHistoryRow.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    async def get_latest(self) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = (
                await s.execute(select(CommandHistoryRow).order_by(desc(CommandHistoryRow.created_at)).limit(1))
            ).scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    async def increment_replay(self, cmd_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            row = await s.get(CommandHistoryRow, cmd_id)
            if row is None:
                return None
            row.replay_count = int(row.replay_count or 0) + 1
            row.updated_at = time.time()
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

