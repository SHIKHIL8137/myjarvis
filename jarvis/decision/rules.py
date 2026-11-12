"""Layer 1 Deterministic Fast Rules (< 5 ms), language detection & hard security policy boundaries."""
import re
import time

from .schemas import DecisionEnvelope

SLASH_CMD = re.compile(r"^/([a-z0-9_-]+)(?:\s+(.*))?$", re.I | re.S)
HARD_STOP = re.compile(r"^\W*(?:jarvis\W+)?(?:stop(?:\s+talking|\s+speaking)?|quiet|silence|cancel)\W*$", re.I)
DEV_KEYWORDS = re.compile(
    r"\b(?:repo(?:sitory)?|codebase|refactor|implement|bug\s*fix|unit\s*test|pytest|endpoint|"
    r"migration|alembic|worktree|opencode|graphify|architecture|pull\s+request|git\s+diff|backend|frontend|"
    r"fix\s+(?:the\s+|this\s+)?bug|debug\s+(?:the\s+|this\s+)?(?:error|failure|issue|module)|"
    r"find\s+bugs|(?:review|analyze|inspect|audit|check)\s+(?:(?:my|the|this)\s+)?(?:[A-Za-z0-9_.-]+\s+)?"
    r"(?:project|repo|codebase|app|code)|project(?:'s)?\b[^\n]{0,40}\b(?:code|tests?|bugs?|modules?))\b",
    re.I,
)
DOC_EXPORT_RE = re.compile(
    r"\b(?:generate|create|export|build|write|make|prepare)\s+(?:a\s+|an\s+|me\s+a\s+)?(?:formatted\s+|summary\s+)?(pdf|docx|word|pptx|powerpoint|slides?|xlsx|excel|spreadsheet)\b",
    re.I,
)
REMINDER_RE = re.compile(
    r"\b(?:remind\s+me|set\s+(?:a\s+)?reminder|schedule\s+(?:a\s+)?reminder|(?:list|show|what\s+are)\s+(?:(?:all|my|pending|active)\s+)*reminders?)\b",
    re.I,
)
WEB_RESEARCH_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|look\s+up\s+online|latest\s+news|weather\b|stock\s+price|current\s+version)\b",
    re.I,
)
FILESYSTEM_OPS_RE = re.compile(
    r"\b(?:(?:list|show|find|search|inspect|read|open|create|write|edit|save|delete)\s+"
    r"(?:(?:[A-Za-z0-9_.-]+\s+){0,4})?"
    r"(?:files?|folders?|directories|projects?|workspace\s+files?|pdf\s+documents?|documents?|downloads?|desktop|reports?\b)|"
    r"(?:list|show|find|search|inspect|read|open|create|write|edit|save|delete)\b[^\n]{0,70}"
    r"(?:my\s+|the\s+|that\s+|this\s+)?(?:documents|downloads|desktop|pictures|folder|directory|project)\b|"
    r"(?:list|show|find|search|inspect|read|open)\s+(?:its\s+|the\s+)?(?:backend|frontend|src|app|apps|packages|client|server|database|docs|tests|config)\b|"
    r"\b(?:read|open|show|inspect|check)\s+(?:its\s+|the\s+)?[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}\b[^\n]{0,40}\b(?:in|inside|from)?|"
    r"\b(?:folder|directory)\s+structure\b|"
    r"what\s+files\s+are\s+in\b|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+\\)",
    re.I,
)
TASK_STATUS_RE = re.compile(
    r"\b(?:(?:check|list|show|what\s+are|status\s+of)\s+(?:(?:all|my|the|current|active|pending|running)\s+)*tasks?)\b",
    re.I,
)
HOST_FOLDER_TARGET_RE = re.compile(
    r"(?:[A-Za-z]:\\|\\\\[^\s]+|"
    r"\b(?:my\s+|the\s+|that\s+|this\s+|installed\s+)?(?:documents|downloads|desktop|pictures|folder|directory|project)\b|"
    r"\b(?:folder|directory)\s+structure\b|"
    r"\b(?:in|inside|of)\s+(?:that|this|the)\s+(?:project|folder|directory)\b|"
    r"\bits\s+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}\b)",
    re.I,
)
MEMORY_MANAGE_RE = re.compile(
    r"\b(?:(?:list|show|inspect|search|check)\s+(?:(?:all|my|personal)\s+)*memor(?:y|ies)|remember\s+this\b|save\s+this\s+to\s+memory\b)\b",
    re.I,
)
DOCKER_OPS_RE = re.compile(
    r"\b(?:docker\s+(?:status|daemon|containers?|images?|ps|info|sandbox)|(?:check|show|is|verify)\s+docker\b)\b",
    re.I,
)
RECOVERY_OPS_RE = re.compile(
    r"\b(?:(?:list|show|check|verify|status\s+of)\s+(?:(?:all|latest|my)\s+)*(?:backups?|snapshots?|recovery(?:\s+targets?)?|database\s+targets?)|disaster\s+recovery\s+status)\b",
    re.I,
)
SYSTEM_STATUS_RE = re.compile(
    r"\b(?:(?:check|show|monitor|report|what\s+is)\s+(?:the\s+|current\s+|my\s+)?(?:system\s+(?:health|status|metrics|resources)|cpu|ram|memory\s+usage|gpu|vram|audio\s+devices?|hardware\s+status)|system\s+(?:status|health)\b)\b",
    re.I,
)

