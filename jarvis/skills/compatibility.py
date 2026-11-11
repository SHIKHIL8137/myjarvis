"""Explicit multi-runtime skill compatibility analyzer & adapters (Jarvis, OpenCode, Claude Code, Codex)."""
from pathlib import Path
import shutil

from .security import enforce_system_policy_precedence


class SkillCompatibilityError(RuntimeError):
    """Raised when a skill cannot execute safely or natively on the requested runtime."""


def inspect_runtime_compatibility(skill_dir: Path, frontmatter: dict, security_report: dict) -> dict:
    """Assess actual runtime compatibility across Jarvis, OpenCode, Claude Code & Codex without guessing."""
    base = Path(skill_dir).resolve()
    skill_md = base / "SKILL.md"
    raw_text = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
    perms = security_report.get("permission_manifest") or {}
    critical_blocked = security_report.get("verification_status") == "blocked"
    scripts = security_report.get("scripts") or []
    hooks = security_report.get("hooks") or []
    mcp_configs = security_report.get("mcp_configs") or []

    results: dict[str, dict] = {}

    # 1. Jarvis-native agents (guidance injection under system policy precedence)
    if critical_blocked:
        results["jarvis_native"] = {
            "compatible": False,
            "mode": "blocked",
            "reason": "Blocked by security scanner (critical policy override / unsafe pattern).",
        }
    else:
        results["jarvis_native"] = {
            "compatible": True,
            "mode": "instruction_only" if not scripts else "instruction_only_scripts_quarantined",
            "reason": "Markdown instructions wrapped in policy-precedence envelope.",
        }

    # 2. OpenCode (.opencode/skills/<id>/SKILL.md)
    opencode_issues: list[str] = []
    if critical_blocked:
        opencode_issues.append("Critical security findings block container materialization.")
    if "${CLAUDE_SKILL_DIR}" in raw_text:
        opencode_issues.append("References Claude-specific `${CLAUDE_SKILL_DIR}` launcher variable.")
    if hooks:
        opencode_issues.append(f"Contains Claude/IDE hooks ({len(hooks)} file(s)) unsupported inside OpenCode worktree sandbox.")
    if mcp_configs:
        opencode_issues.append(f"Bundles external MCP config ({', '.join(mcp_configs)}) disallowed in sandboxed OpenCode.")
    if perms.get("browser_automation"):
        opencode_issues.append("Requires headless Chromium/Playwright absent from default `jarvis-opencode:1.19.5` container.")

    results["opencode"] = {
        "compatible": len(opencode_issues) == 0,
        "mode": "instruction_only" if len(opencode_issues) == 0 else "incompatible",
        "incompatibilities": opencode_issues,
        "reason": "Compatible with `.opencode/skills/<id>/SKILL.md`" if not opencode_issues else "; ".join(opencode_issues),
    }

    # 3. Claude Code (.claude/skills vs plugins)
    claude_issues: list[str] = []
    if critical_blocked:
        claude_issues.append("Critical security finding detected.")
    if not (skill_md.exists()):
        claude_issues.append("Missing root `SKILL.md`.")
    results["claude_code"] = {
        "compatible": len(claude_issues) == 0,
        "mode": "plugin_with_hooks_or_mcp" if (hooks or mcp_configs) else "skill_md",
        "incompatibilities": claude_issues,
        "reason": "Valid Claude Code skill" if not claude_issues else "; ".join(claude_issues),
    }

    # 4. Codex (.agents/skills/<id>/SKILL.md — strict open SKILL.md standard)
    codex_issues: list[str] = []
    if critical_blocked:
        codex_issues.append("Critical security finding detected.")
    name_val = str(frontmatter.get("name") or "")
    desc_val = str(frontmatter.get("description") or "")
    if not name_val or not desc_val:
        codex_issues.append("Open Agent Skills standard requires both `name` and `description` YAML frontmatter fields.")
    if len(desc_val) > 1024:
        codex_issues.append("Frontmatter `description` exceeds 1024-character Codex limit.")
    if "${CLAUDE_SKILL_DIR}" in raw_text or hooks:
        codex_issues.append("Relies on Claude-specific hooks or `${CLAUDE_SKILL_DIR}` variable.")

    results["codex"] = {
        "compatible": len(codex_issues) == 0,
        "mode": "instruction_only" if len(codex_issues) == 0 else "incompatible",
        "incompatibilities": codex_issues,
        "reason": "Compatible with `.agents/skills/<id>/SKILL.md`" if not codex_issues else "; ".join(codex_issues),
    }
    return results


def adapt_skill_for_runtime(
    manifest: dict,
    skill_dir: Path,
    *,
    runtime: str,
    target_workdir: Path | None = None,
    allow_scripts: bool = False,
) -> dict:
    """Convert validated skill content for a target runtime, or fail explicitly with exact incompatibility reason."""
    sid = manifest["skill_id"]
    compat = (manifest.get("runtime_compat") or {}).get(runtime)
    if compat is not None and not compat.get("compatible"):
        raise SkillCompatibilityError(
            f"Skill {sid!r} is incompatible with runtime {runtime!r}: {compat.get('reason') or compat.get('incompatibilities')}"
        )

    skill_md = Path(skill_dir) / "SKILL.md"
    if not skill_md.exists():
        raise SkillCompatibilityError(f"Skill {sid!r} is missing SKILL.md at {skill_dir}")
    body = skill_md.read_text(encoding="utf-8", errors="replace")

    if runtime == "jarvis_native":
        return {
            "runtime": runtime,
            "skill_id": sid,
            "prompt_block": enforce_system_policy_precedence(sid, body, runtime="jarvis_native"),
            "materialized_files": [],
        }

    if runtime in ("opencode", "claude_code", "codex"):
        subdir = ".opencode/skills" if runtime == "opencode" else (".claude/skills" if runtime == "claude_code" else ".agents/skills")
        wrapped = (
            f"<!-- Jarvis System Policy Precedence Enforced | skill_id={sid} | sha256={manifest.get('content_sha256', '')} -->\n"
            + enforce_system_policy_precedence(sid, body, runtime=runtime)
        )
        materialized: list[str] = []
        if target_workdir is not None:
            dest_dir = Path(target_workdir).resolve() / subdir / sid
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "SKILL.md").write_text(wrapped, encoding="utf-8")
            materialized.append(f"{subdir}/{sid}/SKILL.md")
            ref_src = Path(skill_dir) / "references"
            if ref_src.exists() and ref_src.is_dir():
                shutil.copytree(ref_src, dest_dir / "references", dirs_exist_ok=True)
                materialized.append(f"{subdir}/{sid}/references/")
            if allow_scripts and (Path(skill_dir) / "scripts").exists():
                shutil.copytree(Path(skill_dir) / "scripts", dest_dir / "scripts", dirs_exist_ok=True)
                materialized.append(f"{subdir}/{sid}/scripts/")
        return {
            "runtime": runtime,
            "skill_id": sid,
            "prompt_block": wrapped,
            "materialized_files": materialized,
        }

    raise SkillCompatibilityError(f"Unsupported skill target runtime: {runtime!r}")

