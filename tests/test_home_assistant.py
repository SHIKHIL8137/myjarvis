"""Home Assistant preset on the MCP client: reads allowed, every change approved, lock/cover/alarm also need spoken confirmation."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from jarvis.agents.approvals import ApprovalService
from jarvis.config import MCPSpec, load_settings
from jarvis.db.engine import DatabaseManager
from jarvis.integrations.mcp_presets import HOMEASSISTANT, PRESETS, preset_config
from jarvis.mcp_client import MCPManager

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def _mgr(tmp_path, *, network=False, online=False):
    s = load_settings()
    # the real preset (policy, keywords), pointed at the local fake server instead of `uvx ha-mcp`
    spec = HOMEASSISTANT.model_copy(update={"command": sys.executable, "args": [FAKE], "env": {}, "enabled": True, "network": network, "startup_timeout_s": 30})
    s.mcp = MCPSpec(servers={"homeassistant": spec})
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    appr = ApprovalService(db)
    return MCPManager(s, appr, audit_file=tmp_path / "audit.jsonl", is_online_fn=lambda: online), appr


def _audit(tmp_path):
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def run(c):
    return asyncio.run(c)


def test_preset_is_disabled_by_default_and_uses_env_references_only():
    assert PRESETS["homeassistant"].enabled is False and HOMEASSISTANT.network is True
    assert all(v.startswith("${") for v in HOMEASSISTANT.env.values())  # no literal credentials anywhere
    assert preset_config("homeassistant")["mcp"]["servers"]["homeassistant"]["enabled"] is False
    assert HOMEASSISTANT.allowed_tools == [] and HOMEASSISTANT.read_only_tools == []  # no hardcoded tool list: policy comes from discovery


def test_read_only_queries_run_without_approval_even_for_locks(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        r = await m.call_tool("homeassistant.ha_get_state", {"entity_id": "lock.front_door"})
        assert r["ok"] and "state:lock.front_door=on" in r["result"] and "jarvis_untrusted_data" in r["result"]
        assert (await m.call_tool("homeassistant.ha_list_entities", {}))["ok"]
        assert appr.list_pending() == []
        await m.shutdown()
    run(go())


def test_device_change_needs_approval_and_does_not_run_until_approved(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        r = await m.call_tool("homeassistant.ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
        assert r["status"] == "approval_required" and not r["spoken_confirmation_required"]
        assert "called:" not in json.dumps(r)
        row = appr.get(r["approval_token"])
        assert row["action_type"] == "mcp_write" and row["risk_tier"] == "EXECUTE"
        appr.decide(r["approval_token"], approved=True)
        out = await m.execute_approved(r["approval_token"])
        assert out["ok"] and "called:light.turn_on" in out["result"]
        await m.shutdown()
    run(go())


@pytest.mark.parametrize("args", [
    {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
    {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage"},
    {"domain": "alarm_control_panel", "service": "alarm_disarm", "entity_id": "alarm_control_panel.home"},
])
def test_locks_covers_alarms_need_approval_plus_spoken_confirmation(tmp_path, args):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        r = await m.call_tool("homeassistant.ha_call_service", args)
        assert r["status"] == "approval_required" and r["spoken_confirmation_required"] is True
        tok = r["approval_token"]
        row = appr.get(tok)
        assert row["action_type"] == "mcp_critical" and f"confirm {row['args']['code']}" in row["preview_diff"]
        # approval alone is not enough
        appr.decide(tok, approved=True)
        assert (await m.execute_approved(tok))["status"] == "awaiting_spoken_confirmation"
        # typed text, wrong code, or a non-voice source never counts
        assert await m.confirm_spoken(f"confirm {row['args']['code']}", source="text") is None
        wrong = "0000" if row["args"]["code"] != "0000" else "1111"
        assert await m.confirm_spoken(f"confirm {wrong}", source="voice") is None
        # correct spoken code from voice executes
        spoken = " ".join(DIGITS[d] for d in row["args"]["code"])
        out = await m.confirm_spoken(f"confirm {spoken}", source="voice")
        assert out["ok"] and "called:" in out["result"]
        assert appr.get(tok)["status"] == "consumed"
        await m.shutdown()
    run(go())


def test_spoken_confirmation_alone_without_owner_approval_never_executes(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        r = await m.call_tool("homeassistant.ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})
        code = appr.get(r["approval_token"])["args"]["code"]
        res = await m.confirm_spoken("confirm " + " ".join(DIGITS[d] for d in code), source="voice")
        assert res["status"] == "awaiting_owner_approval"
        with pytest.raises(PermissionError):
            await m.execute_approved(r["approval_token"])  # still only 'pending'
        assert not any("called:" in json.dumps(e) for e in _audit(tmp_path))
        await m.shutdown()
    run(go())


def test_denied_approval_never_executes_and_token_cannot_be_replayed(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        r = await m.call_tool("homeassistant.ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})
        appr.decide(r["approval_token"], approved=False)
        with pytest.raises(PermissionError):
            await m.execute_approved(r["approval_token"])
        r2 = await m.call_tool("homeassistant.ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})
        appr.decide(r2["approval_token"], approved=True)
        await m.execute_approved(r2["approval_token"])
        with pytest.raises(PermissionError):
            await m.execute_approved(r2["approval_token"])
        await m.shutdown()
    run(go())


def test_llm_cannot_bypass_by_naming_tool_differently_or_unknown_tool(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path)
        await m.start("homeassistant")
        assert (await m.call_tool("homeassistant.does_not_exist", {}))["ok"] is False
        assert (await m.call_tool("other.ha_call_service", {}))["ok"] is False
        r = await m.call_tool("homeassistant.ha_call_service", {"domain": "LOCK", "service": "UNLOCK", "entity_id": "Lock.Front-Door"})
        assert r["spoken_confirmation_required"]  # case-insensitive matching
        await m.shutdown()
    run(go())


def test_offline_mode_blocks_the_preset_until_online_is_explicitly_enabled(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, network=True, online=False)
        with pytest.raises(Exception, match="offline"):
            await m.start("homeassistant")
        m2, _ = _mgr(tmp_path, network=True, online=True)
        assert (await m2.start("homeassistant"))["state"] == "ready"
        await m2.shutdown()
    run(go())


def test_every_action_is_audited_without_argument_values(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("homeassistant")
        await m.call_tool("homeassistant.ha_get_state", {"entity_id": "lock.front_door"})
        r = await m.call_tool("homeassistant.ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})
        appr.decide(r["approval_token"], approved=False)
        await m.shutdown()
    run(go())
    rows = [e for e in _audit(tmp_path) if e["action"] == "mcp_call"]
    assert len(rows) == 2
    assert rows[0]["allowed"] is True and rows[1]["allowed"] is False and "approval_required" in rows[1]["reason"]
    assert "front_door" not in json.dumps(rows)  # values are never written to the audit log


def test_install_preset_via_api_stays_disabled_and_needs_no_secret(settings, assistant, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.api import create_app

    monkeypatch.setattr("jarvis.api.save_overrides", lambda p, *a: None)
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda p, *a: None)
    with TestClient(create_app(settings, assistant)) as c:
        H = {"X-Jarvis": "1"}
        assert c.get("/api/v2/mcp/presets").json()["presets"][0]["installed"] is False
        r = c.post("/api/v2/mcp/presets/homeassistant/install", headers=H)
        assert r.status_code == 200, r.text
        srv = r.json()["settings"]["servers"]["homeassistant"]
        assert srv["enabled"] is False and "HOMEASSISTANT_TOKEN" in srv["env"]
        assert c.get("/api/v2/mcp/presets").json()["presets"][0]["installed"] is True
        assert c.post("/api/v2/mcp/presets/homeassistant/install", headers=H).status_code == 409
        st = c.post("/api/v2/mcp/servers/homeassistant/start", headers=H)
        assert st.status_code == 409 and "disabled" in st.text  # installed != running
