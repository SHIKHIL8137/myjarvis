"""Canonical domain error hierarchy & structured API error envelopes."""
from dataclasses import dataclass, field


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


class PermissionDeniedError(JarvisError):
    status_code = 403
    code = "permission_denied"


class PolicyBlockedError(JarvisError):
    status_code = 403
    code = "policy_blocked"


class ResourceNotFoundError(JarvisError):
    status_code = 404
    code = "not_found"


class ServiceUnavailableError(JarvisError):
    status_code = 503
    code = "service_unavailable"


@dataclass
class ErrorEnvelope:
    error: str
    code: str = "error"
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict = {"error": self.error, "code": self.code}
        if self.details:
            out["details"] = self.details
        return out

