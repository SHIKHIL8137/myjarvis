"""Canonical domain error hierarchy & structured API error envelopes."""
from dataclasses import dataclass, field

from .logging_ctx import correlation_id_var


class JarvisError(Exception):
    """Base application error carrying deterministic HTTP status & machine code."""

    status_code: int = 400
    code: str = "jarvis_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = details or {}


class ValidationDomainError(JarvisError):
    status_code = 422
    code = "validation_error"


class PermissionDeniedError(JarvisError):
    status_code = 403
    code = "permission_denied"


class PolicyBlockedError(JarvisError):
    status_code = 403
    code = "policy_blocked"


class ResourceNotFoundError(JarvisError):
    status_code = 404
    code = "not_found"


class ConflictError(JarvisError):
    status_code = 409
    code = "conflict"


class TaskTransitionError(ConflictError):
    code = "invalid_task_transition"


class UndoConflictError(ConflictError):
    code = "undo_conflict"


class TimeoutExceededError(JarvisError):
    status_code = 408
    code = "timeout"


class RateLimitError(JarvisError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailableError(JarvisError):
    status_code = 503
    code = "service_unavailable"


@dataclass
class ErrorEnvelope:
    error: str
    code: str = "error"
    details: dict = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: correlation_id_var.get())
    detail: str | None = None

    def to_dict(self, *, include_detail: bool = False) -> dict:
        out: dict = {
            "error": self.error,
            "code": self.code,
            "correlation_id": self.correlation_id or correlation_id_var.get(),
        }
        if include_detail:
            out["detail"] = self.detail if self.detail is not None else self.error
        if self.details:
            out["details"] = self.details
        return out
