"""Structured correlation context propagation & secret-redacting logging filters."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from ..security.pii_firewall import redact_secrets

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_correlation_id", default="-")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_request_id", default="-")
task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_task_id", default="-")
routine_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_routine_id", default="-")
agent_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_agent_id", default="-")

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "key", "token", "secret", "subscription-key", "auth", "password"}
_AUTH_KV_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)\s*([:=])\s*(['\"]?)([^\s,;'\"]{4,})\3"
)
_URL_IN_LOG_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def new_correlation_id(prefix: str = "corr") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def sanitize_correlation_id(raw: str | None, *, prefix: str = "corr") -> str:
    cleaned = (raw or "").strip()
    if cleaned and _CORRELATION_ID_RE.match(cleaned):
        return cleaned
    return new_correlation_id(prefix=prefix)


def get_correlation_context() -> dict[str, str]:
    return {
        "correlation_id": correlation_id_var.get(),
        "request_id": request_id_var.get(),
        "task_id": task_id_var.get(),
        "routine_id": routine_id_var.get(),
        "agent_id": agent_id_var.get(),
    }


@contextmanager
def correlation_scope(
    *,
    correlation_id: str | None = None,
    request_id: str | None = None,
    task_id: str | None = None,
    routine_id: str | None = None,
    agent_id: str | None = None,
) -> Iterator[dict[str, str]]:
    """Bind correlation metadata across sync and async execution boundaries."""
    tokens: list[tuple[contextvars.ContextVar[str], contextvars.Token[str]]] = []
    if correlation_id is not None:
        tokens.append((correlation_id_var, correlation_id_var.set(sanitize_correlation_id(correlation_id))))
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(str(request_id)[:64] or "-")))
    if task_id is not None:
        tokens.append((task_id_var, task_id_var.set(str(task_id)[:64] or "-")))
    if routine_id is not None:
        tokens.append((routine_id_var, routine_id_var.set(str(routine_id)[:64] or "-")))
    if agent_id is not None:
        tokens.append((agent_id_var, agent_id_var.set(str(agent_id)[:64] or "-")))
    try:
        yield get_correlation_context()
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def sanitize_log_text(text: Any) -> str:
    """Redact API keys, Bearer tokens, credentials, and sensitive URL query parameters before logging."""
    s, _ = redact_secrets(str(text or ""))
    s = _AUTH_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}", s)

    def _clean_url(m: re.Match[str]) -> str:
        raw_url = m.group(0)
        try:
            parts = urlsplit(raw_url)
            q = [(k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            host = parts.hostname or ""
            netloc = f"[{host}]" if ":" in host else host
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            if q or parts.username or parts.password:
                return urlunsplit((parts.scheme, netloc, parts.path, urlencode(q), parts.fragment))
        except Exception:
            pass
        return raw_url

    return _URL_IN_LOG_RE.sub(_clean_url, s)


class CorrelationAndRedactionFilter(logging.Filter):
    """Inject correlation IDs into every LogRecord and scrub secrets from formatted arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_correlation_context()
        for k, v in ctx.items():
            if not getattr(record, k, None):
                setattr(record, k, v)
        try:
            if record.args:
                if isinstance(record.args, dict):
                    cleaned_args: Any = {
                        k: (sanitize_log_text(v) if isinstance(v, str) else v) for k, v in record.args.items()
                    }
                    record.args = cleaned_args
                elif isinstance(record.args, tuple):
                    record.args = tuple(sanitize_log_text(a) if isinstance(a, str) else a for a in record.args)
            if isinstance(record.msg, str):
                record.msg = sanitize_log_text(record.msg)
        except Exception:
            pass
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Compact single-line JSON formatter with redacted message and correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", correlation_id_var.get()),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "task_id": getattr(record, "task_id", task_id_var.get()),
            "routine_id": getattr(record, "routine_id", routine_id_var.get()),
            "agent_id": getattr(record, "agent_id", agent_id_var.get()),
            "message": sanitize_log_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = sanitize_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
