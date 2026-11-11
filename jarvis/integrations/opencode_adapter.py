"""Headless OpenCode CLI adapter (`opencode 1.19.5`) jailed cleanly to an isolated worktree directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess

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
    """Raised when OpenCode binary or sandboxed execution fails."""


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
) -> dict:
    """Execute a supervised OpenCode coding run strictly inside `workdir` without unrelated secret env vars."""
    wd = Path(workdir).resolve()
    if not wd.exists() or not wd.is_dir():
        raise OpenCodeError(f"OpenCode workdir does not exist: {wd}")

    exe = _find_opencode(bin_name)
    cmd = [exe, "run", prompt, "--dir", str(wd), "--format", "json"]
    if model:
        cmd.extend(["-m", model])
    if agent:
        cmd.extend(["--agent", agent])

    # Scrubbed environment: never expose Jarvis API keys from data/secrets.json or unrelated env tokens
    env = {k: v for k, v in os.environ.items() if k.upper() in _SAFE_ENV_KEYS}
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=str(wd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
    )
    parsed = parse_opencode_json_lines(proc.stdout)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "workdir": str(wd),
        "events": parsed["events"],
        "final_text": parsed["final_text"],
        "stdout": proc.stdout[:16_000],
        "stderr": proc.stderr[:4_000],
    }

