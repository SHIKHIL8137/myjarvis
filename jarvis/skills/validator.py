"""Skill validation, deterministic SHA-256 verification, license gate & install report generation."""
from pathlib import Path
import time

from .compatibility import inspect_runtime_compatibility
from .metadata import SkillManifest, compute_directory_sha256, normalize_skill_id, parse_skill_markdown
from .security import inspect_skill_security


class SkillValidationError(ValueError):
    """Raised when a skill fails manifest, checksum, license, or critical security validation."""


def _detect_license(skill_dir: Path, frontmatter: dict) -> str:
    lic = str(frontmatter.get("license") or "").strip()
    if lic:
        return lic.lower().replace(" ", "-")
    for cand in (skill_dir / "LICENSE", skill_dir / "LICENSE.txt", skill_dir.parent / "LICENSE", skill_dir.parent.parent / "LICENSE"):
        if cand.exists() and cand.is_file():
            head = cand.read_text(encoding="utf-8", errors="replace")[:400].lower()
            if "mit license" in head:
                return "mit"
            if "apache license" in head and "version 2.0" in head:
                return "apache-2.0"
            if "bsd 3-clause" in head:
                return "bsd-3-clause"
            if "isc license" in head:
                return "isc"
    return "unknown"


def validate_skill_package(
    skill_dir: Path,
    *,
    skill_id: str | None = None,
    category: str = "general",
    source_repo: str = "",
    source_commit: str = "",
    license_override: str = "",
    compatible_roles: list[str] | None = None,
    expected_sha256: str = "",
    allowed_licenses: set[str] | None = None,
    allow_untrusted_scripts: bool = False,
) -> tuple[SkillManifest, dict]:
    """Validate skill structure, checksum, license, security, and runtime compatibility BEFORE installation."""
    base = Path(skill_dir).resolve()
    skill_md = base / "SKILL.md"
    if not skill_md.exists() or not skill_md.is_file():
        raise SkillValidationError(f"Skill package at {base} is missing required SKILL.md")

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_skill_markdown(text)
    if not body.strip():
        raise SkillValidationError(f"Skill {base} SKILL.md has empty instruction body.")

    raw_name = str(fm.get("name") or base.name)
    sid = normalize_skill_id(skill_id or raw_name)
    checksum = compute_directory_sha256(base)
    if expected_sha256 and checksum != expected_sha256:
        raise SkillValidationError(f"Checksum verification failed for {sid!r}: expected {expected_sha256}, got {checksum}")

    lic = (license_override or _detect_license(base, fm)).strip().lower()
    if allowed_licenses is not None:
        norm_allowed = {x.lower() for x in allowed_licenses}
        if lic not in norm_allowed:
            raise SkillValidationError(f"Skill {sid!r} license {lic!r} is not in allowed_licenses={sorted(norm_allowed)}")

    sec = inspect_skill_security(base, allow_untrusted_scripts=allow_untrusted_scripts)
    compat = inspect_runtime_compatibility(base, fm, sec)
    supported_runtimes = [rt for rt, info in compat.items() if info.get("compatible")]

    deps: list[str] = []
    if sec["scripts"]:
        if any(s.endswith((".js", ".mjs", ".cjs", ".ts")) for s in sec["scripts"]):
            deps.append("node")
        if any(s.endswith(".py") for s in sec["scripts"]):
            deps.append("python")
    if sec["permission_manifest"].get("browser_automation"):
        deps.append("chromium_playwright")

    exec_mode = "instruction_only"
    if sec["scripts"] and allow_untrusted_scripts:
        exec_mode = "script_assisted"

    now = round(time.time(), 3)
    meta_raw = fm.get("metadata")
    meta_dict = meta_raw if isinstance(meta_raw, dict) else {}
    ver = str(fm.get("version") or meta_dict.get("version") or "1.0.0")
    desc = str(fm.get("description") or body.splitlines()[0][:240]).strip()

    manifest = SkillManifest(
        skill_id=sid,
        name=raw_name,
        description=desc,
        category=category,
        version=ver,
        source_repo=source_repo,
        source_commit=source_commit,
        content_sha256=checksum,
        license=lic,
        supported_runtimes=supported_runtimes,
        compatible_roles=compatible_roles or ["coder_agent", "architect_agent", "assistant_coordinator"],
        required_dependencies=sorted(set(deps)),
        required_permissions=sec["detected_permissions"],
        network_requirements=sec["external_urls"],
        execution_mode=exec_mode,
        approval_required=True,
        installed_at=now,
        updated_at=now,
        enabled=True,
        verification_status=sec["verification_status"],
        install_path=str(base),
        keywords=[w for w in sid.split("-") if len(w) >= 2],
        security_report=sec,
        runtime_compat=compat,
    )
    report = {
        "valid": sec["verification_status"] != "blocked",
        "skill_id": sid,
        "checksum_sha256": checksum,
        "license": lic,
        "verification_status": sec["verification_status"],
        "security": sec,
        "runtime_compat": compat,
        "recommendation": (
            "REJECT / BLOCKED: Critical security pattern detected."
            if sec["verification_status"] == "blocked"
            else "APPROVE INSTRUCTION-ONLY: Safe guidance with scripts/hooks quarantined."
            if sec["scripts"] or sec["hooks"]
            else "SAFE TO INSTALL: Pure validated Markdown skill with explicit owner approval."
        ),
    }
    return manifest, report

