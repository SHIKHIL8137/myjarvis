"""Undo and Action Execution Journaling Service for P5.

Tracks reversible snapshots for filesystem and state mutations, verifies before/after state,
checks external-modification conflicts and expiration, and executes clean rollbacks.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from sqlalchemy import select

from .core.errors import UndoConflictError
from .db.engine import DatabaseManager
from .db.models import ActionExecutionRow, UndoEntryRow

log = logging.getLogger("jarvis.undo")

REVERSIBLE_ACTIONS = frozenset({"create_file", "modify_file", "delete_file", "write_file", "move_file"})


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


class UndoService:
    """Manages reversible action snapshots, external modification conflict checks, and rollback."""

    def __init__(self, db: DatabaseManager, *, default_ttl_s: int = 86400):
        self.db = db
        self.default_ttl_s = default_ttl_s

    async def record_snapshot(
        self,
        *,
        action_type: str,
        target_path: str = "",
        resource_id: str = "",
        before_bytes: bytes | None = None,
        after_bytes: bytes | None = None,
        snapshot_data: dict[str, Any] | None = None,
        reason: str = "",
        ttl_s: int | None = None,
    ) -> dict[str, Any]:
        """Record a reversible pre-action state snapshot."""
        uid = uuid.uuid4().hex
        now = time.time()
        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        expires_at = now + ttl if ttl > 0 else None

        before_sha = sha256_of_bytes(before_bytes) if before_bytes is not None else ""
        after_sha = sha256_of_bytes(after_bytes) if after_bytes is not None else ""

        payload_data = dict(snapshot_data or {})
        if before_bytes is not None and "before_b64" not in payload_data:
            payload_data["before_b64"] = base64.b64encode(before_bytes).decode("ascii")

        row = UndoEntryRow(
            id=uid,
            action_type=action_type,
            target_path=target_path,
            resource_id=resource_id,
            status="available" if action_type in REVERSIBLE_ACTIONS else "non_reversible",
            reversible=action_type in REVERSIBLE_ACTIONS,
            before_sha256=before_sha,
            after_sha256=after_sha,
            snapshot_data=payload_data,
            reason=reason,
            created_at=now,
            expires_at=expires_at,
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

    async def list_undo_entries(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            q = select(UndoEntryRow)
            if status:
                q = q.where(UndoEntryRow.status == status)
            rows = (await s.execute(q.order_by(UndoEntryRow.created_at.desc()).limit(limit))).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def get_undo_entry(self, undo_id: str) -> dict[str, Any] | None:
        async with self.db.async_session() as s:
            r = await s.get(UndoEntryRow, undo_id)
            return self._to_dict(r) if r else None

    async def apply_undo(self, undo_id: str) -> dict[str, Any]:
        """Perform verified rollback of the specified action snapshot."""
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(UndoEntryRow, undo_id)
            if row is None:
                raise KeyError(f"Undo entry {undo_id} not found")

            if row.status == "undone":
                raise UndoConflictError(f"Action {undo_id} was already undone", details={"undo_id": undo_id})
            if not row.reversible or row.status == "non_reversible" or row.action_type not in REVERSIBLE_ACTIONS:
                row.status = "non_reversible"
                await s.commit()
                raise UndoConflictError(f"Action '{row.action_type}' is non-reversible", details={"undo_id": undo_id})
            if row.expires_at is not None and row.expires_at <= now:
                row.status = "expired"
                await s.commit()
                raise UndoConflictError(f"Undo entry {undo_id} has expired", details={"undo_id": undo_id})

            act = row.action_type
            snap = dict(row.snapshot_data or {})

            if act in REVERSIBLE_ACTIONS:
                if act == "move_file":
                    src_p = Path(str(snap.get("src_path") or row.target_path))
                    dst_p = Path(str(snap.get("dst_path") or row.target_path))
                    if not dst_p.exists():
                        row.status = "conflict"
                        await s.commit()
                        raise UndoConflictError(f"Moved target {dst_p.name} no longer exists for rollback")
                    if row.after_sha256:
                        cur_sha = sha256_of_file(dst_p)
                        if cur_sha and cur_sha != row.after_sha256:
                            row.status = "conflict"
                            await s.commit()
                            raise UndoConflictError(f"Moved file {dst_p.name} was modified externally; rollback aborted")
                    src_p.parent.mkdir(parents=True, exist_ok=True)
                    dst_p.replace(src_p)
                else:
                    tp = Path(row.target_path) if row.target_path else None
                    if tp is None:
                        raise UndoConflictError("Missing target file path in undo snapshot")

                    # External modification / conflict check:
                    if row.after_sha256:
                        current_sha = sha256_of_file(tp)
                        if current_sha and current_sha != row.after_sha256:
                            row.status = "conflict"
                            await s.commit()
                            raise UndoConflictError(
                                f"File {tp.name} was modified externally since action executed; rollback aborted.",
                                details={"current_sha256": current_sha, "expected_sha256": row.after_sha256},
                            )

                    # Revert file content (byte-exact across CRLF/LF hosts)
                    if act == "create_file":
                        if tp.exists():
                            tp.unlink()
                    elif act in ("modify_file", "write_file"):
                        if "before_b64" in snap:
                            tp.write_bytes(base64.b64decode(snap["before_b64"]))
                        elif snap.get("before_content") is not None:
                            tp.write_bytes(str(snap["before_content"]).encode("utf-8"))
                        elif "before_hex" in snap:
                            tp.write_bytes(bytes.fromhex(snap["before_hex"]))
                    elif act == "delete_file":
                        tp.parent.mkdir(parents=True, exist_ok=True)
                        if "before_b64" in snap:
                            tp.write_bytes(base64.b64decode(snap["before_b64"]))
                        elif snap.get("before_content") is not None:
                            tp.write_bytes(str(snap["before_content"]).encode("utf-8"))
                        elif "before_hex" in snap:
                            tp.write_bytes(bytes.fromhex(snap["before_hex"]))

            row.status = "undone"
            row.undone_at = now
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

    async def record_execution(
        self,
        *,
        action_type: str,
        target: str = "",
        permission_level: str = "READ",
        status: str = "started",
        verified: bool = False,
        summary: str = "",
        checks: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
        undo_id: str | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Record an execution step into the action executions journal."""
        eid = uuid.uuid4().hex
        row = ActionExecutionRow(
            id=eid,
            action_type=action_type,
            target=target,
            permission_level=permission_level,
            status=status,
            verified=verified,
            summary=summary,
            checks=checks or [],
            evidence=evidence or {},
            undo_id=undo_id,
            error=error,
            created_at=time.time(),
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return {
                "id": row.id,
                "action_type": row.action_type,
                "target": row.target,
                "permission_level": row.permission_level,
                "status": row.status,
                "verified": row.verified,
                "summary": row.summary,
                "checks": row.checks,
                "undo_id": row.undo_id,
                "error": row.error,
                "created_at": row.created_at,
            }

    async def list_executions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            rows = (
                await s.execute(select(ActionExecutionRow).order_by(ActionExecutionRow.created_at.desc()).limit(limit))
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "action_type": r.action_type,
                    "target": r.target,
                    "permission_level": r.permission_level,
                    "status": r.status,
                    "verified": r.verified,
                    "summary": r.summary,
                    "checks": list(r.checks or []),
                    "evidence": dict(r.evidence or {}),
                    "undo_id": r.undo_id,
                    "error": r.error,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    @staticmethod
    def _to_dict(row: UndoEntryRow) -> dict[str, Any]:
        return {
            "undo_id": row.id,
            "action_type": row.action_type,
            "target_path": row.target_path,
            "resource_id": row.resource_id,
            "status": row.status,
            "reversible": row.reversible,
            "before_sha256": row.before_sha256,
            "after_sha256": row.after_sha256,
            "reason": row.reason,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "undone_at": row.undone_at,
        }
