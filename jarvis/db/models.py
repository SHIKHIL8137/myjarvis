"""SQLAlchemy 2 declarative models covering Personal Assistant, Dev Workforce, 5-Way Memory, Gateway, and DR."""
import time
import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> float:
    return round(time.time(), 3)


class Base(DeclarativeBase):
    pass


# ---- 1. Personal Assistant & Notes -------------------------------------------------------------------
class PersonalMemoryRow(Base):
    __tablename__ = "personal_memories"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    namespace: Mapped[str] = mapped_column(String(48), default="personal_memory", index=True)
    owner_scope: Mapped[str] = mapped_column(String(64), default="default", index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # explicit | preference | temporary
    scope: Mapped[str] = mapped_column(String(24), default="global")  # global | temporary
    topic: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="explicit")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    privacy_level: Mapped[str] = mapped_column(String(24), default="private", index=True)  # public | internal | private | restricted
    embedding_model: Mapped[str] = mapped_column(String(120), default="hash-v1:256")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class NoteRow(Base):
    __tablename__ = "notes"
    uid: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(250), index=True)
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    slug: Mapped[str] = mapped_column(String(250), unique=True)
    path: Mapped[str] = mapped_column(String(500), unique=True)
    body_md: Mapped[str] = mapped_column(Text)
    wikilinks: Mapped[list] = mapped_column(JSON, default=list)
    is_trashed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class ConversationSessionRow(Base):
    __tablename__ = "conversation_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="Default Session")
    department: Mapped[str] = mapped_column(String(32), default="assistant")  # assistant | dev
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    active_agent: Mapped[str] = mapped_column(String(64), default="assistant_coordinator")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24))
    agent_name: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class VoiceTranscriptRow(Base):
    __tablename__ = "voice_transcripts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    audio_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_transcript: Mapped[str] = mapped_column(Text)
    corrected_transcript: Mapped[str] = mapped_column(Text)
    detected_language: Mapped[str] = mapped_column(String(16), default="en")
    source_language: Mapped[str] = mapped_column(String(24), default="unknown")
    language_probability: Mapped[float] = mapped_column(Float, default=0.0)
    recognition_quality: Mapped[str] = mapped_column(String(24), default="medium")
    target_language: Mapped[str] = mapped_column(String(16), default="none")
    translated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    model: Mapped[str] = mapped_column(String(64), default="")
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48), default="assistant")
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    assigned_agent: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ReminderRow(Base):
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[float] = mapped_column(Float, index=True)
    recurrence_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="voice+ui")
    status: Mapped[str] = mapped_column(String(24), default="scheduled", index=True)  # scheduled | triggered | completed | cancelled
    triggered_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


# ---- 2. AI Dev Workforce & Multi-Project Intelligence ------------------------------------------------
class ProjectRow(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    root_path: Mapped[str] = mapped_column(String(600), unique=True)
    default_branch: Mapped[str] = mapped_column(String(64), default="main")
    test_cmd: Mapped[str] = mapped_column(String(200), default="pytest -q")
    lint_cmd: Mapped[str] = mapped_column(String(200), default="ruff check")
    privacy_level: Mapped[str] = mapped_column(String(24), default="private")
    tech_stack: Mapped[dict] = mapped_column(JSON, default=dict)
    graphify_status: Mapped[str] = mapped_column(String(32), default="unbuilt")
    graphify_last_built_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ProjectGraphRow(Base):
    __tablename__ = "project_graphs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    git_commit_sha: Mapped[str] = mapped_column(String(64), default="")
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, default=0)
    artifact_path: Mapped[str] = mapped_column(String(600), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    built_at: Mapped[float] = mapped_column(Float, default=_now)


class WorktreeRow(Base):
    __tablename__ = "worktrees"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    branch_name: Mapped[str] = mapped_column(String(120))
    worktree_path: Mapped[str] = mapped_column(String(600), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)  # active | merged | cleaned
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AgentRunRow(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    workflow_name: Mapped[str] = mapped_column(String(64), index=True)  # assistant | workforce
    agent_role: Mapped[str] = mapped_column(String(64), default="coordinator")
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), default="running", index=True)
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    state_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[float] = mapped_column(Float, default=_now)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AgentStepRow(Base):
    __tablename__ = "agent_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    node_name: Mapped[str] = mapped_column(String(80))
    tool_name: Mapped[str] = mapped_column(String(80), default="")
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="completed")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ApprovalRequestRow(Base):
    __tablename__ = "approval_requests"
    token: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(80))
    risk_tier: Mapped[str] = mapped_column(String(16), default="EXECUTE")  # READ | WRITE | EXECUTE
    target_resource: Mapped[str] = mapped_column(String(500), default="")
    preview_diff: Mapped[str] = mapped_column(Text, default="")
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    expires_at: Mapped[float] = mapped_column(Float)
    decided_at: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---- 3. 5-Way Memory, External Knowledge, Training & Gateway Telemetry -------------------------------
