"""Add command_history table for unified command tracking and safe task continuity/replay.

Revision ID: 0008_p11_command_history
Revises: 0007_p11_project_aliases
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0008_p11_command_history"
down_revision = "0007_p11_project_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing_tables: set[str] = set()
    else:
        bind = op.get_bind()
        existing_tables = set(sa.inspect(bind).get_table_names())

    if "command_history" not in existing_tables:
        op.create_table(
            "command_history",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("normalized_command", sa.String(400), nullable=False),
            sa.Column("task_category", sa.String(48), nullable=False, server_default="informational"),
            sa.Column("intent", sa.String(64), nullable=False, server_default="general_chat"),
            sa.Column("department", sa.String(32), nullable=False, server_default="assistant"),
            sa.Column("task_id", sa.String(36), nullable=True),
            sa.Column("project_alias", sa.String(120), nullable=True),
            sa.Column("target_path", sa.String(800), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
            sa.Column("tools_used", sa.JSON(), nullable=False),
            sa.Column("paths_touched", sa.JSON(), nullable=False),
            sa.Column("result_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("error_info", sa.Text(), nullable=False, server_default=""),
            sa.Column("replayable", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source", sa.String(24), nullable=False, server_default="text"),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("completed_at", sa.Float(), nullable=True),
        )
        op.create_index("ix_command_history_normalized_command", "command_history", ["normalized_command"], unique=False)
        op.create_index("ix_command_history_status", "command_history", ["status"], unique=False)
        op.create_index("ix_command_history_task_id", "command_history", ["task_id"], unique=False)
        op.create_index("ix_command_history_created_at", "command_history", ["created_at"], unique=False)


def downgrade() -> None:
    if context.is_offline_mode():
        for ix in (
            "ix_command_history_created_at",
            "ix_command_history_task_id",
            "ix_command_history_status",
            "ix_command_history_normalized_command",
        ):
            op.drop_index(ix, table_name="command_history")
        op.drop_table("command_history")
        return
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "command_history" in insp.get_table_names():
        for idx_info in list(insp.get_indexes("command_history")):
            ix_name = idx_info.get("name")
            if ix_name:
                op.drop_index(str(ix_name), table_name="command_history")
        op.drop_table("command_history")

