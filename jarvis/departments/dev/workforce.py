"""Multi-project registry & Dev Workforce verification/review services."""
from pathlib import Path
import re
import time
import uuid

from ...db.engine import DatabaseManager
from ...db.models import ProjectGraphRow, ProjectRow, WorktreeRow
from ...integrations import git_worktree, graphify_adapter
from ...security.pii_firewall import detect_secret

_UNSAFE_CODE_PATTERNS = [
    ("shell_true", re.compile(r"shell\s*=\s*True")),
    ("verify_false", re.compile(r"verify\s*=\s*False")),
    ("eval_exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
]


class ProjectRegistryService:
    """Explicit multi-project registry preventing blind filesystem scans outside registered repos."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def register(
        self,
        *,
        name: str,
        root_path: str | Path,
        default_branch: str = "main",
        test_cmd: str = "pytest -q",
        lint_cmd: str = "ruff check",
    ) -> dict:
        root = Path(root_path).resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Project root directory does not exist: {root}")
        pid = uuid.uuid4().hex
        stack = {
            "python": any((root / f).exists() for f in ("pyproject.toml", "requirements.txt", "setup.py")),
            "node": (root / "package.json").exists(),
            "docker": (root / "Dockerfile").exists(),
            "git": (root / ".git").exists(),
        }
        with self.db.sync_session() as s:
            existing = s.query(ProjectRow).filter((ProjectRow.name == name) | (ProjectRow.root_path == str(root))).first()
            if existing:
                existing.default_branch = default_branch
                existing.test_cmd = test_cmd
                existing.lint_cmd = lint_cmd
                existing.tech_stack = stack
                s.commit()
                return self._to_dict(existing)
            row = ProjectRow(
                id=pid,
                name=name,
                root_path=str(root),
                default_branch=default_branch,
                test_cmd=test_cmd,
                lint_cmd=lint_cmd,
                tech_stack=stack,
            )
            s.add(row)
            s.commit()
            return self._to_dict(row)

    def list_projects(self) -> list[dict]:
        with self.db.sync_session() as s:
            rows = s.query(ProjectRow).order_by(ProjectRow.created_at.desc()).all()
            return [self._to_dict(r) for r in rows]

    def get(self, project_id: str) -> dict:
        with self.db.sync_session() as s:
            r = s.get(ProjectRow, project_id)
            if r is None:
                raise KeyError(project_id)
            return self._to_dict(r)

    def sync_graphify(self, project_id: str, *, bin_name: str = "graphify", no_cluster: bool = True) -> dict:
        proj = self.get(project_id)
        root = Path(proj["root_path"])
        res = graphify_adapter.update_graph(root, bin_name=bin_name, no_cluster=no_cluster)
        sha = git_worktree.git_head_sha(root)
        with self.db.sync_session() as s:
            row = s.get(ProjectRow, project_id)
            if row:
                row.graphify_status = "ready"
                row.graphify_last_built_at = round(time.time(), 3)
            s.add(
                ProjectGraphRow(
                    project_id=project_id,
                    git_commit_sha=sha,
                    node_count=res["node_count"],
                    edge_count=res["edge_count"],
                    artifact_path=res["artifact_path"],
                    summary={"stdout": res["stdout"][:1000]},
                )
            )
            s.commit()
        return res

    @staticmethod
    def _to_dict(r: ProjectRow) -> dict:
        return {
            "id": r.id,
            "name": r.name,
            "root_path": r.root_path,
            "default_branch": r.default_branch,
            "test_cmd": r.test_cmd,
            "lint_cmd": r.lint_cmd,
            "tech_stack": r.tech_stack or {},
            "graphify_status": r.graphify_status,
            "graphify_last_built_at": r.graphify_last_built_at,
        }


class DevWorkforceService:
    """Supervises isolated worktrees, QA test verification, and pre-merge security/diff auditing."""

    def __init__(self, db: DatabaseManager, projects: ProjectRegistryService):
        self.db = db
        self.projects = projects

    def open_worktree(self, project_id: str, task_id: str, worktree_root_name: str = ".jarvis_worktrees") -> dict:
        proj = self.projects.get(project_id)
        wt = git_worktree.create_worktree(Path(proj["root_path"]), task_id, worktree_root_name=worktree_root_name)
        with self.db.sync_session() as s:
            s.add(
                WorktreeRow(
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    run_id=task_id,
                    branch_name=wt["branch"],
                    worktree_path=wt["worktree_path"],
                    status="active",
                )
            )
            s.commit()
        return wt

    def security_scan_diff(self, diff_text: str) -> dict:
        """Scan unified git diff for hardcoded credentials or unsafe execution patterns before merge approval."""
        findings: list[dict] = []
        added_lines = [ln[1:] for ln in (diff_text or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        joined = "\n".join(added_lines)
        if sec := detect_secret(joined):
            findings.append({"severity": "critical", "type": "secret_leak", "detail": sec})
        for label, pat in _UNSAFE_CODE_PATTERNS:
            if pat.search(joined):
                findings.append({"severity": "warning", "type": label, "detail": f"Added code matches {label}"})
        return {"clean": not any(f["severity"] == "critical" for f in findings), "findings": findings}

    def list_worktrees(self, project_id: str | None = None) -> list[dict]:
        with self.db.sync_session() as s:
            q = s.query(WorktreeRow)
            if project_id:
                q = q.filter(WorktreeRow.project_id == project_id)
            rows = q.order_by(WorktreeRow.created_at.desc()).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "run_id": r.run_id,
                    "branch_name": r.branch_name,
                    "worktree_path": r.worktree_path,
                    "status": r.status,
                }
                for r in rows
            ]

