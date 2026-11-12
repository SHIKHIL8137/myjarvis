"""Typed DTOs, strict label schema validation, and security enums for the 3-Layer Decision Engine."""
from dataclasses import asdict, dataclass, field
import math

ALLOWED_DEPARTMENTS = frozenset({"assistant", "dev", "system", "chat"})
ALLOWED_INTENTS = frozenset(
    {
        "general_chat",
        "dev_coding",
        "dev_architecture",
        "dev_review",
        "dev_debug",
        "document_export",
        "reminder_manage",
        "web_research",
        "memory_manage",
        "filesystem_ops",
        "system_status",
        "task_status",
        "docker_ops",
        "recovery_ops",
        "clarification_needed",
        "sensitive_action_request",
        "security_audit",
        "empty",
        "hardware_stop",
    }
)
ALLOWED_COMPLEXITIES = frozenset({"trivial", "standard", "complex_reasoning", "heavy_coding"})
ALLOWED_RISK_TIERS = frozenset({"READ", "WRITE", "EXECUTE"})
ALLOWED_WORKFLOWS = frozenset({"fast_command", "direct_chat", "assistant_graph", "workforce_graph", "clarify"})
ALLOWED_CLASSIFIER_SOURCES = frozenset({"layer1_rules", "heuristic", "llm", "laya_isolated", "laya_shadow_fallback"})

SENSITIVE_OPERATION_CATEGORIES = frozenset(
    {
        "shell_execution",
        "file_deletion",
        "database_migration",
        "database_restore",
        "git_commit_or_merge",
        "deployment_or_publish",
        "credential_access",
        "network_policy_change",
        "skill_installation",
        "privilege_escalation",
    }
)

# Canonical Laya typed-decision schema matching Jarvis ALLOWED_* enums strictly
JARVIS_LAYA_QUESTIONS: dict[str, dict] = {
    "department": {
        "type": "choice",
        "instructions": "Which Jarvis department should handle the request?",
        "criteria": {
            "chat": "casual conversation, greetings, general Q&A with no file, code, reminder, or web tool",
            "assistant": "documents (PDF/Word/PPTX/Excel), reminders, personal memory, email drafts, or live web research",
            "dev": "software engineering, repository code edits, debugging, architecture, unit tests, git, or security code audit",
            "system": "Jarvis status, settings, backups, database administration, permissions, or sensitive host actions",
        },
    },
    "intent": {
        "type": "choice",
        "instructions": "Select the exact primary intent for this user prompt.",
        "criteria": {
            "general_chat": "ordinary conversation, explanation, translation, or brainstorming",
            "dev_coding": "write, implement, fix, refactor, or test repository source code",
            "dev_architecture": "explain codebase architecture, module design, or dependency graphs",
            "dev_review": "review a diff, pull request, or code quality",
            "dev_debug": "diagnose an error, exception, traceback, or failing test",
            "document_export": "generate or export PDF, DOCX, PPTX, or XLSX files",
            "reminder_manage": "create, list, or complete reminders or scheduled items",
            "web_research": "look up current web facts, weather, news, stock prices, or online versions",
            "memory_manage": "save, search, list, update, or forget personal memories or notes",
            "filesystem_ops": "search, list, inspect, or read local files and workspace folders inside allowlisted paths",
            "system_status": "inspect system health, CPU, RAM, GPU, audio devices, models, or configuration",
            "task_status": "check, list, or inspect pending, running, paused, or completed assistant tasks",
            "docker_ops": "check Docker daemon availability, list sandbox images or containers, or inspect sandboxes",
            "recovery_ops": "list or inspect disaster recovery backups, database targets, or restore status",
            "security_audit": "audit source code or dependencies for security vulnerabilities",
            "clarification_needed": "underspecified or ambiguous reference requiring a clarifying question",
            "sensitive_action_request": "request involving shell execution, file deletion, DB migration/restore, git merge, deploy, secrets, or privileges",
        },
    },
    "complexity": {
        "type": "choice",
        "instructions": "Estimate reasoning and workflow complexity.",
        "criteria": {
            "trivial": "short greeting, stop command, or one-line simple lookup",
            "standard": "ordinary single-step chat, reminder, search, or document task",
            "complex_reasoning": "multi-step analysis, architecture design, or debugging investigation",
            "heavy_coding": "multi-file repository modification requiring worktree coding and QA verification",
        },
    },
    "risk_tier": {
        "type": "choice",
        "instructions": "Classify execution risk tier strictly.",
        "criteria": {
            "READ": "read-only question, search, explanation, or status check with no side effects",
            "WRITE": "creating a document, note, reminder, draft, or isolated branch code edit",
            "EXECUTE": "destructive or privileged action: delete files, shell commands, DB restore/migrate, merge, deploy, secrets, skills install",
        },
    },
    "needs_live_web": {
        "type": "noul",
        "instructions": "Does answering accurately require live internet search or current web data?",
    },
    "needs_project_context": {
        "type": "noul",
        "instructions": "Does answering require inspecting a registered local code repository or project files?",
    },
}

