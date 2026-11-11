"""P10.12 Agent Skills & Capabilities Registry schema migration.

Revision ID: 0003_p10_12_skills_and_targets
Revises: 0002_p10_hardening
Create Date: 2026-11-11
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0003_p10_12_skills_and_targets"
down_revision: Union[str, Sequence[str], None] = "0002_p10_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())

    if "skills" not in existing:
        op.create_table(
            "skills",
            sa.Column("skill_id", sa.String(120), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("category", sa.String(64), nullable=False, server_default="general"),
            sa.Column("version", sa.String(64), nullable=False, server_default="1.0.0"),
            sa.Column("source_repo", sa.String(300), nullable=False, server_default=""),
            sa.Column("source_commit", sa.String(80), nullable=False, server_default=""),
            sa.Column("content_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("license", sa.String(64), nullable=False, server_default="unknown"),
            sa.Column("supported_runtimes", sa.JSON(), nullable=False),
            sa.Column("compatible_roles", sa.JSON(), nullable=False),
            sa.Column("required_dependencies", sa.JSON(), nullable=False),
            sa.Column("required_permissions", sa.JSON(), nullable=False),
            sa.Column("network_requirements", sa.JSON(), nullable=False),
            sa.Column("execution_mode", sa.String(32), nullable=False, server_default="instruction_only"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("verification_status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("install_path", sa.String(600), nullable=False, server_default=""),
            sa.Column("installed_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("last_used_at", sa.Float(), nullable=True),
            sa.Column("manifest_json", sa.JSON(), nullable=False),
        )
        op.create_index("ix_skills_category", "skills", ["category"])
        op.create_index("ix_skills_enabled", "skills", ["enabled"])
        op.create_index("ix_skills_verification_status", "skills", ["verification_status"])

    if "skill_versions" not in existing:
        op.create_table(
            "skill_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("skill_id", sa.String(120), sa.ForeignKey("skills.skill_id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("source_commit", sa.String(80), nullable=False, server_default=""),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("snapshot_dir", sa.String(600), nullable=False, server_default=""),
            sa.Column("manifest_json", sa.JSON(), nullable=False),
            sa.Column("installed_by", sa.String(64), nullable=False, server_default="owner"),
            sa.Column("action", sa.String(32), nullable=False, server_default="install"),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])

    if "skill_assignments" not in existing:
        op.create_table(
            "skill_assignments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("skill_id", sa.String(120), sa.ForeignKey("skills.skill_id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_role", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(36), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("assigned_by", sa.String(64), nullable=False, server_default="owner"),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_skill_assignments_skill_id", "skill_assignments", ["skill_id"])
        op.create_index("ix_skill_assignments_agent_role", "skill_assignments", ["agent_role"])
        op.create_index("ix_skill_assignments_project_id", "skill_assignments", ["project_id"])

    if "skill_metrics" not in existing:
        op.create_table(
            "skill_metrics",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("skill_id", sa.String(120), sa.ForeignKey("skills.skill_id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_role", sa.String(64), nullable=False, server_default="coder_agent"),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("task_kind", sa.String(64), nullable=False, server_default="general"),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("qa_passed", sa.Boolean(), nullable=True),
            sa.Column("baseline_qa_passed", sa.Boolean(), nullable=True),
            sa.Column("latency_ms", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("token_overhead", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("user_corrected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.Float(), nullable=False, server_default="0.0"),
        )
        op.create_index("ix_skill_metrics_skill_id", "skill_metrics", ["skill_id"])
        op.create_index("ix_skill_metrics_agent_role", "skill_metrics", ["agent_role"])
        op.create_index("ix_skill_metrics_run_id", "skill_metrics", ["run_id"])
        op.create_index("ix_skill_metrics_created_at", "skill_metrics", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for tbl in ("skill_metrics", "skill_assignments", "skill_versions", "skills"):
        if tbl in existing:
            op.drop_table(tbl)

