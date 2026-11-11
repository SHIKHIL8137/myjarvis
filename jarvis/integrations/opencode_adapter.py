"""Headless OpenCode CLI adapter executed under configured ExecutionPolicy (Docker container or restricted subprocess)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from .git_worktree import (
    ExecutionPolicy,
    _posix_limits_fn,
    _terminate_proc_tree,
    build_docker_run_cmd,
    is_docker_available,
    register_run_process,
    unregister_run_process,
)

_SAFE_ENV_KEYS = {
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "VIRTUAL_ENV",
    "OLLAMA_HOST",
}


class OpenCodeError(RuntimeError):
    """Raised when OpenCode execution, sandbox policy, or cancellation triggers an abort."""


def _find_opencode(bin_name: str = "opencode") -> str:
    return shutil.which(f"{bin_name}.cmd") or shutil.which(f"{bin_name}.exe") or shutil.which(bin_name) or bin_name


def parse_opencode_json_lines(raw_stdout: str) -> dict:
    """Parse JSON event stream emitted by `opencode run --format json`."""
    events: list[dict] = []
    texts: list[str] = []
    for ln in (raw_stdout or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
            events.append(ev)
            part = ev.get("part") or {}
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(ev.get("text"), str):
                texts.append(ev["text"])
        except json.JSONDecodeError:
            texts.append(s)
    return {"events": events, "final_text": "\n".join(texts).strip()}


def run_opencode_task(
    prompt: str,
    *,
    workdir: str | Path,
    model: str | None = None,
    agent: str | None = None,
    bin_name: str = "opencode",
    timeout_s: int = 180,
    policy: ExecutionPolicy | None = None,
    run_id: str | None = None,
    cancel_check=None,
) -> dict:
    """Execute supervised OpenCode coding strictly inside `workdir` using Docker container or restricted host policy."""
    wd = Path(workdir).resolve()
    if not wd.exists() or not wd.is_dir():
        raise OpenCodeError(f"OpenCode workdir does not exist: {wd}")

    pol = policy or ExecutionPolicy(allowed_workdirs=[wd], timeout_s=timeout_s)
    pol.validate_access(wd, "write")
    eff_timeout = min(int(timeout_s), int(getattr(pol, "timeout_s", timeout_s)))

    inner_args = [bin_name, "run", prompt, "--dir", "/workspace", "--format", "json"]
    if model:
        inner_args.extend(["-m", model])
    if agent:
        inner_args.extend(["--agent", agent])

    docker_ready = is_docker_available() if pol.sandbox_backend == "docker" else False
    if pol.sandbox_backend == "docker" and not docker_ready and not pol.allow_host_fallback:
        raise OpenCodeError(
            "Execution policy requires Docker isolation (`sandbox_backend='docker'`), "
            "but Docker daemon is unavailable. Refusing unisolated host OpenCode execution."
        )
    caps = pol.capabilities(docker_ready=docker_ready)

    cid_file: Path | None = None
    if caps["backend"] == "docker":
        cid_file = wd.parent / f".oc-{uuid.uuid4().hex[:8]}.cid"
        cmd = build_docker_run_cmd(wd, inner_args, pol, cid_file=cid_file)
        env = {k: v for k, v in os.environ.items() if k.upper() in pol.env_allowlist}
        cwd_arg = None
        preexec = None
    else:
        exe = _find_opencode(bin_name)
        cmd = [exe, "run", prompt, "--dir", str(wd), "--format", "json"]
        if model:
            cmd.extend(["-m", model])
        if agent:
            cmd.extend(["--agent", agent])
        allow = set(getattr(pol, "env_allowlist", _SAFE_ENV_KEYS))
        env = {k: v for k, v in os.environ.items() if k.upper() in allow}
        env["PYTHONUTF8"] = "1"
        cwd_arg = str(wd)
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
                raise OpenCodeError("OpenCode task cancelled cleanly by operator.")
            if time.monotonic() > deadline:
                _terminate_proc_tree(proc, cid_file)
                raise OpenCodeError(f"OpenCode task timed out after {eff_timeout}s and was terminated.")
            time.sleep(0.1)
        stdout, stderr = proc.communicate(timeout=2)
    finally:
        unregister_run_process(run_id, proc)
        if cid_file and cid_file.exists():
            cid_file.unlink(missing_ok=True)

    parsed = parse_opencode_json_lines(stdout or "")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "workdir": str(wd),
        "events": parsed["events"],
        "final_text": parsed["final_text"],
        "stdout": (stdout or "")[:16_000],
        "stderr": (stderr or "")[:4_000],
        "isolation": caps,
    }
