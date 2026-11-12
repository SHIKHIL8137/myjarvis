"""Add project_aliases table mapping approved user folder aliases to canonical paths.

Revision ID: 0007_p11_project_aliases
Revises: 0006_voice_pipeline_upgrade
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision = "0007_p11_project_aliases"
down_revision = "0006_voice_pipeline_upgrade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing_tables: set[str] = set()
    else:
        bind = op.get_bind()
        existing_tables = set(sa.inspect(bind).get_table_names())

    if "project_aliases" not in existing_tables:
        op.create_table(
            "project_aliases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("alias", sa.String(120), nullable=False, unique=True),
            sa.Column("display_name", sa.String(160), nullable=False, server_default=""),
            sa.Column("folder_path", sa.String(800), nullable=False),
            sa.Column("description", sa.String(400), nullable=False, server_default=""),
            sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_used_at", sa.Float(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_project_aliases_alias", "project_aliases", ["alias"], unique=True)
        op.create_index("ix_project_aliases_folder_path", "project_aliases", ["folder_path"], unique=False)


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_index("ix_project_aliases_folder_path", table_name="project_aliases")
        op.drop_index("ix_project_aliases_alias", table_name="project_aliases")
        op.drop_table("project_aliases")
        return
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "project_aliases" in insp.get_table_names():
        for ix in list(insp.get_indexes("project_aliases")):
            if ix.get("name"):
                op.drop_index(str(ix["name"]), table_name="project_aliases")
        op.drop_table("project_aliases")

