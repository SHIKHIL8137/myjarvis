"""Isolated Git worktree lifecycle manager for safe coding agent execution."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

_SAFE_ALLOWLIST_BINS = {"pytest", "python", "ruff", "mypy", "node", "npm", "npx", "tsc", "git"}


class GitWorktreeError(RuntimeError):
    """Raised when a Git worktree operation or sandbox boundary check fails."""


def _run_git(cwd: Path, args: list[str], timeout_s: int = 30) -> subprocess.CompletedProcess:
    git_bin = shutil.which("git") or "git"
    return subprocess.run(
        [git_bin, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
    )


def git_head_sha(repo_root: Path) -> str:
    p = _run_git(Path(repo_root), ["rev-parse", "HEAD"])
    return p.stdout.strip() if p.returncode == 0 else ""


def git_current_branch(repo_root: Path) -> str:
    p = _run_git(Path(repo_root), ["rev-parse", "--abbrev-ref", "HEAD"])
    return p.stdout.strip() if p.returncode == 0 else ""


def create_worktree(repo_root: Path, task_id: str, worktree_root_name: str = ".jarvis_worktrees") -> dict:
    """Create an isolated branch + worktree under `<repo>/<worktree_root_name>/<task_id>`."""
    root = Path(repo_root).resolve()
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", task_id).strip("-")[:48] or "task"
    branch = f"jarvis/{slug}"
    wt_base = (root / worktree_root_name).resolve()
    wt_path = (wt_base / slug).resolve()
    if wt_base not in wt_path.parents:
        raise GitWorktreeError("Invalid worktree path traversal blocked")
    wt_base.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
        _run_git(root, ["worktree", "prune"])

    proc = _run_git(root, ["worktree", "add", "-B", branch, str(wt_path), "HEAD"])
    if proc.returncode != 0:
        raise GitWorktreeError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return {"repo_root": str(root), "worktree_path": str(wt_path), "branch": branch, "head_sha": git_head_sha(wt_path)}


def worktree_diff(worktree_path: Path) -> dict:
    """Inspect uncommitted & committed changes inside the isolated worktree."""
    wt = Path(worktree_path).resolve()
    st = _run_git(wt, ["status", "--short"])
    df = _run_git(wt, ["diff", "HEAD"])
    untracked = [ln[3:].strip() for ln in st.stdout.splitlines() if ln.startswith("?? ")]
    changed = [ln[3:].strip() for ln in st.stdout.splitlines() if ln.strip() and not ln.startswith("?? ")]
    return {
        "status_short": st.stdout.strip(),
        "diff": df.stdout[:40_000],
        "changed_files": changed,
        "untracked_files": untracked,
    }


def run_worktree_command(worktree_path: Path, argv: list[str], *, timeout_s: int = 120) -> dict:
    """Execute an allowlisted verification/lint command strictly inside the isolated worktree."""
    if not argv:
        raise GitWorktreeError("Empty command argv")
    exe_name = Path(argv[0]).stem.lower()
    if exe_name not in _SAFE_ALLOWLIST_BINS:
        raise GitWorktreeError(f"Executable {argv[0]!r} is not allowlisted for worktree QA execution")
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")

    # Prefer current venv Python / tools when available so QA executes inside project virtualenv cleanly
    venv_sibling = Path(sys.executable).with_name(f"{exe_name}.exe" if os.name == "nt" else exe_name)
    if exe_name in ("python", "python3"):
        resolved = sys.executable
    elif venv_sibling.exists():
        resolved = str(venv_sibling)
    else:
        resolved = shutil.which(f"{argv[0]}.exe") or shutil.which(f"{argv[0]}.cmd") or shutil.which(argv[0]) or argv[0]
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "VIRTUAL_ENV", "COMSPEC"}}
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [resolved, *argv[1:]],
        cwd=str(wt),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
    )
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout": proc.stdout[:12_000],
        "stderr": proc.stderr[:6_000],
    }


def commit_worktree_changes(worktree_path: Path, message: str) -> dict:
    wt = Path(worktree_path).resolve()
    _run_git(wt, ["add", "-A"])
    st = _run_git(wt, ["status", "--short"])
    if not st.stdout.strip():
        return {"committed": False, "sha": git_head_sha(wt)}
    p = _run_git(wt, ["commit", "-m", message[:120]])
    if p.returncode != 0:
        raise GitWorktreeError(f"git commit failed: {p.stderr.strip() or p.stdout.strip()}")
    return {"committed": True, "sha": git_head_sha(wt)}


def merge_worktree(repo_root: Path, branch_name: str, worktree_path: Path, commit_msg: str = "Jarvis verified coding task") -> dict:
    """Commit worktree changes cleanly and merge into target repository HEAD (call ONLY after human approval!)."""
    root = Path(repo_root).resolve()
    wt = Path(worktree_path).resolve()
    commit_worktree_changes(wt, commit_msg)
    m = _run_git(root, ["merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"])
    if m.returncode != 0:
        raise GitWorktreeError(f"git merge failed: {m.stderr.strip() or m.stdout.strip()}")
    return {"merged": True, "branch": branch_name, "head_sha": git_head_sha(root), "output": m.stdout.strip()}


def cleanup_worktree(repo_root: Path, worktree_path: Path, branch_name: str | None = None) -> dict:
    root = Path(repo_root).resolve()
    wt = Path(worktree_path).resolve()
    rm = _run_git(root, ["worktree", "remove", "--force", str(wt)])
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _run_git(root, ["worktree", "prune"])
    if branch_name:
        _run_git(root, ["branch", "-D", branch_name])
    return {"cleaned": True, "returncode": rm.returncode}
