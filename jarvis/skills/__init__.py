"""Provider-independent Agent Skills & Capabilities Manager."""
from .compatibility import SkillCompatibilityError, adapt_skill_for_runtime, inspect_runtime_compatibility
from .metadata import SkillManifest, compute_directory_sha256, normalize_skill_id, parse_skill_markdown
from .recommended_audit import (
    RECOMMENDED_SKILL_AUDIT_CATALOG,
    SKILL_AUDIT_CLASSIFICATIONS,
    enrich_catalog_with_registry,
    get_audit_report,
    list_audit_reports,
)
from .registry import SkillRegistryService
from .router import AgentCapabilityRouter
from .security import enforce_system_policy_precedence, inspect_skill_security
from .validator import SkillValidationError, validate_skill_package

__all__ = [
    "AgentCapabilityRouter",
    "RECOMMENDED_SKILL_AUDIT_CATALOG",
    "SKILL_AUDIT_CLASSIFICATIONS",
    "SkillCompatibilityError",
    "SkillManifest",
    "SkillRegistryService",
    "SkillValidationError",
    "adapt_skill_for_runtime",
    "compute_directory_sha256",
    "enforce_system_policy_precedence",
    "enrich_catalog_with_registry",
    "get_audit_report",
    "inspect_runtime_compatibility",
    "inspect_skill_security",
    "list_audit_reports",
    "normalize_skill_id",
    "parse_skill_markdown",
    "validate_skill_package",
]

