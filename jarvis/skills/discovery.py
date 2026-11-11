"""Read-only skill discovery across local folders, project skills, & curated audited sources (never executes scripts)."""
from pathlib import Path

from .metadata import parse_skill_markdown
from .recommended_audit import RECOMMENDED_SKILL_AUDIT_CATALOG
from .validator import validate_skill_package


def discover_local_skills(
    search_roots: list[Path],
    *,
    allowed_licenses: set[str] | None = None,
) -> list[dict]:
    """Scan existing project `.opencode/skills`, `.claude/skills`, `.agents/skills`, or explicit folders without executing scripts."""
    found: list[dict] = []
    seen_dirs: set[Path] = set()
    for root in search_roots:
        rp = Path(root).resolve()
        if not rp.exists() or not rp.is_dir():
            continue
        cands = list(rp.glob("**/SKILL.md"))
        if (rp / "SKILL.md").exists():
            cands.insert(0, rp / "SKILL.md")
        for smd in cands:
            if ".git" in smd.parts or "node_modules" in smd.parts or ".jarvis_worktrees" in smd.parts:
                continue
            sdir = smd.parent.resolve()
            if sdir in seen_dirs:
                continue
            seen_dirs.add(sdir)
            fm, _ = parse_skill_markdown(smd.read_text(encoding="utf-8", errors="replace"))
            try:
                manifest, report = validate_skill_package(sdir, allowed_licenses=allowed_licenses)
                found.append({"path": str(sdir), "manifest": manifest.model_dump(), "report": report})
            except Exception as e:
                found.append({"path": str(sdir), "name": str(fm.get("name") or sdir.name), "error": str(e), "valid": False})
    return found


def discover_from_project(project_root: Path, *, allowed_licenses: set[str] | None = None) -> list[dict]:
    pr = Path(project_root).resolve()
    roots = [
        pr / ".opencode" / "skills",
        pr / ".claude" / "skills",
        pr / ".agents" / "skills",
        pr / "skills",
    ]
    return discover_local_skills(roots, allowed_licenses=allowed_licenses)


def discover_curated_catalog() -> list[dict]:
    return [dict(item) for item in RECOMMENDED_SKILL_AUDIT_CATALOG]

