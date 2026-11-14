"""Stage D tests: Desktop & Browser Action Registry, Risk Tiers, Confirmation, SSRF & Security."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from starlette.testclient import TestClient

from jarvis.actions import ACTION_REGISTRY
from jarvis.api import create_app

H = {"X-Jarvis": "1"}


def _setup_actions(assistant, *, allow_input: bool = False):
    assistant.actions.spec.enabled = True
    assistant.actions.spec.allowed_apps = {"notepad": ["notepad.exe"]}
    assistant.actions.spec.confirm_levels = ["modify", "destructive", "system"]
    assistant.actions.spec.allowed_url_domains = ["docs.python.org", "github.com"]
    assistant.actions.spec.allow_screenshots = True
    assistant.actions.screen_guard = None  # keep this test independent of whatever windows are open on the dev machine
    assistant.actions.spec.allow_media_controls = True
    assistant.actions.spec.allow_window_controls = True
    assistant.actions.spec.allow_input_automation = allow_input


def test_action_registry_metadata_and_open_app_security(assistant, settings, monkeypatch):
    _setup_actions(assistant)
    client = TestClient(create_app(settings, assistant=assistant))

    reg = client.get("/api/v2/actions/registry")
    assert reg.status_code == 200
    body = reg.json()
    ids = {a["action_id"] for a in body["actions"]}
    assert ids == set(ACTION_REGISTRY.keys())
    for item in body["actions"]:
        assert item["risk_tier"] in {"Read-only", "Low", "Medium", "High"}
        assert isinstance(item["timeout_s"], (int, float))
        assert isinstance(item["arg_schema"], dict)
        assert isinstance(item["required_permissions"], list)

    # Disallowed app rejected cleanly
    bad = client.post("/api/v2/actions/execute", json={"action": "open_app", "args": {"app": "powershell"}}, headers=H)
    assert bad.status_code == 403
    assert "allowed_apps" in bad.json()["detail"]

    # NL explicit blocked app returns clear denial instead of falling through silently
    nl_bad = client.post("/api/chat", json={"message": "open app powershell"}, headers=H)
    assert nl_bad.status_code == 200
    assert nl_bad.json()["ok"] is False
    assert "allowed_apps" in nl_bad.json()["answer"]

    # Allowlisted app uses verification check and records started status
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=42424))
    ok = client.post("/api/v2/actions/execute", json={"action": "open_app", "args": {"app": "notepad"}}, headers=H)
    assert ok.status_code == 200
    assert ok.json()["status"] in {"started", "completed"}
    assert ok.json()["result"]["pid"] == 42424


def test_window_media_screenshot_and_nl_intents(assistant, settings, monkeypatch):
    _setup_actions(assistant)
    client = TestClient(create_app(settings, assistant=assistant))

    fake_windows = [
        {"hwnd": 101, "title": "Visual Studio Code", "visible": True},
        {"hwnd": 202, "title": "MyJarvis Terminal", "visible": True},
    ]
    focus_calls: list[int] = []
    monkeypatch.setattr("jarvis.actions._enum_windows_win32", lambda: list(fake_windows))
    monkeypatch.setattr("jarvis.actions._get_active_window_win32", lambda: {"hwnd": 202, "title": "MyJarvis Terminal", "supported": True})
    monkeypatch.setattr("jarvis.actions._focus_window_win32", lambda hwnd: focus_calls.append(hwnd) or True)
    monkeypatch.setattr("jarvis.actions._send_media_key_win32", lambda cmd: True)

    # List windows & active window via API and NL
    lw = client.post("/api/v2/actions/execute", json={"action": "list_windows", "args": {}}, headers=H)
    assert lw.status_code == 200
    assert lw.json()["result"]["count"] == 2

    nl_lw = client.post("/api/chat", json={"message": "list open windows"}, headers=H)
    assert nl_lw.status_code == 200
    assert nl_lw.json()["command"] == "list_windows"
    assert "Visual Studio Code" in nl_lw.json()["answer"]

    nl_aw = client.post("/api/chat", json={"message": "what is the active window"}, headers=H)
    assert nl_aw.status_code == 200
    assert nl_aw.json()["command"] == "get_active_window"
    assert "MyJarvis Terminal" in nl_aw.json()["answer"]

    # Focus window works & records target
    fw = client.post("/api/v2/actions/execute", json={"action": "focus_window", "args": {"title": "terminal"}}, headers=H)
    assert fw.status_code == 200
    assert focus_calls == [202]
    assert fw.json()["result"]["verified_foreground"] is True

    # Media control validation + toggle + NL
    bad_media = client.post("/api/v2/actions/execute", json={"action": "media_control", "args": {"command": "self_destruct"}}, headers=H)
    assert bad_media.status_code == 403

    nl_media = client.post("/api/chat", json={"message": "pause media"}, headers=H)
    assert nl_media.status_code == 200
    assert nl_media.json()["command"] == "media_control"
    assert nl_media.json()["ok"] is True

    assistant.actions.spec.allow_media_controls = False
    dis_media = client.post("/api/v2/actions/execute", json={"action": "media_control", "args": {"command": "pause"}}, headers=H)
    assert dis_media.status_code == 403
    assistant.actions.spec.allow_media_controls = True

    # Screenshot path traversal containment & size verification
    fake_img = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: fake_img)
    shot = client.post("/api/v2/actions/execute", json={"action": "take_screenshot", "args": {"filename": "../../escape.png"}}, headers=H)
    assert shot.status_code == 200
    saved = Path(shot.json()["result"]["path"]).resolve()
    captures_dir = (Path(assistant.filesystem.default_base) / "captures").resolve()
    assert saved.parent == captures_dir
    assert saved.exists() and saved.stat().st_size >= 128

    assistant.actions.spec.allow_screenshots = False
    dis_shot = client.post("/api/v2/actions/execute", json={"action": "take_screenshot", "args": {}}, headers=H)
    assert dis_shot.status_code == 403


def test_input_automation_opt_in_and_confirmation_flow(assistant, settings):
    _setup_actions(assistant, allow_input=False)
    client = TestClient(create_app(settings, assistant=assistant))

    # Disabled by default
    denied = client.post("/api/v2/actions/execute", json={"action": "keyboard_type", "args": {"text": "hello"}}, headers=H)
    assert denied.status_code == 403
    assert "allow_input_automation" in denied.json()["detail"]

    # Enable input automation -> must still require high-risk owner confirmation
    assistant.actions.spec.allow_input_automation = True
    req = client.post("/api/v2/actions/execute", json={"action": "mouse_click", "args": {"x": 120, "y": 240}}, headers=H)
    assert req.status_code == 200
    assert req.json()["confirmation_required"] is True
    token = req.json()["token"]

    # Rejecting blocks execution and invalidates reuse
    rej = client.post(f"/api/v2/actions/{token}/reject", headers=H)
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"
    reuse = client.post(f"/api/v2/actions/{token}/confirm", headers=H)
    assert reuse.status_code == 409

    # New request + explicit confirmation succeeds
    req2 = client.post("/api/v2/actions/execute", json={"action": "keyboard_type", "args": {"text": "status check"}}, headers=H)
    tok2 = req2.json()["token"]
    conf = client.post(f"/api/v2/actions/{tok2}/confirm", headers=H)
    assert conf.status_code == 200
    assert conf.json()["status"] in {"started", "completed"}
    assert conf.json()["result"]["executed"] is True


def test_open_url_ssrf_blocks_and_browser_security_tiers(assistant, settings, monkeypatch):
    _setup_actions(assistant)
    client = TestClient(create_app(settings, assistant=assistant))
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u, new=2: opened_urls.append(u) or True)

    # SSRF / unsafe protocols / embedded credentials strictly blocked
    for unsafe_url in (
        "http://example.com",
        "file:///C:/Windows/System32/drivers/etc/hosts",
        "https://127.0.0.1:8080/admin",
        "https://localhost/secret",
        "https://user:pass@docs.python.org/3/",
    ):
        r = client.post("/api/v2/actions/execute", json={"action": "open_url", "args": {"url": unsafe_url}}, headers=H)
        assert r.status_code == 403, f"Expected 403 for {unsafe_url}, got {r.status_code}"

    # Allowlisted public HTTPS URL opens directly via NL command
    nl_ok = client.post("/api/chat", json={"message": "open url https://docs.python.org/3/"}, headers=H)
    assert nl_ok.status_code == 200
    assert nl_ok.json()["command"] == "open_url"
    assert nl_ok.json()["ok"] is True
    assert opened_urls == ["https://docs.python.org/3/"]

    # Non-allowlisted HTTPS domain elevates to system tier & requires confirmation
    unlisted = client.post(
        "/api/v2/actions/execute",
        json={"action": "open_url", "args": {"url": "https://unlisted.example.org/docs"}},
        headers=H,
    )
    assert unlisted.status_code == 200
    assert unlisted.json()["confirmation_required"] is True
    assert unlisted.json()["level"] == "SYSTEM"

    # Browser 3-tier security:
    # Tier 1: public_read allowed immediately on allowlisted domain
    t1 = client.post(
        "/api/v2/actions/execute",
        json={"action": "browser_action", "args": {"operation": "public_read", "url": "https://github.com/python/cpython"}},
        headers=H,
    )
    assert t1.status_code == 200
    assert t1.json()["ok"] is True
    assert t1.json()["result"]["credentials_exposed"] is False

    # Never expose credentials
    cred = client.post(
        "/api/v2/actions/execute",
        json={
            "action": "browser_action",
            "args": {"operation": "open_authenticated", "url": "https://github.com", "expose_credentials": True},
        },
        headers=H,
    )
    assert cred.status_code == 403

    # Tier 3: side_effect ALWAYS requires confirmation even if confirm_levels is emptied
    assistant.actions.spec.confirm_levels = []
    t3 = client.post(
        "/api/v2/actions/execute",
        json={"action": "browser_action", "args": {"operation": "side_effect", "url": "https://github.com/issues/new", "summary": "Submit form"}},
        headers=H,
    )
    assert t3.status_code == 200
    assert t3.json()["confirmation_required"] is True
