"""P10.20 Scoped Filesystem Permissions & Centralized Assistant Tasks migration.

Revision ID: 0004_p10_20_filesystem_and_tasks
Revises: 0003_p10_12_skills_and_targets
Create Date: 2026-11-12
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0004_p10_20_filesystem_and_tasks"
down_revision: Union[str, Sequence[str], None] = "0003_p10_12_skills_and_targets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())

    if "filesystem_permissions" not in existing:
        op.create_table(
            "filesystem_permissions",
            sa.Column("permission_id", sa.String(36), primary_key=True),
            sa.Column("canonical_folder_path", sa.String(800), nullable=False),
            sa.Column("granted_operations", sa.JSON(), nullable=False),
            sa.Column("scope", sa.String(24), nullable=False, server_default="once"),
            sa.Column("granted_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("expires_at", sa.Float(), nullable=True),
            sa.Column("granted_by", sa.String(64), nullable=False, server_default="owner"),
            sa.Column("revoked_at", sa.Float(), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("purpose", sa.String(400), nullable=False, server_default=""),
            sa.Column("session_id", sa.String(64), nullable=True),
            sa.Column("task_id", sa.String(64), nullable=True),
        )
        op.create_index("ix_fs_perm_folder", "filesystem_permissions", ["canonical_folder_path"])
        op.create_index("ix_fs_perm_status", "filesystem_permissions", ["status"])

    if "filesystem_permission_requests" not in existing:
        op.create_table(
            "filesystem_permission_requests",
            sa.Column("request_id", sa.String(36), primary_key=True),
            sa.Column("task_id", sa.String(64), nullable=True),
            sa.Column("requested_path", sa.String(800), nullable=False),
            sa.Column("canonical_path", sa.String(800), nullable=False),
            sa.Column("requested_operations", sa.JSON(), nullable=False),
            sa.Column("not_requested_operations", sa.JSON(), nullable=False),
            sa.Column("sensitive_flags", sa.JSON(), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False, server_default=""),
            sa.Column("access_duration", sa.String(24), nullable=False, server_default="once"),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
            sa.Column("permission_id", sa.String(36), nullable=True),
            sa.Column("deferred_action", sa.JSON(), nullable=False),
            sa.Column("decided_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("expires_at", sa.Float(), nullable=True),
            sa.Column("decided_at", sa.Float(), nullable=True),
        )
        op.create_index("ix_fs_req_task_id", "filesystem_permission_requests", ["task_id"])
        op.create_index("ix_fs_req_status", "filesystem_permission_requests", ["status"])

    if "assistant_tasks" not in existing:
        op.create_table(
            "assistant_tasks",
            sa.Column("task_id", sa.String(36), primary_key=True),
            sa.Column("user_request", sa.Text(), nullable=False, server_default=""),
            sa.Column("source", sa.String(24), nullable=False, server_default="text"),
            sa.Column("intent", sa.String(64), nullable=False, server_default="general_chat"),
            sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
            sa.Column("plan", sa.JSON(), nullable=False),
            sa.Column("selected_capabilities", sa.JSON(), nullable=False),
            sa.Column("selected_agents", sa.JSON(), nullable=False),
            sa.Column("tools_used", sa.JSON(), nullable=False),
            sa.Column("permissions_requested", sa.JSON(), nullable=False),
            sa.Column("current_step", sa.String(200), nullable=False, server_default=""),
            sa.Column("result", sa.Text(), nullable=False, server_default=""),
            sa.Column("generated_files", sa.JSON(), nullable=False),
            sa.Column("error_info", sa.Text(), nullable=False, server_default=""),
            sa.Column("meta", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("completed_at", sa.Float(), nullable=True),
        )
        op.create_index("ix_assistant_tasks_status", "assistant_tasks", ["status"])
        op.create_index("ix_assistant_tasks_created_at", "assistant_tasks", ["created_at"])


def downgrade() -> None:
    op.drop_table("assistant_tasks")
    op.drop_table("filesystem_permission_requests")
    op.drop_table("filesystem_permissions")

