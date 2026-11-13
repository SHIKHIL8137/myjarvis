"""Unified request understanding & structured request representation (P0.1, P0.2, P0.3).
Analyzes user instructions into structured, validated requests with execution modes,
target disambiguation, and conversational continuity context.
"""
import re
from typing import Any

from .schemas import (
    ALLOWED_REQUEST_TYPES,
    ConversationContext,
    RequestTarget,
    StructuredRequest,
)

# Extraction regular expressions
PROJECT_EXPLORATION_RE = re.compile(
    r"\b(?:explore|inspect|analyze|audit|survey|walkthrough)\s+(?:(?:my|the|this)\s+)?(?:([A-Za-z0-9_.-]+)\s+)?(?:project|repo(?:sitory)?|codebase|app)(?:\s+(?:and|to)\s+(.*))?|"
    r"\b(?:explore|inspect)\s+(?:my\s+|the\s+)?([A-Za-z0-9_.-]+)(?:\s+(?:project|codebase|repo))?(?:\s+(?:and|to)\s+(.*))?\b",
    re.I,
)

TASK_STEERING_RE = re.compile(
    r"\b(?:focus\s+(?:more\s+)?on|steer(?:\s+to)?|skip\s+(?:step|the)?|also\s+(?:check|inspect|look\s+at|examine|include)|"
    r"pause\s+(?:the\s+|this\s+)?task|resume\s+(?:the\s+|this\s+)?task|stop\s+(?:the\s+|this\s+)?current\s+task|cancel\s+(?:the\s+|this\s+)?task)\b",
    re.I,
)

QUESTION_EXPLANATION_RE = re.compile(
    r"^\W*(?:what\s+is\s+(?:the\s+)?(?:difference|distinction)\s+between|"
    r"how\s+does\s+.*(?:work|function|operate)|"
    r"explain\s+(?:to\s+me\s+)?(?:how|why|what|the)|"
    r"why\s+(?:is|do|does|did|should)|"
    r"what\s+(?:is|are|does|means?)\s+(?:a\s+|an\s+|the\s+)?(?!(?:my|the)\s+(?:task|project|file|document|status|window|reminder))|"
    r"compare\s+.*(?:and|with|versus|vs)|"
    r"tell\s+me\s+about\s+(?!(?:my|the)\s+(?:task|project|file|reminder)))",
    re.I,
)

GREETING_CASUAL_RE = re.compile(
    r"^\W*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening|day)|greetings|"
    r"how\s+are\s+you|what's\s+up|sup|thanks?|thank\s+you|bye|goodbye|who\s+are\s+you)\W*$",
    re.I,
)

ORDINAL_CANDIDATE_RE = re.compile(
    r"^\W*(?:(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)|([1-9]))(?:\s+(?:one|candidate|folder|directory|project))?\W*$",
    re.I,
)

FOLLOW_UP_PRONOUN_RE = re.compile(
    r"\b(?:its?\s+(?:backend|frontend|database|models?|tests?|schema|package\.json|readme|routes?)|"
    r"(?:look\s+at|inspect|show|check|find)\s+(?:its?|the)\s+(?:backend|frontend|database|api|tests?|dependencies)|"
    r"what\s+about\s+(?:its?|the)\s+(?:backend|frontend|database|tests?|stack)|"
    r"what\s+database\s+does\s+it\s+use|does\s+it\s+have\s+tests?)\b",
    re.I,
)

RESEARCH_CUES_RE = re.compile(
    r"\b(?:research|investigate|survey)\s+(?:the\s+|latest\s+|current\s+|all\s+)?(?:local\s+ai|ai\s+models|frameworks|libraries|solutions|options|tools|approaches)\b|"
    r"\bdeep\s+research\b",
    re.I,
)

CODING_ACTION_RE = re.compile(
    r"\b(?:implement|refactor|write\s+(?:code|a\s+function|a\s+test|a\s+class)|fix\s+the\s+bug|create\s+a\s+unit\s+test)\b",
    re.I,
)


def extract_project_target(text: str) -> str | None:
    """Extract named project candidate from natural text if present."""
    if not text:
        return None
    s = text.strip()
    m = PROJECT_EXPLORATION_RE.search(s)
    if m:
        cand = m.group(1) or m.group(3)
        if cand and cand.lower() not in ("my", "the", "a", "an", "this", "that"):
            return cand.strip()
    # Check "explore <Project>"
    m2 = re.search(r"\b(?:explore|inspect|analyze|open)\s+(?:project\s+)?([A-Za-z0-9_.-]+)\b", s, re.I)
    if m2:
        cand = m2.group(1).strip()
        if cand.lower() not in ("my", "the", "this", "that", "it", "codebase", "project", "folder", "directory", "files"):
            return cand
    return None


