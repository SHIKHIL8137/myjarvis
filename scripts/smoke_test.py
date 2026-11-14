"""Installer smoke test: launch the packaged app (or `python server.py` with --dev), check the API, shut it down cleanly.

  python scripts/smoke_test.py --exe dist\\jarvis\\jarvis.exe
  python scripts/smoke_test.py --dev            # same checks against the development entry point

Uses a throw-away JARVIS_HOME and a free port, so it never touches your real data or a running instance.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # loopback only
        return r.status, r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="path to the packaged jarvis.exe")
    ap.add_argument("--dev", action="store_true", help="run server.py from the repo instead")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    if not args.exe and not args.dev:
        ap.error("pass --exe PATH or --dev")

    port = free_port()
    home = tempfile.mkdtemp(prefix="jarvis-smoke-")
    env = {**os.environ, "JARVIS_HOME": home, "JARVIS_PORT": str(port), "JARVIS_OVERRIDES_FILE": str(Path(home) / "settings.json")}
    if args.exe:
        args.exe = str(Path(args.exe).resolve())
    cmd = [args.exe, "--no-tray", "--no-browser"] if args.exe else [sys.executable, str(ROOT / "server.py"), "--no-tray", "--no-browser"]
    print("launching:", " ".join(cmd), f"(port {port}, home {home})")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    ok = False
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + args.timeout
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                print("process exited early with code", proc.returncode)
                print((proc.stdout.read() or b"").decode("utf-8", "replace")[-2000:])
                return 1
            try:
                _, body = get(base + "/api/health")
                health = json.loads(body)
                break
            except Exception:
                time.sleep(1)
        if health is None:
            print("API did not become healthy in time")
            return 1
        assert health.get("status") == "ok", health
        status, page = get(base + "/")
        assert status == 200 and b"<html" in page.lower(), "frontend not served"
        status, body = get(base + "/api/v2/mcp/servers")
        assert status == 200 and json.loads(body)["servers"] == [], "MCP must be empty by default"
        print("health:", health)
        ok = True
    finally:
        if proc.poll() is None:
            try:
                # the packaged app is a windowed process (no console), so Ctrl+Break cannot reach it: ask it to shut down over its own API
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown", data=b"{}", method="POST", headers={"X-Jarvis": "1", "Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception:
                    proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("did not exit within 30s: killing")
                proc.kill()
                proc.wait()
                ok = False
        print("exit code:", proc.returncode)
        if proc.returncode != 0:  # a handled uvicorn shutdown exits 0
            ok = False
    print("SMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
