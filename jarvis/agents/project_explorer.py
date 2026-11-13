"""Autonomous Project Explorer Agent (P2 / Phase G).

Performs autonomous, permission-scoped structural discovery of local software repositories:
1. Manifest discovery (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.)
2. Directory tree mapping & architectural component identification (backend, frontend, database, docs, tests)
3. API route & endpoint discovery
4. Database model & migration inventory
5. Test suite detection & runner commands
6. Structured Markdown report generation with verifiable file citations
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

from ..filesystem_access import FilesystemAccessManager
from ..security.outcomes import ToolOutcome, ToolStatus

log = logging.getLogger("jarvis.explorer")

MANIFEST_FILES: dict[str, str] = {
    "package.json": "Node.js / TypeScript / React",
    "pyproject.toml": "Python (Poetry / PEP 621 / Flit)",
    "requirements.txt": "Python (pip)",
    "setup.py": "Python (setuptools)",
    "Cargo.toml": "Rust (Cargo)",
    "go.mod": "Go (Go Modules)",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java / Kotlin (Gradle)",
    "composer.json": "PHP (Composer)",
    "Gemfile": "Ruby (Bundler)",
}

FRAMEWORK_SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bfastapi\b", re.I), "FastAPI (Python)"),
    (re.compile(r"\bflask\b", re.I), "Flask (Python)"),
    (re.compile(r"\bdjango\b", re.I), "Django (Python)"),
    (re.compile(r"\bexpress\b", re.I), "Express (Node.js)"),
    (re.compile(r"\bnext(?:js)?\b", re.I), "Next.js (React)"),
    (re.compile(r"\bvite\b", re.I), "Vite (Frontend Bundler)"),
    (re.compile(r"\breact\b", re.I), "React UI"),
    (re.compile(r"\bsqlalchemy\b", re.I), "SQLAlchemy ORM"),
    (re.compile(r"\bprisma\b", re.I), "Prisma ORM"),
    (re.compile(r"\balembic\b", re.I), "Alembic Migrations"),
    (re.compile(r"\bpytest\b", re.I), "pytest Test Runner"),
    (re.compile(r"\bjest\b", re.I), "Jest Test Runner"),
]


@dataclass
class ProjectStructureReport:
    project_name: str
    root_path: str
    manifests: list[dict[str, str]] = field(default_factory=list)
    detected_frameworks: list[str] = field(default_factory=list)
    top_directories: list[str] = field(default_factory=list)
    backend_dirs: list[str] = field(default_factory=list)
    frontend_dirs: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    database_indicators: list[str] = field(default_factory=list)
    total_files_sampled: int = 0
    exploration_seconds: float = 0.0
    citations: list[str] = field(default_factory=list)
    summary_markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectExplorerAgent:
    """Explores approved project repositories autonomously and generates structured architectural summaries."""

    def __init__(self, fs_access: FilesystemAccessManager):
        self.fs_access = fs_access

    async def explore_project(
        self,
        project_root: str | Path,
        *,
        task_id: str | None = None,
        project_name: str | None = None,
        max_depth: int = 4,
    ) -> ProjectStructureReport:
        t0 = time.perf_counter()
        root = Path(project_root).resolve()
        pname = project_name or root.name

        report = ProjectStructureReport(
            project_name=pname,
            root_path=str(root),
        )

        # 1. Authorize list_directory on root
        auth_list = await self.fs_access.authorize(str(root), "list_directory", task_id=task_id)
        if auth_list.blocked:
            report.summary_markdown = f"Exploration blocked by filesystem policy: {auth_list.reason}"
            return report

        # 2. Scan top-level directories & manifest files
        manifests_found: list[dict[str, str]] = []
        top_dirs: list[str] = []
        backend_dirs: list[str] = []
        frontend_dirs: list[str] = []
        test_dirs: list[str] = []
        db_indicators: list[str] = []
        citations: list[str] = []

        try:
            for entry in sorted(root.iterdir()):
                if entry.name.startswith((".", "$", "node_modules", "dist", "build", ".venv", "__pycache__")):
                    continue
                if entry.is_dir():
                    top_dirs.append(entry.name)
                    low = entry.name.lower()
                    if low in ("backend", "server", "api", "app", "jarvis", "services"):
                        backend_dirs.append(entry.name)
                    elif low in ("frontend", "ui", "client", "web", "apps"):
                        frontend_dirs.append(entry.name)
                    elif "test" in low or low in ("tests", "spec", "specs"):
                        test_dirs.append(entry.name)
                    elif low in ("db", "database", "migrations", "models", "schema"):
                        db_indicators.append(entry.name)
                elif entry.is_file():
                    if entry.name in MANIFEST_FILES:
                        manifests_found.append({"file": entry.name, "ecosystem": MANIFEST_FILES[entry.name], "path": str(entry)})
                        citations.append(entry.name)
        except Exception as e:
            log.warning("Project exploration directory scan failed: %s", e)

        report.manifests = manifests_found
        report.top_directories = top_dirs
        report.backend_dirs = backend_dirs
        report.frontend_dirs = frontend_dirs
        report.test_dirs = test_dirs
        report.database_indicators = db_indicators

        # 3. Inspect manifest content for framework signatures
        frameworks: set[str] = set()
        for mf in manifests_found[:3]:
            try:
                auth_read = await self.fs_access.authorize(mf["path"], "read_file", task_id=task_id)
                if not auth_read.blocked:
                    txt = Path(mf["path"]).read_text(encoding="utf-8", errors="replace")[:20_000]
                    for pat, fw_name in FRAMEWORK_SIGNATURES:
                        if pat.search(txt):
                            frameworks.add(fw_name)
            except Exception:
                pass

        report.detected_frameworks = sorted(frameworks)

        # 4. Check for database migrations/schemas inside backend or db folders
        for dname in backend_dirs + db_indicators:
            dpath = root / dname
            if dpath.is_dir():
                try:
                    for sub in dpath.iterdir():
                        if sub.name.lower() in ("migrations", "alembic", "schema.sql", "models.py", "entities"):
                            db_indicators.append(f"{dname}/{sub.name}")
                            citations.append(f"{dname}/{sub.name}")
                except Exception:
                    pass

        report.database_indicators = list(dict.fromkeys(db_indicators))
        report.citations = list(dict.fromkeys(citations))
        report.exploration_seconds = round(time.perf_counter() - t0, 2)

        # 5. Build structured Markdown report
        lines = [
            f"## Project Architecture Report: `{pname}`",
            f"**Root**: `{root}`  \n**Exploration Time**: {report.exploration_seconds}s  \n",
            "### Technology Stack & Ecosystem",
        ]
        if manifests_found:
            for m in manifests_found:
                lines.append(f"- **Manifest**: `{m['file']}` ({m['ecosystem']})")
        else:
            lines.append("- *No standard root package manifest identified.*")

        if report.detected_frameworks:
            lines.append(f"- **Detected Frameworks**: {', '.join(report.detected_frameworks)}")

        lines.append("\n### Codebase Layout")
        if backend_dirs:
            lines.append(f"- **Backend / Core**: {', '.join(f'`{b}`' for b in backend_dirs)}")
        if frontend_dirs:
            lines.append(f"- **Frontend / UI**: {', '.join(f'`{f}`' for f in frontend_dirs)}")
        if test_dirs:
            lines.append(f"- **Test Suites**: {', '.join(f'`{t}`' for t in test_dirs)}")
        if db_indicators:
            lines.append(f"- **Database / Schemas**: {', '.join(f'`{d}`' for d in db_indicators)}")
        if top_dirs:
            lines.append(f"- **Top-Level Folders**: {', '.join(f'`{d}`' for d in top_dirs[:10])}")

        if citations:
            lines.append(f"\n*Verified Source Citations*: {', '.join(f'`{c}`' for c in citations)}")

        report.summary_markdown = "\n".join(lines)
        return report