def extract_requested_operations(text: str, request_type: str) -> list[str]:
    """Identify specific requested operations from user instruction."""
    ops: list[str] = []
    lowered = (text or "").lower()

    if request_type in ("project_exploration", "coding", "follow_up"):
        if any(w in lowered for w in ("structure", "files", "tree", "layout", "overview", "explore", "inspect")):
            ops.append("discover_structure")
        if any(w in lowered for w in ("modules", "components", "packages", "apps", "src")):
            ops.append("inspect_modules")
        if any(w in lowered for w in ("dependencies", "package.json", "pyproject", "cargo", "requirements")):
            ops.append("analyze_dependencies")
        if any(w in lowered for w in ("architecture", "design", "explain", "how it works", "data flow")):
            ops.append("summarize_architecture")
        if any(w in lowered for w in ("database", "db", "schema", "prisma", "models", "migrations")):
            ops.append("inspect_database")
        if any(w in lowered for w in ("test", "tests", "pytest", "jest", "coverage")):
            ops.append("review_tests")
        if any(w in lowered for w in ("security", "audit", "vulnerabilities")):
            ops.append("security_review")
        if not ops:
            ops = ["discover_structure", "inspect_modules", "summarize_architecture"]
    elif request_type == "research":
        ops = ["web_search", "synthesize_evidence", "extract_citations"]
        if "deep" in lowered or "report" in lowered:
            ops.append("generate_research_report")
    elif request_type == "task_steering":
        if "pause" in lowered:
            ops.append("pause_task")
        elif "resume" in lowered:
            ops.append("resume_task")
        elif "stop" in lowered or "cancel" in lowered:
            ops.append("cancel_task")
        else:
            ops.append("steer_focus")
    elif request_type == "question_explanation":
        ops = ["synthesize_explanation"]

    return ops