_FORBIDDEN_BYPASS_KEYS = frozenset(
    {"approved", "bypass_approval", "skip_approval", "execute_without_approval", "auto_authorize", "privilege_granted"}
)


class ClassifierSchemaError(ValueError):
    """Raised when external or model classifier output violates Jarvis's strict typed label contract."""


def validate_classifier_payload(raw: dict, *, allow_slash: bool = False) -> dict:
    """Validate untrusted classifier JSON output strictly against canonical Jarvis decision enums."""
    if not isinstance(raw, dict):
        raise ClassifierSchemaError("Classifier payload must be a JSON object.")

    for fk in _FORBIDDEN_BYPASS_KEYS:
        if fk in raw and raw[fk] is not False and raw[fk] is not None:
            raise ClassifierSchemaError(f"Classifier attempted to emit unauthorized security control key {fk!r}.")

    dept = str(raw.get("department") or "").strip().lower()
    if dept not in ALLOWED_DEPARTMENTS:
        raise ClassifierSchemaError(f"Unsupported department {dept!r}; allowed={sorted(ALLOWED_DEPARTMENTS)}")

    intent = str(raw.get("intent") or "").strip().lower()
    is_slash = allow_slash and intent.startswith("slash_") and len(intent) > 6
    if intent not in ALLOWED_INTENTS and not is_slash:
        raise ClassifierSchemaError(f"Unsupported intent {intent!r}; allowed={sorted(ALLOWED_INTENTS)}")

    complexity = str(raw.get("complexity") or "").strip().lower()
    if complexity not in ALLOWED_COMPLEXITIES:
        raise ClassifierSchemaError(f"Unsupported complexity {complexity!r}; allowed={sorted(ALLOWED_COMPLEXITIES)}")

    risk = str(raw.get("risk_tier") or "").strip().upper()
    if risk not in ALLOWED_RISK_TIERS:
        raise ClassifierSchemaError(f"Unsupported risk_tier {risk!r}; allowed={sorted(ALLOWED_RISK_TIERS)}")

    try:
        raw_conf = raw.get("confidence")
        if raw_conf is None or isinstance(raw_conf, bool):
            raise ValueError("missing or invalid confidence")
        conf = float(raw_conf)
    except (TypeError, ValueError) as e:
        raise ClassifierSchemaError("Classifier confidence must be a float in [0.0, 1.0].") from e
    if not math.isfinite(conf) or conf < 0.0 or conf > 1.0:
        raise ClassifierSchemaError(f"Classifier confidence {conf!r} outside valid [0.0, 1.0] range.")

    needs_web = raw.get("needs_live_web", False)
    needs_proj = raw.get("needs_project_context", False)
    if not isinstance(needs_web, bool) or not isinstance(needs_proj, bool):
        raise ClassifierSchemaError("needs_live_web and needs_project_context must be strict booleans.")

    clarify = raw.get("clarifying_question")
    if clarify is not None and not isinstance(clarify, str):
        raise ClassifierSchemaError("clarifying_question must be a string or null.")

    route_val = str(raw.get("route") or "auto").strip().lower()
    if route_val not in ("auto", "review", "blocked_by_policy"):
        raise ClassifierSchemaError(f"Unsupported classifier route {route_val!r}.")

    return {
        "department": dept,
        "intent": intent,
        "complexity": complexity,
        "risk_tier": risk,
        "needs_live_web": needs_web,
        "needs_project_context": needs_proj,
        "confidence": round(conf, 4),
        "clarifying_question": clarify.strip() if isinstance(clarify, str) and clarify.strip() else None,
        "abstain": bool(raw.get("abstain", False)),
        "unbound": bool(raw.get("unbound", False)),
        "route": route_val,
    }


@dataclass
class DecisionEnvelope:
    layer: int  # 1, 2, or 3
    department: str = "chat"  # "assistant" | "dev" | "system" | "chat"
    intent: str = "general_chat"
    complexity: str = "standard"  # "trivial" | "standard" | "complex_reasoning" | "heavy_coding"
    needs_live_web: bool = False
    needs_project_context: bool = False
    project_hint: str | None = None
    risk_tier: str = "READ"  # "READ" | "WRITE" | "EXECUTE"
    confidence: float = 0.85
    fast_reply: str | None = None
    clarifying_question: str | None = None
    command_payload: dict | None = None
    latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    language: str = "en"
    classifier_source: str = "heuristic"
    abstained: bool = False
    out_of_distribution: bool = False
    laya_route: str = "auto"
    requires_owner_approval: bool = False
    sensitive_flags: list[str] = field(default_factory=list)
    shadow_comparison: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoutingPlan:
    decision: DecisionEnvelope
    workflow: str  # "fast_command" | "direct_chat" | "assistant_graph" | "workforce_graph" | "clarify"
    primary_model: str
    fallback_models: list[str] = field(default_factory=list)
    agent_role: str = "assistant_coordinator"
    allowed_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "workflow": self.workflow,
            "primary_model": self.primary_model,
            "fallback_models": list(self.fallback_models),
            "agent_role": self.agent_role,
            "allowed_tools": list(self.allowed_tools),
        }
