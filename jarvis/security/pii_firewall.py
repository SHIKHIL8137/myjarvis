"""Centralized secret/credential detection and outbound PII/Secret egress firewall."""
import re

_SECRET_PATTERNS = [
    (re.compile(r"\bsk-(?:or-v1-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"), "OpenAI/OpenRouter API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "Anthropic API key"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "Hugging Face token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "GitHub token"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), "Google API key"),
    (re.compile(r"\btvly-[A-Za-z0-9\-_]{12,}\b"), "Tavily API key"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----[\s\S]*?(?:-----END [^\n]+-----|$)"), "Private key block"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", re.I), "Bearer token"),
    (
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^:\s/@'\"]+:([^\s/@'\"]{4,})@"),
        "URI/Database credential",
    ),
    (
        re.compile(r"(?i)\b(?:password|passwd|pwd|api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?([^\s'\"]{6,})"),
        "Credential/password assignment",
    ),
]

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def detect_secret(text: str) -> str | None:
    """Return description if `text` appears to contain credentials/tokens/passwords, else None."""
    s = text or ""
    for pat, label in _SECRET_PATTERNS:
        if pat.search(s):
            return label
    return None


def redact_secrets(text: str, *, mask_email: bool = False) -> tuple[str, list[str]]:
    """Scrub secret tokens (and optionally email addresses) before outbound transmission or training."""
    out = text or ""
    hits: list[str] = []
    for pat, label in _SECRET_PATTERNS:
        if pat.search(out):
            hits.append(label)
            if "Credential/password" in label or "URI/Database" in label:
                out = pat.sub(lambda m: m.group(0).replace(m.group(1), "[REDACTED-SECRET]"), out)
            else:
                out = pat.sub(f"[REDACTED:{label}]", out)
    if mask_email and _EMAIL_RE.search(out):
        hits.append("Email address")
        out = _EMAIL_RE.sub("[redacted-email]", out)
    return out, hits


def scrub_messages(messages: list[dict], *, mask_email: bool = False) -> tuple[list[dict], list[str]]:
    """Deep-scrub outbound chat messages before leaving localhost for any cloud provider."""
    scrubbed: list[dict] = []
    all_hits: list[str] = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            clean, hits = redact_secrets(content, mask_email=mask_email)
            all_hits.extend(hits)
            scrubbed.append({**m, "content": clean})
        else:
            scrubbed.append(dict(m))
    return scrubbed, sorted(set(all_hits))

