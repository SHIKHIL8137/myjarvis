"""MCP wired into the real Assistant/API: tool exposure to the brain, execution path, approvals via the shared endpoint, settings."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.api import create_app
from jarvis.config import MCPServerSpec

H = {"X-Jarvis": "1"}
FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


@pytest.fixture
def client(settings, assistant, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda patch, *a: None)
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda patch, *a: None)
    assistant.mcp.audit_file = tmp_path / "audit.jsonl"
    with TestClient(create_app(settings, assistant)) as c:
        c.a = assistant
        yield c


def _enable(client, **kw):
    spec = dict(command=sys.executable, args=[FAKE], enabled=True, startup_timeout_s=30, call_timeout_s=20)
    spec.update(kw)
    client.a.settings.mcp.servers["fake"] = MCPServerSpec(**spec)


def test_nothing_runs_by_default(client):
    assert client.get("/api/v2/mcp/servers").json() == {"servers": [], "tools": []}
    assert client.a.mcp.tool_schemas() == []


def test_server_ops_discovery_and_brain_tools(client):
    _enable(client)
    r = client.post("/api/v2/mcp/servers/fake/start", headers=H)
    assert r.status_code == 200 and r.json()["state"] == "ready"
    listing = client.get("/api/v2/mcp/servers").json()
    assert {t["full_name"] for t in listing["tools"]} >= {"fake.echo", "fake.write_note"}
    assert all("env" not in s and "input_schema" not in json.dumps(listing) for s in listing["servers"])
    names = [t["function"]["name"] for t in client.a.mcp.tool_schemas()]
    assert "fake.echo" in names
    assert client.post("/api/v2/mcp/servers/nope/start", headers=H).status_code == 404
    assert client.post("/api/v2/mcp/servers/fake/stop", headers=H).json()["state"] == "stopped"


def test_disabled_server_cannot_be_started_via_api(client):
    _enable(client, enabled=False)
    r = client.post("/api/v2/mcp/servers/fake/start", headers=H)
    assert r.status_code == 409 and "disabled" in r.text


def test_assistant_execute_tool_read_and_write_paths(client):
    _enable(client)
    client.post("/api/v2/mcp/servers/fake/start", headers=H)
    a = client.a

    async def go():
        out, _ = await a._execute_tool({"name": "fake.echo", "arguments": {"text": "yo"}})
        assert json.loads(out)["ok"] and "echo:yo" in out
        out2, _ = await a._execute_tool({"name": "fake.write_note", "arguments": {"text": "n"}})
        return json.loads(out2)

    res = client.portal.call(go)
    assert res["status"] == "approval_required"
    # approve through the SAME endpoint used by every other approval; the MCP call then runs
    d = client.post(f"/api/v2/approvals/{res['approval_token']}/decide", json={"approved": True}, headers=H)
    assert d.status_code == 200 and "wrote:n" in d.json()["mcp_result"]["result"]
    again = client.post(f"/api/v2/approvals/{res['approval_token']}/decide", json={"approved": True}, headers=H)
    assert again.status_code == 409  # replay rejected by the shared approval service


def test_denied_approval_never_executes(client):
    _enable(client)
    client.post("/api/v2/mcp/servers/fake/start", headers=H)
    res = client.portal.call(client.a.mcp.call_tool, "fake.delete_item", {"name": "z"})
    d = client.post(f"/api/v2/approvals/{res['approval_token']}/decide", json={"approved": False}, headers=H).json()
    assert "mcp_result" not in d
    audit = (client.a.mcp.audit_file).read_text(encoding="utf-8")
    assert "deleted:z" not in audit


def test_settings_patch_never_stores_literal_credentials(client):
    bad = {"mcp": {"servers": {"x": {"command": "npx", "env": {"API_TOKEN": "literal-secret-value"}}}}}
    assert client.put("/api/v2/settings", json=bad, headers=H).status_code in (400, 422)
    ok = {"mcp": {"servers": {"x": {"command": "npx", "env": {"API_TOKEN": "${HA_TOKEN}"}, "enabled": False}}}}
    r = client.put("/api/v2/settings", json=ok, headers=H)
    assert r.status_code == 200, r.text
    shown = json.dumps(r.json()["settings"]["mcp"])
    assert "HA_TOKEN" not in shown and r.json()["settings"]["mcp"]["servers"]["x"]["enabled"] is False


# ---- dictation wired into the real Assistant ----
def test_dictation_disabled_by_default_and_uses_shared_whisper(client):
    a = client.a
    assert a.dictation.stt is a.stt  # the same Whisper object, never a copy
    st = client.get("/api/v2/dictation/status").json()
    assert st["enabled"] is False and st["running"] is False and st["hotkey"] == "ctrl+alt+space"
    assert client.get("/api/v2/dictation/history").json()["items"] == []
    a.dictation.history.add("hello")
    assert client.get("/api/v2/dictation/history").json()["items"][0]["text"] == "hello"
    assert client.delete("/api/v2/dictation/history", headers=H).json()["deleted"] == 1


def test_dictation_recording_silences_tts_and_voice(client, monkeypatch):
    a = client.a
    calls = []
    a.speaker.stop = lambda: calls.append("tts_stop")
    a.voice.pause = lambda: calls.append("voice_pause")
    a.voice.resume = lambda: calls.append("voice_resume")
    monkeypatch.setattr(type(a.voice), "mic_active", property(lambda self: True))
    a._on_dictation_recording(True)
    a._on_dictation_recording(False)
    assert calls == ["tts_stop", "voice_pause", "voice_resume"]


# ---- brain integration: MCP tools reach the model, filtered per request ----
def test_brain_receives_mcp_tools_filtered_by_tool_select_and_can_call_them(client):
    _enable(client)
    client.post("/api/v2/mcp/servers/fake/start", headers=H)
    a = client.a
    seen: list[list[str]] = []
    msgs_seen: list[str] = []
    state = {"round": 0}

    async def fake_stream_events(messages, tools=None, **kw):
        seen.append([t["function"]["name"] for t in (tools or [])])
        msgs_seen.append(json.dumps(messages))
        state["round"] += 1
        if state["round"] == 1:  # the "model" decides to call the MCP tool it was offered
            yield {"type": "tool_calls", "calls": [{"name": "fake.echo", "arguments": {"text": "ping"}}]}
        else:
            yield {"type": "token", "text": "done"}

    a.brain.stream_events = fake_stream_events

    async def run():
        evs = []
        async for ev in a.stream_turn("echo the text ping back to me", speak=False):
            evs.append(ev)
        return evs

    evs = client.portal.call(run)
    first = seen[0]
    assert "fake.echo" in first and "discover_tools" in first
    assert "fake.delete_item" not in first and "fake.slow" not in first  # irrelevant MCP tools are withheld
    assert len([n for n in first if n != "discover_tools"]) <= a.settings.tools_max_per_turn
    assert any(e.get("type") == "tool" and e.get("name") == "fake.echo" for e in evs)  # the call went through the real execution path
    assert len(seen) == 2 and "echo:ping" in msgs_seen[1] and "jarvis_untrusted_data" in msgs_seen[1]  # result fed back wrapped as untrusted
