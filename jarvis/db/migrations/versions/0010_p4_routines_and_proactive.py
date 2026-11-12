"""Add P4 unified routines, routine_runs, and proactive_dismissals tables.

Revision ID: 0010_p4_routines_and_proactive
Revises: 0009_p1_task_engine_fields
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0010_p4_routines_and_proactive"
down_revision = "0009_p1_task_engine_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing: set[str] = set()
    else:
        bind = op.get_bind()
        existing = set(sa.inspect(bind).get_table_names())

    if "routines" not in existing:
        op.create_table(
            "routines",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("description", sa.String(600), nullable=False, server_default=""),
            sa.Column("trigger_type", sa.String(32), nullable=False, server_default="once"),
            sa.Column("trigger_spec", sa.JSON(), nullable=False),
            sa.Column("action_type", sa.String(64), nullable=False, server_default="notification"),
            sa.Column("action_payload", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("late_policy", sa.String(32), nullable=False, server_default="run_once_if_within_grace"),
            sa.Column("grace_period_s", sa.Integer(), nullable=False, server_default="3600"),
            sa.Column("channel", sa.String(32), nullable=False, server_default="voice+ui"),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("last_run_at", sa.Float(), nullable=True),
            sa.Column("next_run_at", sa.Float(), nullable=True),
            sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("meta", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_routines_trigger_type", "routines", ["trigger_type"])
        op.create_index("ix_routines_enabled", "routines", ["enabled"])
        op.create_index("ix_routines_next_run_at", "routines", ["next_run_at"])

    if "routine_runs" not in existing:
        op.create_table(
            "routine_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("routine_id", sa.String(36), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
            sa.Column("scheduled_for", sa.Float(), nullable=True),
            sa.Column("started_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("completed_at", sa.Float(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("task_id", sa.String(36), nullable=True),
            sa.Column("meta", sa.JSON(), nullable=False),
        )
        op.create_index("ix_routine_runs_routine_id", "routine_runs", ["routine_id"])
        op.create_index("ix_routine_runs_idempotency_key", "routine_runs", ["idempotency_key"], unique=True)

    if "proactive_dismissals" not in existing:
        op.create_table(
            "proactive_dismissals",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("dismissed_until", sa.Float(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_proactive_dismissals_dismissed_until", "proactive_dismissals", ["dismissed_until"])


def downgrade() -> None:
    if context.is_offline_mode():
        existing = {"proactive_dismissals", "routine_runs", "routines"}
    else:
        bind = op.get_bind()
        existing = set(sa.inspect(bind).get_table_names())

    if "proactive_dismissals" in existing:
        op.drop_table("proactive_dismissals")
    if "routine_runs" in existing:
        op.drop_table("routine_runs")
    if "routines" in existing:
        op.drop_table("routines")

