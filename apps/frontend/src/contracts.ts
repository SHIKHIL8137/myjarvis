/**
 * Consolidated P0–P6 Domain Contracts mirrored from jarvis/core/contracts.py.
 */

export type TaskStatusCanonical =
  | "queued"
  | "scheduled"
  | "running"
  | "waiting"
  | "waiting_quota"
  | "paused"
  | "done"
  | "failed"
  | "cancelled";

export type TaskStatusRaw =
  | TaskStatusCanonical
  | "planning"
  | "awaiting_clarification"
  | "awaiting_permission"
  | "awaiting_approval"
  | "completed"
  | "unverified"
  | "started";

export interface TaskBudgetContract {
  step_budget: number;
  steps_used: number;
  token_budget: number;
  tokens_used: number;
  tool_budget: number;
  tools_used_count: number;
  deadline_at?: number | null;
}

export interface TaskPartialReportContract {
  task_id: string;
  reason: string;
  completed_steps: string[];
  checkpoint_keys: string[];
  budget_usage: Record<string, any>;
  summary: string;
  recorded_at: number;
}

export type ActionOutcomeStatus = "started" | "done" | "unverified" | "failed" | "ask" | "denied";

export interface ActionVerificationCheckContract {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ActionExecutionContract {
  execution_id: string;
  action_type: string;
  target: string;
  permission_level: string;
  status: ActionOutcomeStatus;
  verified: boolean;
  summary: string;
  checks: ActionVerificationCheckContract[];
  evidence: Record<string, any>;
  undo_id?: string | null;
  reversible: boolean;
  error: string;
  timestamp: number;
}

export interface UndoEntryContract {
  undo_id: string;
  action_type: string;
  target_path: string;
  resource_id: string;
  status: "available" | "undone" | "expired" | "conflict" | "non_reversible";
  reversible: boolean;
  before_sha256: string;
  after_sha256: string;
  created_at: number;
  expires_at?: number | null;
  undone_at?: number | null;
  reason: string;
}

export type ResearchStage = "PLAN" | "INVESTIGATE" | "REVIEW" | "WRITE" | "VERIFY" | "COMPLETE";

export interface ResearchSourceContract {
  source_id: string;
  title: string;
  url: string;
  source_type: string;
  sha256: string;
  fetched_at: number;
  freshness_label: string;
  excerpt: string;
}

export interface ResearchClaimContract {
  claim_id: string;
  statement: string;
  kind: "direct_quote" | "verified_fact" | "reported_claim" | "unverified";
  verification_status: "verified" | "disputed" | "unverified" | "cited_reported";
  citations: string[];
  quote: string;
  quote_verified: boolean;
  contradicted: boolean;
}

export interface ResearchVerificationContract {
  passed: boolean;
  overall_status: string;
  verified_claim_count: number;
  disputed_claim_count: number;
  unverified_claim_count: number;
  contradictions: Array<Record<string, any>>;
  limitations: string[];
  integrity_warnings: string[];
}

export interface ResearchReportContract {
  task_id: string;
  question: string;
  mode: string;
  stage: ResearchStage;
  sources: ResearchSourceContract[];
  claims: ResearchClaimContract[];
  verification: ResearchVerificationContract;
  markdown: string;
  warnings: string[];
}

export interface AgentProfileContract {
  name: string;
  description: string;
  scope: "builtin" | "user" | "project";
  source_path: string;
  model_role: string;
  permission_tier: string;
  tools_allowlist: string[];
  tools_denylist: string[];
  max_steps: number;
  max_tokens: number;
  can_delegate: boolean;
  max_delegation_depth: number;
  valid: boolean;
  diagnostics: string[];
}

export type RoutineTriggerType =
  | "once"
  | "daily"
  | "weekly"
  | "monthly"
  | "every_n_minutes"
  | "startup"
  | "conditional";

export type LatePolicy = "skip" | "run_once_if_within_grace" | "run_immediately";

export interface RoutineContract {
  routine_id: string;
  title: string;
  trigger_type: RoutineTriggerType;
  enabled: boolean;
  cron_or_time: string;
  interval_minutes?: number | null;
  action_kind: string;
  payload: Record<string, any>;
  late_policy: LatePolicy;
  grace_seconds: number;
  quiet_hours_bypass: boolean;
  last_run_at?: number | null;
  next_run_at?: number | null;
  status: string;
}

export interface RoutineRunContract {
  run_id: string;
  routine_id: string;
  idempotency_key: string;
  scheduled_for: number;
  started_at: number;
  completed_at?: number | null;
  status: string;
  outcome: string;
  error: string;
}

export interface ErrorEnvelopeContract {
  error: string;
  code: string;
  details?: Record<string, any>;
  correlation_id?: string;
  status_code?: number;
  detail?: string;
}

