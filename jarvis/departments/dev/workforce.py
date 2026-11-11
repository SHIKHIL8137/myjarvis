"""Multi-project registry & Dev Workforce verification, manifest scanning, and sandbox services."""
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
    ("subprocess_shell_pipe", re.compile(r"curl\s+.*\|\s*(?:sh|bash)", re.IGNORECASE)),
]

_EXTRA_SECRET_PATTERNS = [
    ("dotenv_secret", re.compile(r"^\s*(?:API_KEY|SECRET_KEY|AUTH_TOKEN|ACCESS_TOKEN|PRIVATE_KEY|DB_PASSWORD|POSTGRES_PASSWORD|DATABASE_URL|AWS_SECRET_ACCESS_KEY)\s*=\s*['\"]?[^\s'\"]{6,}", re.IGNORECASE | re.MULTILINE)),
    ("password_assignment", re.compile(r"\b(?:password|passwd|client_secret|access_token)\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE)),
]

_SENSITIVE_FILENAMES = re.compile(r"(?:^|/)(?:\.env(?:\.[^/]+)?|secrets\.json|id_rsa|id_ed25519|.*\.pem|.*\.key|.*\.pfx)$", re.IGNORECASE)


def _scan_text_block(text: str, file_hint: str = "") -> tuple[list[dict], list[str]]:
    """Scan a text block without ever placing raw secret values into findings."""
    findings: list[dict] = []
    warnings: list[str] = []
    if sec := detect_secret(text):
        findings.append({"severity": "critical", "type": "secret_leak", "file": file_hint, "detail": f"{sec} (redacted)"})
    for label, pat in _EXTRA_SECRET_PATTERNS:
        if pat.search(text):
            findings.append({"severity": "critical", "type": label, "file": file_hint, "detail": f"Detected {label} pattern (redacted)"})
    for label, pat in _UNSAFE_CODE_PATTERNS:
        if pat.search(text):
            findings.append({"severity": "warning", "type": label, "file": file_hint, "detail": f"Added code matches {label}"})
            warnings.append(f"{file_hint or 'diff'}: {label}")
    return findings, warnings


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
    """Supervises isolated worktrees, execution policies, QA verification, and manifest security scans."""

    def __init__(self, db: DatabaseManager, projects: ProjectRegistryService):
        self.db = db
        self.projects = projects

    @staticmethod
    def build_execution_policy(worktree_path: Path, workforce_spec=None) -> git_worktree.ExecutionPolicy:
        wt = Path(worktree_path).resolve()
        if workforce_spec is None:
            return git_worktree.ExecutionPolicy(allowed_workdirs=[wt])
        return git_worktree.ExecutionPolicy(
            allowed_workdirs=[wt],
            allowed_executables=set(workforce_spec.allowed_executables),
            denied_executables=set(workforce_spec.denied_executables),
            timeout_s=int(workforce_spec.qa_timeout_s),
            max_memory_mb=int(workforce_spec.max_memory_mb),
            network_mode=str(workforce_spec.network_mode),
            sandbox_backend=str(workforce_spec.sandbox_backend),
            docker_image=str(workforce_spec.docker_image),
        )

    def open_worktree(
        self,
        project_id: str,
        task_id: str,
        worktree_root_name: str = ".jarvis_worktrees",
        *,
        ensure_unique: bool = True,
    ) -> dict:
        proj = self.projects.get(project_id)
        wt = git_worktree.create_worktree(
            Path(proj["root_path"]),
            task_id,
            worktree_root_name=worktree_root_name,
            ensure_unique=ensure_unique,
        )
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

    def update_worktree_status(self, worktree_path: str | Path, status: str) -> None:
        wt = str(Path(worktree_path).resolve())
        with self.db.sync_session() as s:
            rows = s.query(WorktreeRow).filter(WorktreeRow.worktree_path == wt).all()
            for r in rows:
                r.status = status
            if rows:
                s.commit()

    def security_scan_diff(self, diff_text: str) -> dict:
        """Scan unified git diff for hardcoded credentials or unsafe execution patterns before merge approval."""
        added_lines = [ln[1:] for ln in (diff_text or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        joined = "\n".join(added_lines)
        findings, warnings = _scan_text_block(joined, file_hint="diff")
        return {"clean": not any(f["severity"] == "critical" for f in findings), "findings": findings, "warnings": warnings}

    def security_scan_worktree(self, diff_info: dict, worktree_path: Path | None = None) -> dict:
        """Scan EVERY changed/untracked/renamed/symlink/mode-changed file in the unified manifest cleanly."""
        all_findings: list[dict] = []
        all_warnings: list[str] = []

        diff_scan = self.security_scan_diff(diff_info.get("diff", "") + "\n" + diff_info.get("cached_diff", ""))
        all_findings.extend(diff_scan["findings"])
        all_warnings.extend(diff_scan["warnings"])

        wt = Path(worktree_path).resolve() if worktree_path else None
        manifest = list(diff_info.get("manifest") or [])

        for entry in manifest:
            rel = entry.get("path", "")
            efindings: list[dict] = []

            if entry.get("is_symlink"):
                efindings.append({"severity": "critical", "type": "symlink_blocked", "file": rel, "detail": "Symlink changes are blocked"})
            if not entry.get("contained_in_worktree", True):
                efindings.append({"severity": "critical", "type": "path_traversal", "file": rel, "detail": "File outside worktree bounds"})
            if _SENSITIVE_FILENAMES.search(rel) and entry.get("change_type") != "deleted":
                efindings.append({"severity": "critical", "type": "sensitive_filename", "file": rel, "detail": f"Sensitive file pattern {rel!r} cannot be added"})
            if entry.get("mode_changed") and str(entry.get("new_mode", "")).endswith("755"):
                all_warnings.append(f"{rel}: executable file mode 100755 set")

            # Scan untracked or newly created non-binary file contents directly so `git diff HEAD` never misses them
            if wt and not entry.get("is_binary") and not entry.get("is_symlink") and entry.get("change_type") in ("untracked", "added", "modified", "renamed"):
                fp = (wt / rel).resolve()
                if fp.exists() and fp.is_file() and wt in fp.parents:
                    txt = fp.read_text(encoding="utf-8", errors="replace")[:100_000]
                    f_list, w_list = _scan_text_block(txt, file_hint=rel)
                    efindings.extend(f_list)
                    all_warnings.extend(w_list)

            entry["findings"] = efindings
            has_crit = any(f["severity"] == "critical" for f in efindings)
            entry["contains_sensitive"] = has_crit
            entry["security_result"] = "critical" if has_crit else ("warning" if efindings else "clean")
            all_findings.extend(efindings)

        # Deduplicate warnings while preserving order
        seen_w: set[str] = set()
        dedup_warnings: list[str] = []
        for w in all_warnings:
            if w not in seen_w:
                seen_w.add(w)
                dedup_warnings.append(w)
        clean = not any(f["severity"] == "critical" for f in all_findings)
        return {
            "clean": clean,
            "findings": all_findings,
            "warnings": dedup_warnings,
            "manifest": manifest,
            "manifest_sha256": diff_info.get("manifest_sha256", ""),
        }

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
