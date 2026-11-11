"""Layer 1 Deterministic Fast Rules (< 5 ms): slash commands, hardware controls, safety shortcuts."""
import re
import time

from .schemas import DecisionEnvelope

SLASH_CMD = re.compile(r"^/([a-z0-9_-]+)(?:\s+(.*))?$", re.I | re.S)
HARD_STOP = re.compile(r"^\W*(?:jarvis\W+)?(?:stop(?:\s+talking|\s+speaking)?|quiet|silence|cancel)\W*$", re.I)
DEV_KEYWORDS = re.compile(
    r"\b(?:repo(?:sitory)?|codebase|refactor|implement|bug\s*fix|unit\s*test|pytest|endpoint|"
    r"migration|alembic|worktree|opencode|graphify|architecture|pull\s+request|git\s+diff|backend|frontend)\b",
    re.I,
)
DOC_EXPORT_RE = re.compile(
    r"\b(?:generate|create|export|build|write|make)\s+(?:a\s+|an\s+)?(?:formatted\s+)?(pdf|docx|word|pptx|powerpoint|slides?|xlsx|excel|spreadsheet)\b",
    re.I,
)
REMINDER_RE = re.compile(r"\b(?:remind\s+me|set\s+(?:a\s+)?reminder|schedule\s+(?:a\s+)?reminder|my\s+reminders)\b", re.I)
WEB_RESEARCH_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|look\s+up\s+online|latest\s+news|weather\b|stock\s+price|current\s+version)\b",
    re.I,
)


def evaluate_layer1(text: str) -> DecisionEnvelope | None:
    """Evaluate sub-5ms deterministic slash commands & immediate hardware/safety controls."""
    t0 = time.perf_counter()
    s = (text or "").strip()
    if not s:
        return DecisionEnvelope(layer=1, department="system", intent="empty", confidence=1.0, fast_reply="", latency_ms=0.0)

    if HARD_STOP.match(s):
        return DecisionEnvelope(
            layer=1,
            department="system",
            intent="hardware_stop",
            complexity="trivial",
            risk_tier="READ",
            confidence=0.99,
            command_payload={"action": "stop"},
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    if m := SLASH_CMD.match(s):
        cmd = m.group(1).lower()
        arg = (m.group(2) or "").strip()
        return DecisionEnvelope(
            layer=1,
            department="system" if cmd in ("status", "mode", "model", "backup", "help") else ("dev" if cmd in ("project", "code", "graph") else "assistant"),
            intent=f"slash_{cmd}",
            complexity="trivial",
            needs_project_context=(cmd in ("project", "code", "graph")),
            risk_tier="EXECUTE" if cmd in ("backup", "restore") else "READ",
            confidence=0.99,
            command_payload={"slash": cmd, "arg": arg},
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    return None