class ExperienceLessonRow(Base):
    __tablename__ = "experience_lessons"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(24), default="assistant", index=True)  # assistant | dev
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(48), default="correction")
    topic: Mapped[str] = mapped_column(String(120), default="")
    trigger_context: Mapped[str] = mapped_column(Text)
    outcome_before: Mapped[str] = mapped_column(Text, default="")
    lesson_after: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.85)
    source: Mapped[str] = mapped_column(String(32), default="explicit_user")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class KnowledgeSourceRow(Base):
    __tablename__ = "knowledge_sources"
    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(48))
    location: Mapped[str] = mapped_column(String(600))
    topic: Mapped[str] = mapped_column(String(120), default="general")
    license: Mapped[str] = mapped_column(String(64), default="unknown")
    trust_classification: Mapped[str] = mapped_column(String(48), default="user_provided")
    active_version: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(16), default="en")
    retrieved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    update_status: Mapped[str] = mapped_column(String(48), default="registered")


class KnowledgeVersionRow(Base):
    __tablename__ = "knowledge_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.source_id", ondelete="CASCADE"), index=True)
    version_num: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    doc_version: Mapped[str] = mapped_column(String(64), default="1")
    license: Mapped[str] = mapped_column(String(64), default="unknown")
    language: Mapped[str] = mapped_column(String(16), default="en")
    retrieved_at: Mapped[float] = mapped_column(Float, default=_now)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.source_id", ondelete="CASCADE"), index=True)
    version_num: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200), default="")
    topic: Mapped[str] = mapped_column(String(120), default="general")
    language: Mapped[str] = mapped_column(String(16), default="en")
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DatasetBuildRow(Base):
    __tablename__ = "dataset_builds"
    build_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    target_format: Mapped[str] = mapped_column(String(24))
    train_count: Mapped[int] = mapped_column(Integer, default=0)
    val_count: Mapped[int] = mapped_column(Integer, default=0)
    test_count: Mapped[int] = mapped_column(Integer, default=0)
    leakage_clean: Mapped[bool] = mapped_column(Boolean, default=True)
    sha256: Mapped[str] = mapped_column(String(64))
    artifact_dir: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class TrainingJobRow(Base):
    __tablename__ = "training_jobs"
    job_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dataset_build_id: Mapped[str] = mapped_column(String(120), index=True)
    base_model: Mapped[str] = mapped_column(String(120))
    hf_model_id: Mapped[str] = mapped_column(String(160))
    quantization: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    est_vram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class EvalReportRow(Base):
    __tablename__ = "eval_reports"
    eval_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    baseline_model: Mapped[str] = mapped_column(String(120))
    candidate_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    baseline_score: Mapped[float] = mapped_column(Float)
    candidate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    regressions: Mapped[int] = mapped_column(Integer, default=0)
    safety_failures: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(48))
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ModelVersionRow(Base):
    __tablename__ = "model_versions"
    version_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    base_model: Mapped[str] = mapped_column(String(120))
    artifact_type: Mapped[str] = mapped_column(String(32), default="ollama_tag")
    tag_or_path: Mapped[str] = mapped_column(String(500), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    eval_report_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class LLMUsageLogRow(Base):
    __tablename__ = "llm_usage_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision_layer: Mapped[int] = mapped_column(Integer, default=3)
    provider: Mapped[str] = mapped_column(String(48), index=True)
    model: Mapped[str] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="ok")
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    category: Mapped[str] = mapped_column(String(32), index=True)
    decision: Mapped[str] = mapped_column(String(24))
    target: Mapped[str] = mapped_column(String(500), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class BackupManifestRow(Base):
    __tablename__ = "backup_manifests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tier: Mapped[str] = mapped_column(String(24), index=True)  # tier1_hot | tier2_warm | tier3_cold
    bundle_path: Mapped[str] = mapped_column(String(600))
    sha256_manifest: Mapped[str] = mapped_column(String(64))
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    verified_restore: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class VectorEntryRow(Base):
    """Isolated 5-way vector chunks (SQLite LE normalized BLOB + PostgreSQL pgvector metadata/expiry alignment)."""
    __tablename__ = "vector_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    namespace: Mapped[str] = mapped_column(String(48), index=True)  # personal_memory | project_knowledge | experience_memory | external_knowledge | personal_documents
    owner_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    memory_type: Mapped[str] = mapped_column(String(32), default="fact", index=True)
    doc_id: Mapped[str] = mapped_column(String(120), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(250), default="")
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64), default="system")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    privacy_level: Mapped[str] = mapped_column(String(24), default="private", index=True)
    embedding_model: Mapped[str] = mapped_column(String(120), default="hash-v1:256", index=True)
    dim: Mapped[int] = mapped_column(Integer, default=256)
    embedding_blob: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