# Common Manglish / Malayalam Romanized cues (word boundaries only, avoids English collisions)
_MANGLISH_RE = re.compile(
    r"\b(?:cheyyu|cheyyamo|cheyyanam|tharu|tharumo|parayu|parayamo|kanikku|nokku|nokkamo|"
    r"enthaanu|entha|evide|eppozha|ippo|njan|ente|namukku|nammal|vendam|venda|venam|mathi|"
    r"aano|alle|ille|undoo|undo|ormippikku|ormipikku|kandupidi|thedu|sahayikku)\b",
    re.I,
)

_MALAYALAM_RE = re.compile(r"[\u0D00-\u0D7F]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

# Educational / negated phrasing (e.g. "Do not delete...", "Explain what pg_restore does")
_NEGATION_PREFIX_RE = re.compile(
    r"(?:^|\b)(?:do\s+not|don'?t|never|avoid|without|explain\s+(?:what|how)|what\s+is|describe\s+how|venda|cheyyaruthu|വേണ്ട|ചെയ്യരുത്)\b",
    re.I,
)

_SENSITIVE_RULES: list[tuple[str, re.Pattern]] = [
    ("shell_execution", re.compile(r"\b(?:run\s+(?:shell|bash|powershell|cmd|terminal)|subprocess|os\.system|exec(?:ute)?\s+command|bash\s+-c)\b", re.I)),
    ("file_deletion", re.compile(r"\b(?:delete\s+(?:(?:the|all|my)\s+)*files?|remove\s+(?:(?:the|all|my)\s+)*files?|rm\s+-rf|rmdir|unlink|wipe\s+(?:directory|disk|folder)|del\s+/f|ഡിലീറ്റ്|നീക്കം)\b", re.I)),
    ("database_migration", re.compile(r"\b(?:alembic\s+(?:upgrade|downgrade)|run\s+(?:db\s+)?migration|drop\s+table|alter\s+table|truncate\s+table)\b", re.I)),
    ("database_restore", re.compile(r"\b(?:pg_restore|restore\s+(?:prod(?:uction)?\s+)?database|restore\s+postgres|overwrite\s+(?:live\s+)?db)\b", re.I)),
    ("git_commit_or_merge", re.compile(r"\b(?:git\s+(?:commit|merge|push|rebase|reset\s+--hard)|merge\s+(?:branch|worktree|pr))\b", re.I)),
    ("deployment_or_publish", re.compile(r"\b(?:deploy\s+to\s+prod|publish\s+package|docker\s+push|npm\s+publish|twine\s+upload)\b", re.I)),
    ("credential_access", re.compile(r"(?:\b(?:dump|read|print|export|exfiltrate|show)\b[^\n]*(?:secrets\.json|\.env\b|api[_\s]?key|private[_\s]?key|OPENAI_API_KEY|ANTHROPIC_API_KEY))", re.I)),
    ("network_policy_change", re.compile(r"\b(?:disable\s+offline|enable\s+unrestricted\s+network|bypass\s+firewall|disable\s+egress\s+guard)\b", re.I)),
    ("skill_installation", re.compile(r"\b(?:install\s+(?:external\s+)?skill|enable\s+untrusted\s+scripts?|install\s+plugin\s+framework)\b", re.I)),
    ("privilege_escalation", re.compile(r"(?:\bsudo\b|chmod\s+4755|--privileged|/var/run/docker\.sock|\bbypass\s+(?:human\s+)?approval\b)", re.I)),
]


def detect_text_language(text: str) -> str:
    """Detect English (`en`), pure Malayalam (`ml`), or Malayalam-English code-mixed (`manglish`)."""
    s = (text or "").strip()
    if not s:
        return "en"
    has_ml = bool(_MALAYALAM_RE.search(s))
    has_latin = bool(_LATIN_WORD_RE.search(s))
    if has_ml and has_latin:
        return "manglish"
    if has_ml:
        return "ml"
    if _MANGLISH_RE.search(s):
        return "manglish"
    return "en"


def detect_sensitive_operations(text: str) -> list[str]:
    """Identify sensitive execution/privilege categories in user requests, respecting clear negations/questions."""
    s = (text or "").strip()
    if not s:
        return []
    lowered = s.lower()
    is_negated_or_doc = bool(_NEGATION_PREFIX_RE.search(s)) and not re.search(
        r"\b(?:now|immediately|right\s+now|run\s+it|execute\s+it)\b", lowered
    )
    if is_negated_or_doc and len(s) < 220:
        # Still never treat obvious credential dump / privilege escalation phrases casually
        pass
    hits: list[str] = []
    for cat, pat in _SENSITIVE_RULES:
        m = pat.search(s)
        if not m:
            continue
        # Check a local negation/explanation window immediately before the match
        prefix_window = s[max(0, m.start() - 32) : m.start()]
        if _NEGATION_PREFIX_RE.search(prefix_window) and cat in ("file_deletion", "shell_execution", "database_migration", "database_restore", "git_commit_or_merge"):
            continue
        hits.append(cat)
    return hits


def enforce_security_boundary(text: str, env: DecisionEnvelope) -> DecisionEnvelope:
    """Hard Phase 5 boundary: classifiers may NEVER directly authorize sensitive/privileged actions."""
    sensitive = detect_sensitive_operations(text)
    if sensitive:
        env.sensitive_flags = list(sensitive)
        env.risk_tier = "EXECUTE"
        env.requires_owner_approval = True
        env.laya_route = "blocked_by_policy"
        if env.department == "chat":
            env.department = "system"
        if env.intent == "general_chat":
            env.intent = "sensitive_action_request"
        if "security_policy_boundary_enforced" not in env.notes:
            env.notes.append("security_policy_boundary_enforced")
    return env


def evaluate_layer1(text: str) -> DecisionEnvelope | None:
    """Evaluate sub-5ms deterministic slash commands & immediate hardware/safety controls."""
    t0 = time.perf_counter()
    s = (text or "").strip()
    lang = detect_text_language(s)
    if not s:
        return DecisionEnvelope(
            layer=1,
            department="system",
            intent="empty",
            confidence=1.0,
            fast_reply="",
            latency_ms=0.0,
            language=lang,
            classifier_source="layer1_rules",
        )

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
            language=lang,
            classifier_source="layer1_rules",
        )

    if m := SLASH_CMD.match(s):
        cmd = m.group(1).lower()
        arg = (m.group(2) or "").strip()
        is_exec = cmd in ("backup", "restore")
        return DecisionEnvelope(
            layer=1,
            department="system" if cmd in ("status", "mode", "model", "backup", "restore", "help") else ("dev" if cmd in ("project", "code", "graph") else "assistant"),
            intent=f"slash_{cmd}",
            complexity="trivial",
            needs_project_context=(cmd in ("project", "code", "graph")),
            risk_tier="EXECUTE" if is_exec else "READ",
            requires_owner_approval=(cmd == "restore"),
            confidence=0.99,
            command_payload={"slash": cmd, "arg": arg},
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="layer1_rules",
        )

    return None
