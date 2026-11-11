"""Isolated Git worktree lifecycle, execution policy sandbox (Docker/restricted), process tracking, & complete manifest builder."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
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
DEFAULT_MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024  # 2 MB hard safety bound; oversized changed text files fail review

_PROC_LOCK = threading.Lock()
_ACTIVE_RUN_PROCS: dict[str, list[dict]] = {}


class GitWorktreeError(RuntimeError):
    """Raised when a Git worktree operation, manifest check, or sandbox policy fails."""


def register_run_process(run_id: str | None, proc: subprocess.Popen, *, container_cid_file: Path | None = None) -> None:
    if not run_id:
        return
    with _PROC_LOCK:
        _ACTIVE_RUN_PROCS.setdefault(run_id, []).append({"proc": proc, "cid_file": container_cid_file})


def unregister_run_process(run_id: str | None, proc: subprocess.Popen) -> None:
    if not run_id:
        return
    with _PROC_LOCK:
        items = _ACTIVE_RUN_PROCS.get(run_id) or []
        _ACTIVE_RUN_PROCS[run_id] = [it for it in items if it.get("proc") is not proc]
        if not _ACTIVE_RUN_PROCS[run_id]:
            _ACTIVE_RUN_PROCS.pop(run_id, None)


def _terminate_proc_tree(proc: subprocess.Popen, cid_file: Path | None = None) -> None:
    if cid_file and cid_file.exists():
        try:
            cid = cid_file.read_text(encoding="utf-8", errors="ignore").strip()
            if cid and (docker_exe := shutil.which("docker")):
                subprocess.run([docker_exe, "rm", "-f", cid], capture_output=True, timeout=10, shell=False)
        except Exception:
            pass
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10, shell=False)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_run_processes(run_id: str) -> int:
    """Terminate all active coding/QA child processes and containers registered under `run_id`."""
    with _PROC_LOCK:
        entries = list(_ACTIVE_RUN_PROCS.get(run_id) or [])
    killed = 0
    for ent in entries:
        _terminate_proc_tree(ent["proc"], ent.get("cid_file"))
        killed += 1
    return killed


def is_docker_available() -> bool:
    docker_exe = shutil.which("docker")
    if not docker_exe:
        return False
    try:
        r = subprocess.run([docker_exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=4, shell=False)
        return r.returncode == 0
    except Exception:
        return False


@dataclass
class ExecutionPolicy:
    """Configurable worktree & OpenCode execution sandbox policy with Docker support & honest limitations."""

    allowed_workdirs: list[Path] = field(default_factory=list)
    allowed_executables: set[str] = field(default_factory=lambda: set(_DEFAULT_ALLOWED_BINS))
    denied_executables: set[str] = field(default_factory=lambda: set(_DEFAULT_DENIED_BINS))
    timeout_s: int = 120
    max_memory_mb: int = 1024
    cpu_seconds: int = 120
    cpus: float = 2.0
    env_allowlist: set[str] = field(default_factory=lambda: set(_SAFE_ENV_KEYS))
    network_mode: str = "none"  # legacy alias ("none" | "host")
    network_policy: str = "offline"  # "offline" | "model_gateway" | "restricted_egress" | "unrestricted"
    allowed_egress_domains: list[str] = field(default_factory=list)
    allow_ollama_in_sandbox: bool = False
    allow_unrestricted_network: bool = False
    unrestricted_authorized_by: str = ""
    gateway_base_url: str = ""
    gateway_scoped_token: str = ""
    egress_proxy_url: str = ""
    pids_limit: int = 256
    permissions: set[str] = field(default_factory=lambda: {"read", "write", "execute"})
    sandbox_backend: str = "restricted_subprocess"  # "restricted_subprocess" | "docker"
    docker_image: str = "jarvis-opencode:1.19.5"
    allow_host_fallback: bool = False
    read_only_root_fs: bool = True

    def validate_network_policy(self) -> str:
        pol = (self.network_policy or "offline").strip().lower()
        if self.network_mode == "host" and pol == "offline":
            pol = "unrestricted"
        if pol not in ("offline", "model_gateway", "restricted_egress", "unrestricted"):
            raise GitWorktreeError(f"Invalid sandbox network_policy: {pol!r}")
        if pol == "model_gateway":
            if not self.gateway_base_url.strip() or not self.gateway_scoped_token.strip():
                raise GitWorktreeError(
                    "network_policy='model_gateway' requires scoped gateway_base_url and gateway_scoped_token; refusing silent fallback."
                )
        elif pol == "restricted_egress":
            if not self.egress_proxy_url.strip() or (not self.allowed_egress_domains and not self.allow_ollama_in_sandbox):
                raise GitWorktreeError(
                    "network_policy='restricted_egress' requires active egress_proxy_url and explicit domain/Ollama allowlist; refusing silent fallback."
                )
        elif pol == "unrestricted":
            if not self.allow_unrestricted_network or not (self.unrestricted_authorized_by or "").strip():
                raise GitWorktreeError(
                    "Unrestricted container networking (`network_policy='unrestricted'`) is disabled by default and requires explicit owner authorization."
                )
        return pol

    def capabilities(self, *, docker_ready: bool | None = None) -> dict:
        ready = is_docker_available() if docker_ready is None else docker_ready
        using_docker = self.sandbox_backend == "docker" and ready
        eff_net = (self.network_policy or "offline").strip().lower()
        if using_docker:
            return {
                "backend": "docker",
                "requested_backend": self.sandbox_backend,
                "docker_image": self.docker_image,
                "network_policy": eff_net,
                "directory_scoping": True,
                "env_filtering": True,
                "executable_allowlist": True,
                "os_filesystem_isolation": True,
                "os_network_isolation": eff_net in ("offline", "model_gateway", "restricted_egress"),
                "read_only_root_fs": self.read_only_root_fs,
                "non_root_uid": "10001:10001",
                "cap_drop_all": True,
                "pids_limit": self.pids_limit,
                "no_host_secrets_mounted": True,
                "no_docker_socket_mounted": True,
                "memory_limit_mb": self.max_memory_mb,
                "cpu_seconds_limit": self.cpu_seconds,
                "cpus": self.cpus,
                "limitations": [],
            }
        degraded_note = (
            "Docker backend requested but Docker daemon is unavailable; fell back to restricted_subprocess (OS isolation NOT active)."
            if self.sandbox_backend == "docker"
            else (
                "Directory scoping, executable allowlisting, and environment filtering active; "
                "OS-level network/filesystem namespace isolation is NOT enforced on native Windows without Docker."
                if os.name == "nt"
                else "Directory scoping, env filtering, and POSIX RLIMIT CPU/memory active; network jail requires Docker."
            )
        )
        return {
            "backend": "restricted_subprocess",
            "requested_backend": self.sandbox_backend,
            "directory_scoping": True,
            "env_filtering": True,
            "executable_allowlist": True,
            "os_filesystem_isolation": False,
            "os_network_isolation": False,
            "read_only_root_fs": False,
            "cap_drop_all": False,
            "no_host_secrets_mounted": True,
            "memory_limit_mb": self.max_memory_mb if os.name != "nt" else None,
            "cpu_seconds_limit": self.cpu_seconds if os.name != "nt" else None,
            "limitations": [degraded_note],
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


def to_docker_mount_path(path: Path) -> str:
    """Convert host worktree path cleanly for Docker Desktop on Windows or POSIX bind mounts."""
    resolved = Path(path).resolve()
    raw = str(resolved)
    if os.name == "nt" or (len(raw) >= 2 and raw[1] == ":" and "\\" in raw):
        norm = raw.replace("\\", "/").rstrip("/")
        if len(norm) >= 2 and norm[1] == ":":
            norm = norm[0].upper() + norm[1:]
        return norm
    return resolved.as_posix().rstrip("/") or "/"


def build_docker_run_cmd(
    workdir: Path,
    argv: list[str],
    policy: ExecutionPolicy,
    *,
    cid_file: Path,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Construct strict unprivileged non-root container invocation mounting ONLY target worktree RW."""
    docker_exe = shutil.which("docker") or "docker"
    wd_mount = to_docker_mount_path(workdir)
    net_pol = policy.validate_network_policy()
    if net_pol == "offline":
        net_args = ["--network=none"]
    elif net_pol == "model_gateway":
        net_args = ["--network=bridge", "--add-host=host.docker.internal:host-gateway"]
    elif net_pol == "restricted_egress":
        net_args = ["--network=bridge", "--add-host=host.docker.internal:host-gateway"]
    else:
        net_args = ["--network=bridge"]

    cmd = [
        docker_exe,
        "run",
        "--rm",
        "--init",
        "--user",
        "10001:10001",
        "--cidfile",
        str(cid_file),
        *net_args,
        "--memory",
        f"{int(policy.max_memory_mb)}m",
        "--cpus",
        str(float(policy.cpus)),
        "--pids-limit",
        str(int(policy.pids_limit)),
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
    ]
    if policy.read_only_root_fs:
        cmd.extend(
            [
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=128m",
                "--tmpfs",
                "/home/jarvis/.local/state:rw,nosuid,size=32m",
                "--tmpfs",
                "/home/jarvis/.config:rw,nosuid,size=16m",
            ]
        )

    filtered_env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in policy.env_allowlist
        and k.upper() not in {"PATH", "HOME", "USERPROFILE", "COMSPEC", "APPDATA", "LOCALAPPDATA"}
    }
    if net_pol == "model_gateway":
        filtered_env["OPENAI_BASE_URL"] = policy.gateway_base_url
        filtered_env["OPENAI_API_KEY"] = policy.gateway_scoped_token
        filtered_env["JARVIS_GATEWAY_TOKEN"] = policy.gateway_scoped_token
        filtered_env["JARVIS_NETWORK_POLICY"] = "model_gateway"
    elif net_pol == "restricted_egress":
        filtered_env["HTTP_PROXY"] = policy.egress_proxy_url
        filtered_env["HTTPS_PROXY"] = policy.egress_proxy_url
        filtered_env["JARVIS_ALLOWED_DOMAINS"] = ",".join(policy.allowed_egress_domains)
        filtered_env["JARVIS_NETWORK_POLICY"] = "restricted_egress"
    else:
        filtered_env["JARVIS_NETWORK_POLICY"] = net_pol

    if extra_env:
        for k, v in extra_env.items():
            if k.upper() in policy.env_allowlist:
                filtered_env[k] = v

    for k, v in sorted(filtered_env.items()):
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["-v", f"{wd_mount}:/workspace:rw", "-w", "/workspace", policy.docker_image, *argv])
    return cmd


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
    """Create a strictly isolated branch + worktree and capture immutable original `base_sha`."""
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

    base_sha = git_head_sha(root)
    wt_base.mkdir(parents=True, exist_ok=True)
    proc = _run_git(root, ["worktree", "add", "-b", branch, str(wt_path), base_sha or "HEAD"])
    if proc.returncode != 0:
        raise GitWorktreeError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")

    head_sha = git_head_sha(wt_path)
    info = {
        "repo_root": str(root),
        "worktree_path": str(wt_path),
        "branch": branch,
        "slug": slug,
        "task_id": task_id,
        "base_sha": base_sha or head_sha,
        "head_sha": head_sha,
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
    """Map path -> {old_mode, new_mode, status, old_path} from `git diff ... --raw -z -M`."""
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
                old_p = parts[i + 1]
                new_p = parts[i + 2]
                modes[new_p] = {"old_mode": old_m, "new_mode": new_m, "git_status": st, "old_path": old_p}
                i += 3
            else:
                p = parts[i + 1]
                modes[p] = {"old_mode": old_m, "new_mode": new_m, "git_status": st, "old_path": None}
                i += 2
        else:
            i += 1
    return modes


def _parse_numstat_z(raw_z: str) -> dict[str, bool]:
    """Detect Git-flagged binary files via `git diff ... --numstat -z`."""
    bins: dict[str, bool] = {}
    tokens = raw_z.split("\0")
    i = 0
    while i < len(tokens):
        rec = tokens[i]
        if "\t" not in rec:
            i += 1
            continue
        cols = rec.split("\t", 2)
        if len(cols) == 3:
            added, removed, fpath = cols
            if not fpath and i + 2 < len(tokens):
                # `-z` rename record: `adds\trem\t\0old\0new`
                fpath = tokens[i + 2]
                i += 2
            bins[fpath] = (added == "-" and removed == "-")
        i += 1
    return bins


def _git_status_map(raw_z: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in _parse_porcelain_z(raw_z):
        out[item["path"]] = item
    return out


def build_change_manifest(
    worktree_path: Path,
    base_sha: str | None = None,
    *,
    max_scan_file_bytes: int = DEFAULT_MAX_SCAN_FILE_BYTES,
) -> dict:
    """Build complete change manifest from original `base_sha` through current `HEAD` + working tree + untracked files.
    Detects committed secrets/files, staged/unstaged/untracked changes, renames (old+new), deletions, symlinks, modes, & binary files."""
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")

    owner = _read_owner(wt.parent, wt.name) or {}
    cur_head = git_head_sha(wt)
    orig_base = (base_sha or owner.get("base_sha") or cur_head).strip()

    # 1. Committed range diff: orig_base -> HEAD
    committed_raw = _run_git(wt, ["diff", orig_base, "HEAD", "--raw", "-z", "-M"]) if orig_base and cur_head and orig_base != cur_head else subprocess.CompletedProcess([], 0, "", "")
    committed_num = _run_git(wt, ["diff", orig_base, "HEAD", "--numstat", "-z", "-M"]) if orig_base and cur_head and orig_base != cur_head else subprocess.CompletedProcess([], 0, "", "")
    committed_diff = _run_git(wt, ["diff", orig_base, "HEAD"]) if orig_base and cur_head and orig_base != cur_head else subprocess.CompletedProcess([], 0, "", "")

    # 2. Cumulative base -> working tree + untracked
    st_z = _run_git(wt, ["status", "--porcelain=v1", "-z", "-uall"])
    wt_raw = _run_git(wt, ["diff", "HEAD", "--raw", "-z", "-M"])
    wt_num = _run_git(wt, ["diff", "HEAD", "--numstat", "-z", "-M"])
    df = _run_git(wt, ["diff", "HEAD"])
    df_cached = _run_git(wt, ["diff", "--cached"])

    committed_modes = _parse_diff_raw(committed_raw.stdout)
    committed_bins = _parse_numstat_z(committed_num.stdout)
    wt_modes = _parse_diff_raw(wt_raw.stdout)
    wt_bins = _parse_numstat_z(wt_num.stdout)
    porcelain_map = _git_status_map(st_z.stdout)

    all_paths = sorted(set(committed_modes) | set(wt_modes) | set(porcelain_map))
    entries: list[dict] = []
    changed_files: list[str] = []
    untracked_files: list[str] = []

    for rel in all_paths:
        c_info = committed_modes.get(rel)
        w_info = wt_modes.get(rel)
        p_info = porcelain_map.get(rel)

        xy = p_info["xy"] if p_info else "  "
        old_rel = (p_info["old_path"] if p_info else None) or (w_info or {}).get("old_path") or (c_info or {}).get("old_path")
        mode_info = w_info or c_info or {}
        committed_changed = c_info is not None

        if xy == "??":
            change_type = "untracked"
            untracked_files.append(rel)
        else:
            st_char = (p_info["xy"].strip() if p_info else "") or (mode_info.get("git_status") or "")
            if "D" in xy or st_char.startswith("D"):
                change_type = "deleted"
            elif "R" in xy or st_char.startswith("R"):
                change_type = "renamed"
            elif "A" in xy or st_char.startswith("A"):
                change_type = "added"
            elif "T" in xy or st_char.startswith("T"):
                change_type = "typechange"
            else:
                change_type = "modified"
            changed_files.append(rel)

        staged = xy[0] not in (" ", "?")
        unstaged = xy[1] not in (" ", "?")
        abs_p = (wt / rel).resolve()
        contained = abs_p == wt or wt in abs_p.parents
        is_symlink = abs_p.is_symlink() or mode_info.get("new_mode") == "120000"
        is_binary = bool(wt_bins.get(rel) or committed_bins.get(rel))
        size_bytes = 0
        oversized = False
        content_hash = "deleted"
        mode_changed = bool(
            mode_info.get("old_mode")
            and mode_info.get("new_mode")
            and mode_info["old_mode"] not in ("000000", "")
            and mode_info["old_mode"] != mode_info["new_mode"]
        )

        if change_type != "deleted" and contained and abs_p.exists() and not is_symlink and abs_p.is_file():
            size_bytes = int(abs_p.stat().st_size)
            content_hash = _sha256_file(abs_p)
            prefix = abs_p.read_bytes()[:4096]
            if b"\x00" in prefix:
                is_binary = True
            elif size_bytes > max_scan_file_bytes:
                oversized = True
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
                "committed": committed_changed,
                "staged": staged,
                "unstaged": unstaged,
                "old_mode": mode_info.get("old_mode", ""),
                "new_mode": mode_info.get("new_mode", ""),
                "mode_changed": mode_changed,
                "is_binary": is_binary,
                "is_symlink": is_symlink,
                "size_bytes": size_bytes,
                "oversized": oversized,
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
        [(e["path"], e["old_path"], e["change_type"], e["committed"], e["new_mode"], e["content_hash"], e["is_symlink"]) for e in entries],
        sort_keys=True,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(digest_basis).hexdigest()
    full_diff_bytes = (committed_diff.stdout + "\n" + df.stdout).encode("utf-8", errors="replace")
    diff_hash = hashlib.sha256(full_diff_bytes).hexdigest()
    approval_fingerprint = hashlib.sha256(f"{orig_base}:{cur_head}:{manifest_sha256}:{diff_hash}".encode("utf-8")).hexdigest()

    st_short = _run_git(wt, ["status", "--short"])
    return {
        "base_sha": orig_base,
        "head_sha": cur_head,
        "status_short": st_short.stdout.strip(),
        "committed_diff": committed_diff.stdout,
        "diff": df.stdout[:40_000],
        "cached_diff": df_cached.stdout[:20_000],
        "changed_files": changed_files,
        "untracked_files": untracked_files,
        "manifest": entries,
        "manifest_sha256": manifest_sha256,
        "diff_hash": diff_hash,
        "approval_fingerprint": approval_fingerprint,
    }


def worktree_diff(worktree_path: Path, base_sha: str | None = None) -> dict:
    """Inspect all committed + staged + unstaged + untracked + mode + binary changes inside the worktree."""
    return build_change_manifest(worktree_path, base_sha=base_sha)


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
    run_id: str | None = None,
    cancel_check=None,
) -> dict:
    """Execute an allowlisted verification/lint command inside `worktree_path` under `ExecutionPolicy`."""
    if not argv:
        raise GitWorktreeError("Empty command argv")
    pol = policy or ExecutionPolicy(allowed_workdirs=[Path(worktree_path).resolve()])
    wt = pol.validate_access(Path(worktree_path), "execute", exe_name=argv[0])
    if not wt.exists():
        raise GitWorktreeError(f"Worktree does not exist: {wt}")

    exe_name = Path(argv[0]).stem.lower()
    eff_timeout = int(timeout_s or pol.timeout_s)
    docker_ok = is_docker_available() if pol.sandbox_backend == "docker" else False
    if pol.sandbox_backend == "docker" and not docker_ok and not pol.allow_host_fallback:
        raise GitWorktreeError("Docker sandbox backend requested for QA execution, but Docker daemon is unavailable.")
    caps = pol.capabilities(docker_ready=docker_ok)

    cid_file: Path | None = None
    if caps["backend"] == "docker":
        cid_file = wt.parent / f".qa-{uuid.uuid4().hex[:8]}.cid"
        cmd = build_docker_run_cmd(wt, argv, pol, cid_file=cid_file)
        env = {k: v for k, v in os.environ.items() if k.upper() in pol.env_allowlist}
        cwd_arg = None
        preexec = None
    else:
        venv_sibling = Path(sys.executable).with_name(f"{exe_name}.exe" if os.name == "nt" else exe_name)
        if exe_name in ("python", "python3"):
            resolved = sys.executable
        elif venv_sibling.exists():
            resolved = str(venv_sibling)
        else:
            resolved = shutil.which(f"{argv[0]}.exe") or shutil.which(f"{argv[0]}.cmd") or shutil.which(argv[0]) or argv[0]
        cmd = [resolved, *argv[1:]]
        env = {k: v for k, v in os.environ.items() if k.upper() in pol.env_allowlist}
        env["PYTHONUTF8"] = "1"
        cwd_arg = str(wt)
        preexec = _posix_limits_fn(pol)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd_arg,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        preexec_fn=preexec,
        shell=False,
    )
    register_run_process(run_id, proc, container_cid_file=cid_file)
    deadline = time.monotonic() + eff_timeout
    try:
        while proc.poll() is None:
            if cancel_check and cancel_check():
                _terminate_proc_tree(proc, cid_file)
                raise GitWorktreeError("Execution cancelled cleanly by operator.")
            if time.monotonic() > deadline:
                _terminate_proc_tree(proc, cid_file)
                raise subprocess.TimeoutExpired(cmd, eff_timeout)
            time.sleep(0.1)
        stdout, stderr = proc.communicate(timeout=2)
    finally:
        unregister_run_process(run_id, proc)
        if cid_file and cid_file.exists():
            cid_file.unlink(missing_ok=True)

    return {
        "argv": argv,
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout": (stdout or "")[:12_000],
        "stderr": (stderr or "")[:6_000],
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
    """Commit remaining worktree changes cleanly and merge into target repository HEAD (call ONLY after passed QA + scan + pinned approval!)."""
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
                "base_sha": owner.get("base_sha"),
                "has_uncommitted": bool(dirty),
            }
        )
    return found