Index("ix_vec_ns_proj", VectorEntryRow.namespace, VectorEntryRow.project_id)
Index("ix_vec_expiry", VectorEntryRow.namespace, VectorEntryRow.expires_at)


# ---- 4. Agent Skills & Capabilities Registry (P10.12) ------------------------------------------------
class SkillRow(Base):
    """Canonical central registry of validated, version-pinned, checksummed agent skills."""
    __tablename__ = "skills"
    skill_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    version: Mapped[str] = mapped_column(String(64), default="1.0.0")
    source_repo: Mapped[str] = mapped_column(String(300), default="")
    source_commit: Mapped[str] = mapped_column(String(80), default="")
    content_sha256: Mapped[str] = mapped_column(String(64), default="")
    license: Mapped[str] = mapped_column(String(64), default="unknown")
    supported_runtimes: Mapped[list] = mapped_column(JSON, default=list)
    compatible_roles: Mapped[list] = mapped_column(JSON, default=list)
    required_dependencies: Mapped[list] = mapped_column(JSON, default=list)
    required_permissions: Mapped[list] = mapped_column(JSON, default=list)
    network_requirements: Mapped[list] = mapped_column(JSON, default=list)
    execution_mode: Mapped[str] = mapped_column(String(32), default="instruction_only")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    install_path: Mapped[str] = mapped_column(String(600), default="")
    installed_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)
    last_used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SkillVersionRow(Base):
    __tablename__ = "skill_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.skill_id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    source_commit: Mapped[str] = mapped_column(String(80), default="")
    content_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_dir: Mapped[str] = mapped_column(String(600), default="")
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)
    installed_by: Mapped[str] = mapped_column(String(64), default="owner")
    action: Mapped[str] = mapped_column(String(32), default="install")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class SkillAssignmentRow(Base):
    __tablename__ = "skill_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.skill_id", ondelete="CASCADE"), index=True)
    agent_role: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_by: Mapped[str] = mapped_column(String(64), default="owner")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class SkillMetricRow(Base):
    __tablename__ = "skill_metrics"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.skill_id", ondelete="CASCADE"), index=True)
    agent_role: Mapped[str] = mapped_column(String(64), default="coder_agent", index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_kind: Mapped[str] = mapped_column(String(64), default="general")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    qa_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    baseline_qa_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    token_overhead: Mapped[int] = mapped_column(Integer, default=0)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


# ---- 5. P10.20 Scoped Filesystem Permissions & Centralized Assistant Tasks ------------------------
class FilesystemPermissionRow(Base):
    __tablename__ = "filesystem_permissions"
    permission_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    canonical_folder_path: Mapped[str] = mapped_column(String(800), index=True)
    granted_operations: Mapped[list] = mapped_column(JSON, default=list)
    scope: Mapped[str] = mapped_column(String(24), default="once", index=True)  # "once" | "session" | "persistent"
    granted_at: Mapped[float] = mapped_column(Float, default=_now, index=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    granted_by: Mapped[str] = mapped_column(String(64), default="owner")
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)  # "active" | "revoked" | "expired" | "consumed"
    purpose: Mapped[str] = mapped_column(String(400), default="")
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class FilesystemPermissionRequestRow(Base):
    __tablename__ = "filesystem_permission_requests"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requested_path: Mapped[str] = mapped_column(String(800))
    canonical_path: Mapped[str] = mapped_column(String(800), index=True)
    requested_operations: Mapped[list] = mapped_column(JSON, default=list)
    not_requested_operations: Mapped[list] = mapped_column(JSON, default=list)
    sensitive_flags: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(500), default="")
    access_duration: Mapped[str] = mapped_column(String(24), default="once")  # "once" | "session" | "persistent"
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)  # "pending" | "approved" | "denied" | "expired" | "consumed"
    permission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deferred_action: Mapped[dict] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    decided_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AssistantTaskRow(Base):
    __tablename__ = "assistant_tasks"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_request: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(24), default="text")
    intent: Mapped[str] = mapped_column(String(64), default="general_chat", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    worker_kind: Mapped[str] = mapped_column(String(48), default="assistant", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    not_before: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    next_retry_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    deadline_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    step_budget: Mapped[int] = mapped_column(Integer, default=20)
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, default=120000)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    tool_budget: Mapped[int] = mapped_column(Integer, default=40)
    tools_used_count: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[list] = mapped_column(JSON, default=list)
    selected_capabilities: Mapped[list] = mapped_column(JSON, default=list)
    selected_agents: Mapped[list] = mapped_column(JSON, default=list)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    permissions_requested: Mapped[list] = mapped_column(JSON, default=list)
    current_step: Mapped[str] = mapped_column(String(200), default="")
    result: Mapped[str] = mapped_column(Text, default="")
    generated_files: Mapped[list] = mapped_column(JSON, default=list)
    error_info: Mapped[str] = mapped_column(Text, default="")
    quota_reason: Mapped[str] = mapped_column(String(200), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint_json: Mapped[dict] = mapped_column(JSON, default=dict)
    steer_notes: Mapped[list] = mapped_column(JSON, default=list)
    completed_step_keys: Mapped[list] = mapped_column(JSON, default=list)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claimed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ProjectAliasRow(Base):
    """User-approved friendly alias mapped to a canonical local folder path."""

    __tablename__ = "project_aliases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    alias: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    folder_path: Mapped[str] = mapped_column(String(800), index=True)
    description: Mapped[str] = mapped_column(String(400), default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class CommandHistoryRow(Base):
    """Unified persisted execution history for chat/voice commands, workflows, and replay continuity."""

    __tablename__ = "command_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_command: Mapped[str] = mapped_column(String(400), index=True)
    task_category: Mapped[str] = mapped_column(String(48), default="informational", index=True)
    intent: Mapped[str] = mapped_column(String(64), default="general_chat", index=True)
    department: Mapped[str] = mapped_column(String(32), default="assistant")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_alias: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_path: Mapped[str | None] = mapped_column(String(800), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    paths_touched: Mapped[list] = mapped_column(JSON, default=list)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    error_info: Mapped[str] = mapped_column(Text, default="")
    replayable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(24), default="text")
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)

