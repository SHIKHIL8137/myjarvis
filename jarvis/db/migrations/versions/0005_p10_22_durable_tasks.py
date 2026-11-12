"""Add P10.22 durable background task scheduling, budget, checkpoint, claim, and steering columns.

Revision ID: 0005_p10_22_durable_tasks
Revises: 0004_p10_20_filesystem_and_tasks
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0005_p10_22_durable_tasks"
down_revision = "0004_p10_20_filesystem_and_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing_cols: set[str] = set()
        existing_idx: set[str] = set()
    else:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        existing_cols = {c["name"] for c in insp.get_columns("assistant_tasks")}
        existing_idx = {idx["name"] for idx in insp.get_indexes("assistant_tasks")}

    new_columns: list[sa.Column] = [
        sa.Column("worker_kind", sa.String(48), nullable=False, server_default="assistant"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("not_before", sa.Float(), nullable=True),
        sa.Column("next_retry_at", sa.Float(), nullable=True),
        sa.Column("deadline_at", sa.Float(), nullable=True),
        sa.Column("step_budget", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("steps_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_budget", sa.Integer(), nullable=False, server_default="120000"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_budget", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("tools_used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quota_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("checkpoint_json", sa.JSON(), nullable=True),
        sa.Column("steer_notes", sa.JSON(), nullable=True),
        sa.Column("completed_step_keys", sa.JSON(), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.Float(), nullable=True),
    ]

    with op.batch_alter_table("assistant_tasks") as batch:
        for col in new_columns:
            if col.name not in existing_cols:
                batch.add_column(col)

    idx_defs = [
        ("ix_assistant_tasks_worker_kind", ["worker_kind"]),
        ("ix_assistant_tasks_not_before", ["not_before"]),
        ("ix_assistant_tasks_claimed_by", ["claimed_by"]),
    ]
    for name, cols in idx_defs:
        if name not in existing_idx:
            op.create_index(name, "assistant_tasks", cols)


def downgrade() -> None:
    cols_to_drop = [
        "claimed_at",
        "claimed_by",
        "completed_step_keys",
        "steer_notes",
        "checkpoint_json",
        "payload",
        "quota_reason",
        "tools_used_count",
        "tool_budget",
        "tokens_used",
        "token_budget",
        "steps_used",
        "step_budget",
        "deadline_at",
        "next_retry_at",
        "not_before",
        "max_attempts",
        "attempts",
        "worker_kind",
    ]
    if context.is_offline_mode():
        existing_cols = set(cols_to_drop)
        existing_idx = {"ix_assistant_tasks_claimed_by", "ix_assistant_tasks_not_before", "ix_assistant_tasks_worker_kind"}
    else:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        existing_cols = {c["name"] for c in insp.get_columns("assistant_tasks")}
        existing_idx = {idx["name"] for idx in insp.get_indexes("assistant_tasks")}

    for name in ("ix_assistant_tasks_claimed_by", "ix_assistant_tasks_not_before", "ix_assistant_tasks_worker_kind"):
        if name in existing_idx:
            op.drop_index(name, table_name="assistant_tasks")
    with op.batch_alter_table("assistant_tasks") as batch:
        for cname in cols_to_drop:
            if cname in existing_cols:
                batch.drop_column(cname)
