"""Consolidated P0–P6 Canonical Domain Contracts (mirrored in apps/frontend/src/contracts.ts)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusCanonical(str, Enum):
    """Canonical durable background task states across API, TaskRunner, and HUD."""

    QUEUED = "queued"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_QUOTA = "waiting_quota"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TASK_STATUS_TO_CANONICAL: dict[str, TaskStatusCanonical] = {
    "queued": TaskStatusCanonical.QUEUED,
    "planning": TaskStatusCanonical.QUEUED,
    "scheduled": TaskStatusCanonical.SCHEDULED,
    "running": TaskStatusCanonical.RUNNING,
    "waiting": TaskStatusCanonical.WAITING,
    "awaiting_clarification": TaskStatusCanonical.WAITING,
    "awaiting_permission": TaskStatusCanonical.WAITING,
    "awaiting_approval": TaskStatusCanonical.WAITING,
    "waiting_quota": TaskStatusCanonical.WAITING_QUOTA,
    "paused": TaskStatusCanonical.PAUSED,
    "done": TaskStatusCanonical.DONE,
    "completed": TaskStatusCanonical.DONE,
    "unverified": TaskStatusCanonical.FAILED,
    "started": TaskStatusCanonical.RUNNING,
    "failed": TaskStatusCanonical.FAILED,
    "cancelled": TaskStatusCanonical.CANCELLED,
}


def normalize_task_status(raw_status: str) -> TaskStatusCanonical:
    """Normalize internal/legacy task statuses into the canonical P1 contract state."""
    return _TASK_STATUS_TO_CANONICAL.get(str(raw_status or "").strip().lower(), TaskStatusCanonical.QUEUED)


class TaskBudgetContract(BaseModel):
    step_budget: int = Field(20, ge=1)
    steps_used: int = Field(0, ge=0)
    token_budget: int = Field(120_000, ge=1)
    tokens_used: int = Field(0, ge=0)
    tool_budget: int = Field(40, ge=1)
    tools_used_count: int = Field(0, ge=0)
    deadline_at: float | None = None


class TaskPartialReportContract(BaseModel):
    task_id: str
    reason: str
    completed_steps: list[str] = Field(default_factory=list)
    checkpoint_keys: list[str] = Field(default_factory=list)
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    recorded_at: float = 0.0


class TaskRecordContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    user_request: str = ""
    source: str = "text"
    intent: str = "general_chat"
    status: str = "queued"
    canonical_status: TaskStatusCanonical = TaskStatusCanonical.QUEUED
    worker_kind: str = "assistant"
    attempts: int = 0
    max_attempts: int = 3
    scheduled_for: float | None = None
    pause_reason: str = ""
    correlation_id: str = ""
    current_step: str = ""
    result: str = ""
    error_info: str = ""
    quota_reason: str = ""
    plan: list[Any] = Field(default_factory=list)
    selected_agents: list[str] = Field(default_factory=list)
    selected_capabilities: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    completed_step_keys: list[str] = Field(default_factory=list)
    steer_notes: list[dict[str, Any]] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float | None = None


class ActionOutcomeStatus(str, Enum):
    """Canonical P5 action execution outcome states."""

    STARTED = "started"
    DONE = "done"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    ASK = "ask"
    DENIED = "denied"


class ActionVerificationCheckContract(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ActionExecutionContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_id: str = ""
    action_type: str
    target: str = ""
    permission_level: str = "READ"
    status: ActionOutcomeStatus
    verified: bool = False
    summary: str = ""
    checks: list[ActionVerificationCheckContract] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    undo_id: str | None = None
    reversible: bool = False
    error: str = ""
    timestamp: float = 0.0


class UndoEntryContract(BaseModel):
    undo_id: str
    action_type: str
    target_path: str = ""
    resource_id: str = ""
    status: str = "available"  # available | undone | expired | conflict | non_reversible
    reversible: bool = True
    before_sha256: str = ""
    after_sha256: str = ""
    created_at: float = 0.0
    expires_at: float | None = None
    undone_at: float | None = None
    reason: str = ""


class ResearchStage(str, Enum):
    PLAN = "PLAN"
    INVESTIGATE = "INVESTIGATE"
    REVIEW = "REVIEW"
    WRITE = "WRITE"
    VERIFY = "VERIFY"
    COMPLETE = "COMPLETE"


class ResearchSourceContract(BaseModel):
    source_id: str
    title: str = ""
    url: str = ""
    source_type: str = "web_page"
    sha256: str = ""
    fetched_at: float = 0.0
    freshness_label: str = "unknown"
    excerpt: str = ""


class ResearchClaimContract(BaseModel):
    claim_id: str = ""
    statement: str
    kind: str = "reported_claim"  # direct_quote | verified_fact | reported_claim | unverified
    verification_status: str = "unverified"  # verified | disputed | unverified | cited_reported
    citations: list[str] = Field(default_factory=list)
    quote: str = ""
    quote_verified: bool = False
    contradicted: bool = False


class ResearchVerificationContract(BaseModel):
    passed: bool = False
    overall_status: str = "partial"
    verified_claim_count: int = 0
    disputed_claim_count: int = 0
    unverified_claim_count: int = 0
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    integrity_warnings: list[str] = Field(default_factory=list)


class ResearchReportContract(BaseModel):
    task_id: str
    question: str
    mode: str = "local_kb_only"
    stage: ResearchStage = ResearchStage.COMPLETE
    sources: list[ResearchSourceContract] = Field(default_factory=list)
    claims: list[ResearchClaimContract] = Field(default_factory=list)
    verification: ResearchVerificationContract = Field(default_factory=ResearchVerificationContract)
    markdown: str = ""
    warnings: list[str] = Field(default_factory=list)


class AgentProfileContract(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = ""
    scope: str = "builtin"  # builtin | user | project
    source_path: str = ""
    model_role: str = "fast"
    permission_tier: str = "READ"
    tools_allowlist: list[str] = Field(default_factory=list)
    tools_denylist: list[str] = Field(default_factory=list)
    max_steps: int = Field(12, ge=1, le=60)
    max_tokens: int = Field(32_000, ge=512, le=256_000)
    can_delegate: bool = False
    max_delegation_depth: int = Field(1, ge=0, le=3)
    valid: bool = True
    diagnostics: list[str] = Field(default_factory=list)


class RoutineTriggerType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVERY_N_MINUTES = "every_n_minutes"
    STARTUP = "startup"
    CONDITIONAL = "conditional"


class LatePolicy(str, Enum):
    SKIP = "skip"
    RUN_ONCE_IF_WITHIN_GRACE = "run_once_if_within_grace"
    RUN_IMMEDIATELY = "run_immediately"


class QuietHoursSpec(BaseModel):
    enabled: bool = False
    start_hour: int = Field(22, ge=0, le=23)
    end_hour: int = Field(7, ge=0, le=23)
    defer_non_critical: bool = True


class RoutineContract(BaseModel):
    routine_id: str
    title: str
    trigger_type: RoutineTriggerType
    enabled: bool = True
    cron_or_time: str = ""
    interval_minutes: int | None = None
    action_kind: str = "reminder"  # reminder | task | system_check | research
    payload: dict[str, Any] = Field(default_factory=dict)
    late_policy: LatePolicy = LatePolicy.RUN_ONCE_IF_WITHIN_GRACE
    grace_seconds: int = Field(3600, ge=0)
    quiet_hours_bypass: bool = False
    last_run_at: float | None = None
    next_run_at: float | None = None
    status: str = "active"


class RoutineRunContract(BaseModel):
    run_id: str
    routine_id: str
    idempotency_key: str
    scheduled_for: float
    started_at: float
    completed_at: float | None = None
    status: str = "completed"  # queued | running | completed | deferred_quiet_hours | skipped_late | failed
    outcome: str = ""
    error: str = ""


class ErrorEnvelopeContract(BaseModel):
    error: str
    code: str = "error"
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = ""
    status_code: int = 400
    detail: str | None = None

