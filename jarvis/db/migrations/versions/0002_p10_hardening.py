"""P10 Hardening: memory lifecycle, expiry, privacy, embedding versioning, and pgvector readiness.

Revision ID: 0002_p10_hardening
Revises: 0001_initial_schema
Create Date: 2026-11-11
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0002_p10_hardening"
down_revision: Union[str, Sequence[str], None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE_NEW_COLS: dict[str, list[tuple[str, sa.Column]]] = {
    "personal_memories": [
        ("namespace", sa.Column("namespace", sa.String(48), nullable=False, server_default="personal_memory")),
        ("owner_scope", sa.Column("owner_scope", sa.String(64), nullable=False, server_default="default")),
        ("project_id", sa.Column("project_id", sa.String(36), nullable=True)),
        ("meta", sa.Column("meta", sa.JSON(), nullable=False, server_default="{}")),
        ("privacy_level", sa.Column("privacy_level", sa.String(24), nullable=False, server_default="private")),
        ("embedding_model", sa.Column("embedding_model", sa.String(120), nullable=False, server_default="hash-v1:256")),
        ("archived", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("deleted_at", sa.Column("deleted_at", sa.Float(), nullable=True)),
        ("superseded_by", sa.Column("superseded_by", sa.String(32), nullable=True)),
        ("supersedes_id", sa.Column("supersedes_id", sa.String(32), nullable=True)),
    ],
    "projects": [
        ("privacy_level", sa.Column("privacy_level", sa.String(24), nullable=False, server_default="private")),
    ],
    "vector_entries": [
        ("memory_type", sa.Column("memory_type", sa.String(32), nullable=False, server_default="fact")),
        ("source", sa.Column("source", sa.String(64), nullable=False, server_default="system")),
        ("confidence", sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0")),
        ("privacy_level", sa.Column("privacy_level", sa.String(24), nullable=False, server_default="private")),
        ("embedding_model", sa.Column("embedding_model", sa.String(120), nullable=False, server_default="hash-v1:256")),
        ("expires_at", sa.Column("expires_at", sa.Float(), nullable=True)),
        ("created_at", sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0")),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    for tbl, cols in _TABLE_NEW_COLS.items():
        if tbl not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(tbl)}
        missing = [(cname, col) for cname, col in cols if cname not in have]
        if missing:
            with op.batch_alter_table(tbl) as batch_op:
                for _, col in missing:
                    batch_op.add_column(col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    for tbl, cols in _TABLE_NEW_COLS.items():
        if tbl not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(tbl)}
        present = {cname for cname, _ in cols if cname in have}
        if not present:
            continue
        indexes = insp.get_indexes(tbl)
        drop_idx: list[str] = [
            str(idx["name"])
            for idx in indexes
            if isinstance(idx.get("name"), str) and any(col_name in present for col_name in (idx.get("column_names") or []))
        ]
        with op.batch_alter_table(tbl) as batch_op:
            for iname in drop_idx:
                batch_op.drop_index(iname)
            for cname, _ in cols:
                if cname in present:
                    batch_op.drop_column(cname)
