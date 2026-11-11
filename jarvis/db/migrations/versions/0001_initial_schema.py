"""Initial Jarvis schema across Personal Assistant, Dev Workforce, 5-Way Memory, and DR.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-11
"""
from typing import Sequence, Union
from alembic import op
from jarvis.db.models import Base

revision: str = "0001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)

