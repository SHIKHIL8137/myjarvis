"""Add P5 undo_entries and action_executions tables.

Revision ID: 0011_p5_undo_journal
Revises: 0010_p4_routines_and_proactive
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0011_p5_undo_journal"
down_revision = "0010_p4_routines_and_proactive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing: set[str] = set()
    else:
        bind = op.get_bind()
        existing = set(sa.inspect(bind).get_table_names())

    if "undo_entries" not in existing:
        op.create_table(
            "undo_entries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("target_path", sa.String(800), nullable=False, server_default=""),
            sa.Column("resource_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="available"),
            sa.Column("reversible", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("before_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("after_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("snapshot_data", sa.JSON(), nullable=False),
            sa.Column("reason", sa.String(400), nullable=False, server_default=""),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("expires_at", sa.Float(), nullable=True),
            sa.Column("undone_at", sa.Float(), nullable=True),
        )
        op.create_index("ix_undo_entries_action_type", "undo_entries", ["action_type"])
        op.create_index("ix_undo_entries_target_path", "undo_entries", ["target_path"])
        op.create_index("ix_undo_entries_status", "undo_entries", ["status"])
        op.create_index("ix_undo_entries_created_at", "undo_entries", ["created_at"])

    if "action_executions" not in existing:
        op.create_table(
            "action_executions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("target", sa.String(800), nullable=False, server_default=""),
            sa.Column("permission_level", sa.String(32), nullable=False, server_default="READ"),
            sa.Column("status", sa.String(32), nullable=False, server_default="started"),
            sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("checks", sa.JSON(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("undo_id", sa.String(36), nullable=True),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_action_executions_action_type", "action_executions", ["action_type"])
        op.create_index("ix_action_executions_status", "action_executions", ["status"])
        op.create_index("ix_action_executions_created_at", "action_executions", ["created_at"])


def downgrade() -> None:
    if context.is_offline_mode():
        existing = {"action_executions", "undo_entries"}
    else:
        bind = op.get_bind()
        existing = set(sa.inspect(bind).get_table_names())

    if "action_executions" in existing:
        op.drop_table("action_executions")
    if "undo_entries" in existing:
        op.drop_table("undo_entries")

