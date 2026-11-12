"""Add P1 background task scheduling, pause_reason, partial_report, correlation_id, and parent_task_id columns.

Revision ID: 0009_p1_task_engine_fields
Revises: 0008_p11_command_history
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0009_p1_task_engine_fields"
down_revision = "0008_p11_command_history"
branch_labels = None
depends_on = None

_NEW_COLS: tuple[tuple[str, sa.Column], ...] = (
    ("scheduled_for", sa.Column("scheduled_for", sa.Float(), nullable=True)),
    ("pause_reason", sa.Column("pause_reason", sa.String(240), nullable=False, server_default="")),
    ("partial_report", sa.Column("partial_report", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
    ("correlation_id", sa.Column("correlation_id", sa.String(64), nullable=False, server_default="")),
    ("parent_task_id", sa.Column("parent_task_id", sa.String(36), nullable=True)),
)


_INDEX_DEFS: tuple[tuple[str, list[str]], ...] = (
    ("ix_assistant_tasks_scheduled_for", ["scheduled_for"]),
    ("ix_assistant_tasks_correlation_id", ["correlation_id"]),
    ("ix_assistant_tasks_parent_task_id", ["parent_task_id"]),
)


def upgrade() -> None:
    if context.is_offline_mode():
        existing_cols: set[str] = set()
        existing_idx: set[str] = set()
    else:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        if "assistant_tasks" not in insp.get_table_names():
            return
        existing_cols = {str(c["name"]) for c in insp.get_columns("assistant_tasks")}
        existing_idx = {str(i["name"]) for i in insp.get_indexes("assistant_tasks")}

    with op.batch_alter_table("assistant_tasks") as batch:
        for name, col in _NEW_COLS:
            if name not in existing_cols:
                batch.add_column(col)

    for idx_name, cols in _INDEX_DEFS:
        if idx_name not in existing_idx:
            op.create_index(idx_name, "assistant_tasks", cols)


def downgrade() -> None:
    if context.is_offline_mode():
        for idx_name, _ in reversed(_INDEX_DEFS):
            op.drop_index(idx_name, table_name="assistant_tasks")
        with op.batch_alter_table("assistant_tasks") as batch:
            for name, _ in reversed(_NEW_COLS):
                batch.drop_column(name)
        return
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "assistant_tasks" not in insp.get_table_names():
        return
    existing_idx = {str(i["name"]) for i in insp.get_indexes("assistant_tasks")}
    for idx_name, _ in reversed(_INDEX_DEFS):
        if idx_name in existing_idx:
            op.drop_index(idx_name, table_name="assistant_tasks")
    existing_cols = {str(c["name"]) for c in insp.get_columns("assistant_tasks")}
    with op.batch_alter_table("assistant_tasks") as batch:
        for name, _ in reversed(_NEW_COLS):
            if name in existing_cols:
                batch.drop_column(name)
