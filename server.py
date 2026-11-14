"""Entry point: python server.py [--tray] [--no-browser]  ->  http://127.0.0.1:4700 (loopback only).

`--tray` (default in the packaged build) adds the Windows tray icon whose menu controls this same process.
"""
import argparse
import contextlib
import os
import signal
import sys
import threading
import webbrowser

# Models are already on disk; never let a library try to reach the hub at runtime.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

for _name in ("stdout", "stderr"):  # windowed (console=False) builds have no std streams; uvicorn logging expects them
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

import uvicorn  # noqa: E402

from jarvis.api import create_app  # noqa: E402
from jarvis.config import ROOT, ensure_app_home, load_settings  # noqa: E402


class JarvisServer(uvicorn.Server):
    """uvicorn re-raises the received signal after shutdown, which turns a graceful Ctrl+Break into exit code 3 on Windows.
    Install our own handlers instead: any of SIGINT/SIGTERM/SIGBREAK (or the tray's Quit) -> clean shutdown -> exit code 0."""

    @contextlib.contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])  # type: ignore[attr-defined]
        previous = {s: signal.signal(s, lambda *_: setattr(self, "should_exit", True)) for s in sigs}
        try:
            yield
        finally:
            for s, h in previous.items():
                signal.signal(s, h)


def build_server(settings) -> uvicorn.Server:
    app = create_app(settings)
    server = JarvisServer(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning", timeout_graceful_shutdown=5))  # an open dashboard SSE stream must not block Quit
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)  # POST /api/system/shutdown
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tray", action="store_true", default=bool(getattr(sys, "frozen", False)), help="show the Windows tray icon")
    ap.add_argument("--no-tray", dest="tray", action="store_false")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard on start")
    args = ap.parse_args(argv)

    ensure_app_home()  # installed build: seed %LOCALAPPDATA%\JARVIS from the bundle
    s = load_settings()
    server = build_server(s)
    url = f"http://{s.host}:{s.port}"
    tray = None
    if args.tray:
        from jarvis.tray import JarvisTray, TrayActions

        actions = TrayActions(url, ROOT / "logs", on_quit=lambda: setattr(server, "should_exit", True))
        tray = JarvisTray(actions.menu, tooltip="JARVIS")
        tray.start()
    if args.tray and not args.no_browser:
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    try:
        server.run()
    finally:
        if tray:
            tray.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