def build_structured_request(text: str, context: ConversationContext | None = None) -> StructuredRequest:
    """Analyze a user utterance into a formal StructuredRequest with execution mode and context."""
    s = (text or "").strip()
    lowered = s.lower()
    ctx = context or ConversationContext()

    # 1. Clarification response intercept
    if ctx.pending_clarification and ORDINAL_CANDIDATE_RE.match(s):
        m = ORDINAL_CANDIDATE_RE.match(s)
        ord_val = m.group(1) or m.group(2) if m else s
        return StructuredRequest(
            request_type="clarification_response",
            intent="clarification_needed",
            target=RequestTarget(type="task", target_id=ctx.active_task_id),
            requested_operations=["resolve_clarification"],
            execution_mode="direct",
            original_text=s,
            confidence=0.98,
            meta={"selected_ordinal": ord_val, "clarification": ctx.pending_clarification},
        )

    # 2. Task steering intercept (when an active task is running or paused)
    if TASK_STEERING_RE.search(s) and (ctx.active_task_id or any(w in lowered for w in ("the task", "this task", "current task"))):
        ops = extract_requested_operations(s, "task_steering")
        return StructuredRequest(
            request_type="task_steering",
            intent="task_steering",
            target=RequestTarget(type="task", target_id=ctx.active_task_id),
            requested_operations=ops,
            execution_mode="direct",
            requires_filesystem=False,
            requires_web=False,
            confidence=0.95,
            original_text=s,
            meta={"instruction": s, "task_id": ctx.active_task_id},
        )

    # 3. Project exploration
    proj_name = extract_project_target(s)
    if (PROJECT_EXPLORATION_RE.search(s) and ("project" in lowered or "codebase" in lowered or "repo" in lowered)) or (
        proj_name and any(w in lowered for w in ("explore", "architecture", "inspect codebase", "review project"))
    ):
        ops = extract_requested_operations(s, "project_exploration")
        return StructuredRequest(
            request_type="project_exploration",
            intent="dev_architecture" if any(w in lowered for w in ("architecture", "design", "explain")) else "inspect_project",
            target=RequestTarget(type="project", name=proj_name or ctx.active_project_name or "current_project", path=ctx.active_project_path or ""),
            requested_operations=ops,
            execution_mode="background",
            requires_filesystem=True,
            requires_web=False,
            requires_confirmation=False,
            confidence=0.92,
            original_text=s,
            meta={"project_name": proj_name, "active_inherited": bool(not proj_name and ctx.active_project_name)},
        )

    # 4. Contextual follow-up referring to active project
    if (FOLLOW_UP_PRONOUN_RE.search(s) or (len(s.split()) <= 6 and any(w in lowered for w in ("backend", "frontend", "database", "models", "tests")))) and (
        ctx.active_project_path or ctx.active_project_name
    ):
        ops = extract_requested_operations(s, "follow_up")
        sub_item = next((w for w in ("backend", "frontend", "database", "tests", "models", "schema") if w in lowered), None)
        return StructuredRequest(
            request_type="follow_up",
            intent="dev_architecture" if any(w in lowered for w in ("explain", "how", "what")) else "filesystem_ops",
            target=RequestTarget(
                type="project",
                name=ctx.active_project_name or "active_project",
                path=ctx.active_project_path or "",
                sub_item=sub_item,
            ),
            requested_operations=ops,
            execution_mode="direct" if len(s.split()) <= 6 and "explain" not in lowered else "background",
            requires_filesystem=True,
            requires_web=False,
            confidence=0.88,
            original_text=s,
            meta={"inherited_project": ctx.active_project_name, "inherited_path": ctx.active_project_path},
        )

    # 5. Coding requests
    if CODING_ACTION_RE.search(s) and any(w in lowered for w in ("code", "function", "test", "bug", "implement", "refactor")):
        ops = ["implement_code", "verify_syntax"]
        return StructuredRequest(
            request_type="coding",
            intent="dev_coding",
            target=RequestTarget(type="project", name=ctx.active_project_name or "", path=ctx.active_project_path or ""),
            requested_operations=ops,
            execution_mode="background",
            requires_filesystem=True,
            requires_confirmation=False,
            confidence=0.90,
            original_text=s,
        )

    # 6. Deep Research requests
    if RESEARCH_CUES_RE.search(s):
        ops = extract_requested_operations(s, "research")
        return StructuredRequest(
            request_type="research",
            intent="web_research",
            target=RequestTarget(type="none"),
            requested_operations=ops,
            execution_mode="background" if ("deep" in lowered or "report" in lowered) else "direct",
            requires_filesystem=False,
            requires_web=True,
            confidence=0.91,
            original_text=s,
        )

    # 7. Pure Question & Explanation (Knowledge QA, technical definitions, hardware QA)
    # MUST be execution_mode="direct" with NO background task started
    if QUESTION_EXPLANATION_RE.search(s) or (
        any(w in lowered for w in ("difference between", "how to", "what does", "can you explain"))
        and not any(w in lowered for w in ("delete", "remove", "open app", "schedule", "save this"))
    ):
        return StructuredRequest(
            request_type="question_explanation",
            intent="general_chat",
            target=RequestTarget(type="none"),
            requested_operations=["synthesize_explanation"],
            execution_mode="direct",
            requires_filesystem=False,
            requires_web=False,
            requires_confirmation=False,
            confidence=0.94,
            original_text=s,
        )

    # 8. Greetings & Casual Conversation
    # MUST be execution_mode="direct" with NO background task started
    if GREETING_CASUAL_RE.match(s) or (len(s.split()) <= 3 and any(w in lowered for w in ("hello", "hi", "hey", "thanks", "ok", "cool"))):
        return StructuredRequest(
            request_type="general_conversation",
            intent="general_chat",
            target=RequestTarget(type="none"),
            requested_operations=["conversational_reply"],
            execution_mode="direct",
            requires_filesystem=False,
            requires_web=False,
            requires_confirmation=False,
            confidence=0.98,
            original_text=s,
        )

    # 9. Fallback / Standard conversation turn
    return StructuredRequest(
        request_type="general_conversation",
        intent="general_chat",
        target=RequestTarget(type="none"),
        requested_operations=["conversational_reply"],
        execution_mode="direct",
        requires_filesystem=False,
        requires_web=False,
        requires_confirmation=False,
        confidence=0.85,
        original_text=s,
    )


def is_conversational_turn(req: StructuredRequest) -> bool:
    """Return True if request is a direct conversational turn that should NOT start a background task."""
    return req.execution_mode == "direct" and req.request_type in (
        "general_conversation",
        "question_explanation",
        "clarification_response",
        "task_steering",
    )

