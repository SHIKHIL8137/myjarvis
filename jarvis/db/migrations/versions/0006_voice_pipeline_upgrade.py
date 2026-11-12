"""Add extended speech recognition, quality diagnostics, and translation metadata to voice_transcripts.

Revision ID: 0006_voice_pipeline_upgrade
Revises: 0005_p10_22_durable_tasks
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0006_voice_pipeline_upgrade"
down_revision = "0005_p10_22_durable_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing_cols: set[str] = set()
    else:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        existing_cols = {c["name"] for c in insp.get_columns("voice_transcripts")}

    new_columns: list[sa.Column] = [
        sa.Column("source_language", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("language_probability", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("recognition_quality", sa.String(24), nullable=False, server_default="medium"),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
    ]

    with op.batch_alter_table("voice_transcripts") as batch:
        for col in new_columns:
            if col.name not in existing_cols:
                batch.add_column(col)


def downgrade() -> None:
    with op.batch_alter_table("voice_transcripts") as batch:
        for col in ("meta_json", "recognition_quality", "language_probability", "source_language"):
            batch.drop_column(col)

