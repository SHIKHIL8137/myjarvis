"""Isolated Git worktree lifecycle, execution policy sandbox, and full-change manifest builder."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

_DEFAULT_ALLOWED_BINS = {"pytest", "python", "python3", "ruff", "mypy", "node", "npm", "npx", "tsc", "git"}
_DEFAULT_DENIED_BINS = {
    "powershell",
    "pwsh",
    "cmd",
    "bash",
    "sh",
    "zsh",
    "curl",
    "wget",
    "rm",
    "del",
    "format",
    "reg",
    "diskpart",
}
_SAFE_ENV_KEYS = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "VIRTUAL_ENV", "COMSPEC", "HOME", "USERPROFILE"}
_QA_RELEVANT_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".json", ".toml", ".yml", ".yaml",
    ".ps1", ".sh", ".env", ".cfg", ".ini", ".dockerfile",
}


class GitWorktreeError(RuntimeError):
    """Raised when a Git worktree operation, manifest check, or sandbox policy fails."""


@dataclass
class ExecutionPolicy:
    """Configurable worktree execution policy with honest capability reporting."""

    allowed_workdirs: list[Path] = field(default_factory=list)
    allowed_executables: set[str] = field(default_factory=lambda: set(_DEFAULT_ALLOWED_BINS))
    denied_executables: set[str] = field(default_factory=lambda: set(_DEFAULT_DENIED_BINS))
    timeout_s: int = 120
    max_memory_mb: int = 1024
    cpu_seconds: int = 120
    env_allowlist: set[str] = field(default_factory=lambda: set(_SAFE_ENV_KEYS))
    network_mode: str = "none"  # "none" | "host"
    permissions: set[str] = field(default_factory=lambda: {"read", "write", "execute"})
    sandbox_backend: str = "restricted_subprocess"  # "restricted_subprocess" | "docker"
    docker_image: str = "python:3.12-slim"

    def capabilities(self) -> dict:
        docker_exe = shutil.which("docker")
        using_docker = self.sandbox_backend == "docker" and bool(docker_exe)
        if using_docker:
            return {
                "backend": "docker",
                "directory_scoping": True,
                "env_filtering": True,
                "executable_allowlist": True,
                "os_filesystem_isolation": True,
                "os_network_isolation": self.network_mode == "none",
                "memory_limit_mb": self.max_memory_mb,
                "cpu_seconds_limit": self.cpu_seconds,
                "limitations": [],
            }
        limitations = [
            "Directory scoping, executable allowlisting, and environment filtering active; "
            + (
                "OS-level network/filesystem namespace isolation requires Docker backend on Windows."
                if os.name == "nt"
                else "POSIX RLIMIT CPU/memory enforced; full network jail requires Docker backend."
            )
        ]
        return {
            "backend": "restricted_subprocess",
            "requested_backend": self.sandbox_backend,
            "directory_scoping": True,
            "env_filtering": True,
            "executable_allowlist": True,
            "os_filesystem_isolation": False,
            "os_network_isolation": False,
            "memory_limit_mb": self.max_memory_mb if os.name != "nt" else None,
            "cpu_seconds_limit": self.cpu_seconds if os.name != "nt" else None,
            "limitations": limitations,
        }

    def validate_access(self, target_dir: Path, required_perm: str, exe_name: str | None = None) -> Path:
        if required_perm not in self.permissions:
            raise GitWorktreeError(f"Execution policy denies '{required_perm}' permission")
        resolved = Path(target_dir).resolve()
        if self.allowed_workdirs:
            allowed = [Path(d).resolve() for d in self.allowed_workdirs]
            if not any(resolved == d or d in resolved.parents for d in allowed):
                raise GitWorktreeError(f"Path outside allowed policy workdirs: {resolved}")
        if exe_name is not None:
            stem = Path(exe_name).stem.lower()
            if stem in {d.lower() for d in self.denied_executables}:
                raise GitWorktreeError(f"Executable {exe_name!r} is explicitly denied by execution policy")
            if self.allowed_executables and stem not in {a.lower() for a in self.allowed_executables}:
                raise GitWorktreeError(f"Executable {exe_name!r} is not allowlisted by execution policy")
        return resolved


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


def _validate_repo_root(repo_root: Path) -> Path:
    root = Path(repo_root).resolve()
    if not root.exists() or not root.is_dir():
        raise GitWorktreeError(f"Repository root does not exist: {root}")
    top = _run_git(root, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        raise GitWorktreeError(f"Directory is not a valid Git repository root: {root}")
    return root


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _owner_file(wt_base: Path, slug: str) -> Path:
    return (wt_base / ".owners" / f"{slug}.json").resolve()


def _write_owner(wt_base: Path, slug: str, payload: dict) -> None:
    of = _owner_file(wt_base, slug)
    of.parent.mkdir(parents=True, exist_ok=True)
    tmp = of.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, of)


def _read_owner(wt_base: Path, slug: str) -> dict | None:
    of = _owner_file(wt_base, slug)
    if not of.exists():
        return None
    try:
        return json.loads(of.read_text(encoding="utf-8"))
    except Exception:
        return None


def git_head_sha(repo_root: Path) -> str:
    p = _run_git(Path(repo_root), ["rev-parse", "HEAD"])
    return p.stdout.strip() if p.returncode == 0 else ""


def git_current_branch(repo_root: Path) -> str:
    p = _run_git(Path(repo_root), ["rev-parse", "--abbrev-ref", "HEAD"])
    return p.stdout.strip() if p.returncode == 0 else ""


def _branch_exists(repo_root: Path, branch: str) -> bool:
    p = _run_git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return p.returncode == 0


def create_worktree(
    repo_root: Path,
    task_id: str,
    worktree_root_name: str = ".jarvis_worktrees",
    *,
    ensure_unique: bool = False,
) -> dict:
    """Create a strictly isolated task-specific branch + worktree without overwriting existing worktrees."""
    root = _validate_repo_root(repo_root)
    if ".." in Path(worktree_root_name).parts or Path(worktree_root_name).is_absolute():
        raise GitWorktreeError("Invalid worktree root directory name")
    raw_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (task_id or "").strip()).strip("-")[:40] or "task"
    wt_base = (root / worktree_root_name).resolve()
    if root not in wt_base.parents:
        raise GitWorktreeError("Invalid worktree base traversal blocked")

    slug = f"{raw_slug}-{uuid.uuid4().hex[:6]}" if ensure_unique else raw_slug
    branch = f"jarvis/{slug}"
    wt_path = (wt_base / slug).resolve()
    if wt_base not in wt_path.parents or wt_path == wt_base:
        raise GitWorktreeError("Invalid worktree path traversal blocked")

    if wt_path.exists():
        raise GitWorktreeError(f"Worktree for task '{slug}' already exists; refusing accidental deletion/overwrite")
    if _branch_exists(root, branch):
        raise GitWorktreeError(f"Worktree branch '{branch}' already exists; refusing overwrite")

    wt_base.mkdir(parents=True, exist_ok=True)
    proc = _run_git(root, ["worktree", "add", "-b", branch, str(wt_path), "HEAD"])
    if proc.returncode != 0:
        raise GitWorktreeError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")

    info = {
        "repo_root": str(root),
        "worktree_path": str(wt_path),
        "branch": branch,
        "slug": slug,
        "task_id": task_id,
        "head_sha": git_head_sha(wt_path),
        "created_at": round(time.time(), 3),
    }
    _write_owner(wt_base, slug, info)
    return info


def verify_worktree_ownership(repo_root: Path, worktree_path: Path, task_id: str) -> dict:
    """Verify worktree belongs to validated repo_root & expected task owner."""
    root = _validate_repo_root(repo_root)
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")
    top = _run_git(wt, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != wt:
        raise GitWorktreeError(f"Path is not an active Git worktree: {wt}")
    owner = _read_owner(wt.parent, wt.name)
    if owner and owner.get("task_id") and owner["task_id"] != task_id:
        raise GitWorktreeError(f"Worktree {wt.name!r} is owned by another task ({owner['task_id']!r})")
    if owner and Path(owner.get("repo_root", "")).resolve() != root:
        raise GitWorktreeError(f"Worktree {wt.name!r} does not belong to project repo {root}")
    return owner or {"repo_root": str(root), "worktree_path": str(wt), "task_id": task_id}


def _parse_porcelain_z(raw_z: str) -> list[dict]:
    items: list[dict] = []
    tokens = [t for t in raw_z.split("\0") if t]
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        if len(entry) < 4:
            i += 1
            continue
        xy = entry[:2]
        path = entry[3:]
        old_path = None
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i + 1 < len(tokens):
                old_path = tokens[i + 1]
                i += 1
        items.append({"xy": xy, "path": path, "old_path": old_path})
        i += 1
    return items


def _parse_diff_raw(raw_z: str) -> dict[str, dict]:
    """Map path -> {old_mode, new_mode, status} from `git diff HEAD --raw -z`."""
    modes: dict[str, dict] = {}
    parts = [p for p in raw_z.split("\0") if p != ""]
    i = 0
    while i < len(parts):
        hdr = parts[i]
        if hdr.startswith(":") and i + 1 < len(parts):
            toks = hdr[1:].split()
            old_m = toks[0] if len(toks) > 0 else ""
            new_m = toks[1] if len(toks) > 1 else ""
            st = toks[4] if len(toks) > 4 else ""
            if st.startswith(("R", "C")) and i + 2 < len(parts):
                p = parts[i + 2]
                i += 3
            else:
                p = parts[i + 1]
                i += 2
            modes[p] = {"old_mode": old_m, "new_mode": new_m, "git_status": st}
        else:
            i += 1
    return modes


def _parse_numstat_z(raw_z: str) -> dict[str, bool]:
    """Detect Git-flagged binary files via `git diff HEAD --numstat -z`."""
    bins: dict[str, bool] = {}
    for rec in raw_z.split("\0"):
        if "\t" not in rec:
            continue
        cols = rec.split("\t", 2)
        if len(cols) == 3:
            added, removed, fpath = cols
            bins[fpath] = (added == "-" and removed == "-")
    return bins


def build_change_manifest(worktree_path: Path) -> dict:
    """Build complete change manifest covering staged, unstaged, untracked, renamed, deleted, binary, mode, & symlink files."""
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")

    st_z = _run_git(wt, ["status", "--porcelain=v1", "-z", "-uall"])
    raw_z = _run_git(wt, ["diff", "HEAD", "--raw", "-z"])
    num_z = _run_git(wt, ["diff", "HEAD", "--numstat", "-z"])
    df = _run_git(wt, ["diff", "HEAD"])
    df_cached = _run_git(wt, ["diff", "--cached"])

    porcelain = _parse_porcelain_z(st_z.stdout)
    mode_map = _parse_diff_raw(raw_z.stdout)
    bin_map = _parse_numstat_z(num_z.stdout)

    entries: list[dict] = []
    untracked_corpus_parts: list[str] = []
    changed_files: list[str] = []
    untracked_files: list[str] = []

    for item in porcelain:
        xy = item["xy"]
        rel = item["path"]
        old_rel = item["old_path"]
        mode_info = mode_map.get(rel, {})
        abs_p = (wt / rel).resolve()
        contained = abs_p == wt or wt in abs_p.parents

        if xy == "??":
            change_type = "untracked"
            untracked_files.append(rel)
        elif "D" in xy:
            change_type = "deleted"
            changed_files.append(rel)
        elif "R" in xy:
            change_type = "renamed"
            changed_files.append(rel)
        elif "A" in xy:
            change_type = "added"
            changed_files.append(rel)
        elif "T" in xy:
            change_type = "typechange"
            changed_files.append(rel)
        else:
            change_type = "modified"
            changed_files.append(rel)

        staged = xy[0] not in (" ", "?")
        unstaged = xy[1] not in (" ", "?")
        is_symlink = abs_p.is_symlink() or mode_info.get("new_mode") == "120000"
        is_binary = bool(bin_map.get(rel, False))
        content_hash = "deleted"
        mode_changed = bool(
            mode_info.get("old_mode")
            and mode_info.get("new_mode")
            and mode_info["old_mode"] != "000000"
            and mode_info["old_mode"] != mode_info["new_mode"]
        )

        if change_type != "deleted" and contained and abs_p.exists() and not is_symlink and abs_p.is_file():
            content_hash = _sha256_file(abs_p)
            prefix = abs_p.read_bytes()[:4096]
            if b"\x00" in prefix:
                is_binary = True
            elif change_type in ("untracked", "added"):
                # Explicitly include untracked & newly staged files so scanning never skips them
                txt = abs_p.read_text(encoding="utf-8", errors="replace")[:65_536]
                untracked_corpus_parts.append(f"--- NEW_FILE: {rel}\n{txt}")
        elif is_symlink:
            try:
                content_hash = hashlib.sha256(str(os.readlink(abs_p)).encode("utf-8", errors="replace")).hexdigest()
            except OSError:
                content_hash = "symlink"

        ext = Path(rel).suffix.lower()
        entries.append(
            {
                "path": rel,
                "old_path": old_rel,
                "change_type": change_type,
                "porcelain_xy": xy,
                "staged": staged,
                "unstaged": unstaged,
                "old_mode": mode_info.get("old_mode", ""),
                "new_mode": mode_info.get("new_mode", ""),
                "mode_changed": mode_changed,
                "is_binary": is_binary,
                "is_symlink": is_symlink,
                "contained_in_worktree": contained,
                "content_hash": content_hash,
                "qa_relevant": (ext in _QA_RELEVANT_EXTS) or ("test" in rel.lower()),
                "security_result": "pending",
                "contains_sensitive": False,
                "findings": [],
            }
        )

    entries.sort(key=lambda e: e["path"])
    digest_basis = json.dumps(
        [(e["path"], e["change_type"], e["new_mode"], e["content_hash"], e["is_symlink"]) for e in entries],
        sort_keys=True,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(digest_basis).hexdigest()

    st_short = _run_git(wt, ["status", "--short"])
    return {
        "status_short": st_short.stdout.strip(),
        "diff": df.stdout[:40_000],
        "cached_diff": df_cached.stdout[:20_000],
        "changed_files": changed_files,
        "untracked_files": untracked_files,
        "manifest": entries,
        "manifest_sha256": manifest_sha256,
        "untracked_corpus": "\n".join(untracked_corpus_parts),
    }


def worktree_diff(worktree_path: Path) -> dict:
    """Inspect all tracked + untracked + mode + binary changes inside the isolated worktree."""
    return build_change_manifest(worktree_path)


def _posix_limits_fn(policy: ExecutionPolicy):
    if os.name == "nt":
        return None
    import resource

    def _apply():
        mem_bytes = int(policy.max_memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    return _apply


def run_worktree_command(
    worktree_path: Path,
    argv: list[str],
    *,
    timeout_s: int | None = None,
    policy: ExecutionPolicy | None = None,
) -> dict:
    """Execute an allowlisted verification/lint command strictly inside the worktree under ExecutionPolicy."""
    if not argv:
        raise GitWorktreeError("Empty command argv")
    pol = policy or ExecutionPolicy(allowed_workdirs=[Path(worktree_path).resolve()])
    wt = pol.validate_access(Path(worktree_path), "execute", exe_name=argv[0])
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")

    exe_name = Path(argv[0]).stem.lower()
    eff_timeout = int(timeout_s or pol.timeout_s)
    caps = pol.capabilities()

    if caps["backend"] == "docker":
        docker_exe = shutil.which("docker") or "docker"
        cmd = [
            docker_exe,
            "run",
            "--rm",
            f"--network={pol.network_mode}",
            "--memory",
            f"{pol.max_memory_mb}m",
            "-v",
            f"{wt}:/workspace",
            "-w",
            "/workspace",
            pol.docker_image,
            *argv,
        ]
        env = {k: v for k, v in os.environ.items() if k.upper() in pol.env_allowlist}
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=eff_timeout,
            shell=False,
        )
    else:
        venv_sibling = Path(sys.executable).with_name(f"{exe_name}.exe" if os.name == "nt" else exe_name)
        if exe_name in ("python", "python3"):
            resolved = sys.executable
        elif venv_sibling.exists():
            resolved = str(venv_sibling)
        else:
            resolved = shutil.which(f"{argv[0]}.exe") or shutil.which(f"{argv[0]}.cmd") or shutil.which(argv[0]) or argv[0]
        env = {k: v for k, v in os.environ.items() if k.upper() in pol.env_allowlist}
        env["PYTHONUTF8"] = "1"
        preexec = _posix_limits_fn(pol)
        proc = subprocess.run(
            [resolved, *argv[1:]],
            cwd=str(wt),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=eff_timeout,
            preexec_fn=preexec,
            shell=False,
        )

    return {
        "argv": argv,
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout": proc.stdout[:12_000],
        "stderr": proc.stderr[:6_000],
        "isolation": caps,
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
    """Commit worktree changes cleanly and merge into target repository HEAD (call ONLY after human approval + passed QA!)."""
    root = _validate_repo_root(repo_root)
    wt = Path(worktree_path).resolve()
    commit_res = commit_worktree_changes(wt, commit_msg)
    m = _run_git(root, ["merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"])
    if m.returncode != 0:
        raise GitWorktreeError(f"git merge failed: {m.stderr.strip() or m.stdout.strip()}")
    return {
        "merged": True,
        "branch": branch_name,
        "worktree_commit_sha": commit_res.get("sha", ""),
        "head_sha": git_head_sha(root),
        "output": m.stdout.strip(),
    }


def cleanup_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str | None = None,
    *,
    authorized_uncommitted: bool = False,
    expected_task_id: str | None = None,
    worktree_root_name: str = ".jarvis_worktrees",
) -> dict:
    """Safely remove a task worktree after verifying ownership, containment, and uncommitted-change authorization."""
    root = _validate_repo_root(repo_root)
    wt = Path(worktree_path).resolve()
    wt_base = (root / worktree_root_name).resolve()
    if wt_base not in wt.parents or wt == wt_base:
        raise GitWorktreeError(f"Refusing cleanup outside isolated worktree root: {wt}")

    if expected_task_id:
        verify_worktree_ownership(root, wt, expected_task_id)

    if wt.exists():
        dirty = _run_git(wt, ["status", "--porcelain"])
        if dirty.stdout.strip() and not authorized_uncommitted:
            raise GitWorktreeError(
                f"Worktree '{wt.name}' has uncommitted changes; explicit cleanup authorization is required."
            )

    rm_args = ["worktree", "remove", "--force", str(wt)] if authorized_uncommitted else ["worktree", "remove", str(wt)]
    rm = _run_git(root, rm_args)
    if wt.exists():
        if not authorized_uncommitted:
            raise GitWorktreeError(f"Failed to cleanly remove worktree '{wt.name}': {rm.stderr.strip()}")
        shutil.rmtree(wt, ignore_errors=True)
    _run_git(root, ["worktree", "prune"])
    if branch_name:
        _run_git(root, ["branch", "-D" if authorized_uncommitted else "-d", branch_name])

    of = _owner_file(wt_base, wt.name)
    if of.exists():
        of.unlink(missing_ok=True)
    return {"cleaned": True, "returncode": rm.returncode}


def recover_orphaned_worktrees(repo_root: Path, worktree_root_name: str = ".jarvis_worktrees") -> list[dict]:
    """Prune stale Git worktree refs while inspecting remaining active/orphaned task directories cleanly."""
    root = _validate_repo_root(repo_root)
    _run_git(root, ["worktree", "prune"])
    wt_base = (root / worktree_root_name).resolve()
    if not wt_base.exists():
        return []
    found: list[dict] = []
    for entry in sorted(wt_base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        owner = _read_owner(wt_base, entry.name) or {}
        dirty = _run_git(entry, ["status", "--porcelain"]).stdout.strip() if (entry / ".git").exists() else ""
        found.append(
            {
                "slug": entry.name,
                "path": str(entry),
                "task_id": owner.get("task_id"),
                "branch": owner.get("branch"),
                "has_uncommitted": bool(dirty),
            }
        )
    return found
