"""Persistent unified READ / WRITE / EXECUTE permission matrix and approval queue backed by SQLAlchemy."""
import time
import uuid

from ..db.engine import DatabaseManager
from ..db.models import ApprovalRequestRow

ACTION_RISK_TIERS: dict[str, str] = {
    # READ (Auto-allowed, audited)
    "memory_read": "READ",
    "search_files": "READ",
    "read_file": "READ",
    "inspect_project": "READ",
    "graphify_query": "READ",
    "graphify_affected": "READ",
    "reminders_list": "READ",
    "email_summarize": "READ",
    # WRITE (Policy / scoped auto or confirm)
    "create_note": "WRITE",
    "create_document": "WRITE",
    "create_reminder": "WRITE",
    "email_draft": "WRITE",
    "graphify_update": "WRITE",
    "worktree_edit": "WRITE",
    "worktree_test": "WRITE",
    "create_file": "WRITE",
    # EXECUTE (Mandatory human approval / LangGraph interrupt)
    "modify_file": "EXECUTE",
    "move_file": "EXECUTE",
    "delete_file": "EXECUTE",
    "open_app": "EXECUTE",
    "run_command": "EXECUTE",
    "email_send": "EXECUTE",
    "merge_worktree": "EXECUTE",
    "db_migrate": "EXECUTE",
    "start_training": "EXECUTE",
    "promote_model": "EXECUTE",
    "rollback_model": "EXECUTE",
    "restore_backup": "EXECUTE",
    "restore_production_database": "EXECUTE",
    "install_skill": "EXECUTE",
    "update_skill": "EXECUTE",
    "uninstall_skill": "EXECUTE",
}


class ApprovalService:
    """Durable DB-backed approval queue surviving FastAPI restarts."""

    def __init__(self, db: DatabaseManager, default_ttl_s: float = 900.0):
        self.db = db
        self.default_ttl_s = default_ttl_s

    @staticmethod
    def risk_for(action_type: str) -> str:
        return ACTION_RISK_TIERS.get(action_type, "EXECUTE")

    def create_request(
        self,
        *,
        action_type: str,
        target_resource: str = "",
        preview_diff: str = "",
        args: dict | None = None,
        run_id: str | None = None,
        ttl_s: float | None = None,
    ) -> dict:
        tok = uuid.uuid4().hex[:12]
        now = round(time.time(), 3)
        exp = now + float(ttl_s or self.default_ttl_s)
        tier = self.risk_for(action_type)
        with self.db.sync_session() as s:
            row = ApprovalRequestRow(
                token=tok,
                run_id=run_id,
                action_type=action_type,
                risk_tier=tier,
                target_resource=target_resource[:500],
                preview_diff=preview_diff[:30_000],
                args=args or {},
                status="pending",
                created_at=now,
                expires_at=exp,
            )
            s.add(row)
            s.commit()
            return self._to_dict(row)

    def list_pending(self) -> list[dict]:
        now = time.time()
        with self.db.sync_session() as s:
            rows = (
                s.query(ApprovalRequestRow)
                .filter(ApprovalRequestRow.status == "pending")
                .order_by(ApprovalRequestRow.created_at.desc())
                .all()
            )
            out: list[dict] = []
            expired_any = False
            for r in rows:
                if r.expires_at < now:
                    r.status = "expired"
                    expired_any = True
                else:
                    out.append(self._to_dict(r))
            if expired_any:
                s.commit()
            return out

    def decide(self, token: str, *, approved: bool, decided_by: str = "owner") -> dict:
        with self.db.sync_session() as s:
            row = s.get(ApprovalRequestRow, token)
            if not row:
                raise KeyError(token)
            if row.status != "pending":
                raise ValueError(f"Approval {token} already {row.status}")
            if time.time() > row.expires_at:
                row.status = "expired"
                s.commit()
                raise TimeoutError(f"Approval {token} expired")
            row.status = "approved" if approved else "rejected"
            row.decided_by = decided_by
            row.decided_at = round(time.time(), 3)
            s.commit()
            return self._to_dict(row)

    def cancel_for_run(self, run_id: str, *, decided_by: str = "system_cancel") -> int:
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            rows = (
                s.query(ApprovalRequestRow)
                .filter(ApprovalRequestRow.run_id == run_id, ApprovalRequestRow.status == "pending")
                .all()
            )
            for row in rows:
                row.status = "cancelled"
                row.decided_by = decided_by
                row.decided_at = now
            if rows:
                s.commit()
            return len(rows)

    def get(self, token: str) -> dict:
        with self.db.sync_session() as s:
            row = s.get(ApprovalRequestRow, token)
            if not row:
                raise KeyError(token)
            return self._to_dict(row)

    def consume_approved(self, token: str, *, expected_action_type: str | None = None) -> dict:
        """Atomically verify and mark a previously approved token consumed so it cannot be replayed."""
        with self.db.sync_session() as s:
            row = s.get(ApprovalRequestRow, token)
            if not row:
                raise KeyError(token)
            if expected_action_type and row.action_type != expected_action_type:
                raise PermissionError(f"Approval token {token} is for {row.action_type!r}, expected {expected_action_type!r}.")
            if row.status != "approved":
                raise PermissionError(f"Approval token {token} status is {row.status!r}, expected 'approved'.")
            if time.time() > float(row.expires_at):
                row.status = "expired"
                s.commit()
                raise TimeoutError(f"Approval token {token} expired.")
            row.status = "consumed"
            s.commit()
            return self._to_dict(row)

    @staticmethod
    def _to_dict(r: ApprovalRequestRow) -> dict:
        return {
            "token": r.token,
            "run_id": r.run_id,
            "action_type": r.action_type,
            "risk_tier": r.risk_tier,
            "target_resource": r.target_resource,
            "preview_diff": r.preview_diff,
            "args": r.args or {},
            "status": r.status,
            "decided_by": r.decided_by,
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "decided_at": r.decided_at,
        }

