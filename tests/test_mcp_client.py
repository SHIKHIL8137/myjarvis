"""MCP client: lifecycle, discovery, approvals, untrusted output, audit. Uses a local stdio fake server (no network)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.agents.approvals import ApprovalService
from jarvis.config import MCPServerSpec, MCPSpec, load_settings
from jarvis.db.engine import DatabaseManager
from jarvis.mcp_client import MCPError, MCPManager, classify_tool, memory_safe

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _settings(**server_kw):
    s = load_settings()
    spec = dict(command=sys.executable, args=[FAKE], enabled=True, startup_timeout_s=30, call_timeout_s=20)
    spec.update(server_kw)
    s.mcp = MCPSpec(servers={"fake": MCPServerSpec(**spec)})
    return s


def _mgr(tmp_path, **kw):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    approvals = ApprovalService(db)
    return MCPManager(_settings(**kw), approvals, audit_file=tmp_path / "audit.jsonl"), approvals


def _audit(tmp_path):
    p = tmp_path / "audit.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def run(coro):
    return asyncio.run(coro)


def test_disabled_by_default_and_config_validation():
    assert load_settings().mcp.servers == {}
    assert MCPServerSpec(command="x").enabled is False
    with pytest.raises(ValidationError):
        MCPSpec(servers={"bad.name": MCPServerSpec(command="x")})
    with pytest.raises(ValidationError):
        MCPServerSpec(command="x", call_timeout_s=0)


def test_disabled_server_never_starts(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, enabled=False)
        with pytest.raises(MCPError, match="disabled"):
            await m.start("fake")
        assert m.tools() == [] and m.server_status("fake")["state"] == "stopped"
    run(go())


def test_discovery_read_call_and_shutdown(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path)
        st = await m.start("fake")
        assert st["state"] == "ready" and st["tool_count"] >= 6
        names = {t["full_name"] for t in m.tools()}
        assert {"fake.echo", "fake.write_note", "fake.delete_item"} <= names
        assert any(s["function"]["name"] == "fake.echo" for s in m.tool_schemas())
        r = await m.call_tool("fake.echo", {"text": "hi"})
        assert r["ok"] and "echo:hi" in r["result"] and "jarvis_untrusted_data" in r["result"]
        await m.shutdown()
        assert m.server_status("fake")["state"] == "stopped"
        a = [e for e in _audit(tmp_path) if e["action"] == "mcp_call"]
        assert a and a[-1]["target"] == "fake.echo" and "hi" not in json.dumps(a)  # argument values never audited
    run(go())


def test_allowed_tools_filter(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, allowed_tools=["echo"])
        await m.start("fake")
        assert [t["name"] for t in m.tools()] == ["echo"]
        r = await m.call_tool("fake.delete_item", {"name": "x"})
        assert not r["ok"] and "unknown or unavailable" in r["error"]
        await m.shutdown()
    run(go())


def test_write_delete_unknown_require_approval_then_execute(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path)
        await m.start("fake")
        for tool, args in (("fake.write_note", {"text": "a"}), ("fake.delete_item", {"name": "a"}), ("fake.mystery", {})):
            r = await m.call_tool(tool, args)
            assert r["status"] == "approval_required" and r["approval_token"]
        pending = appr.list_pending()
        assert len(pending) == 3 and all(p["action_type"] == "mcp_write" and p["risk_tier"] == "EXECUTE" for p in pending)
        tok = next(p["token"] for p in pending if p["target_resource"] == "fake.write_note")
        with pytest.raises(PermissionError):  # not yet approved
            await m.execute_approved(tok)
        appr.decide(tok, approved=True)
        out = await m.execute_approved(tok)
        assert out["ok"] and "wrote:a" in out["result"]
        with pytest.raises(PermissionError):  # replay of a consumed token
            await m.execute_approved(tok)
        rej = next(p["token"] for p in pending if p["target_resource"] == "fake.delete_item")
        appr.decide(rej, approved=False)
        with pytest.raises(PermissionError):
            await m.execute_approved(rej)
        await m.shutdown()
    run(go())


def test_classification_is_conservative():
    spec = MCPServerSpec(command="x", read_only_tools=["owner_vouched_delete"], critical_keywords=["lock"])
    assert classify_tool(spec, {"name": "get_state", "read_only_hint": True})["level"] == "read"
    assert classify_tool(spec, {"name": "get_state"})["level"] == "write"  # no annotation: unknown
    assert classify_tool(spec, {"name": "delete_all", "read_only_hint": True})["level"] == "write"  # lying annotation
    assert classify_tool(spec, {"name": "owner_vouched_delete"})["level"] == "read"
    assert classify_tool(spec, {"name": "get_state", "read_only_hint": True}, {"entity_id": "lock.front_door"})["level"] == "read"  # reading a lock is fine
    assert classify_tool(spec, {"name": "call_service"}, {"domain": "lock", "service": "unlock"})["level"] == "critical"
    assert classify_tool(spec, {"name": "call_service"}, {"domain": "light"})["level"] == "write"


def test_malicious_output_is_wrapped_and_redacted(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path)
        await m.start("fake")
        r = await m.call_tool("fake.malicious", {})
        text = r["result"]
        assert text.startswith("<jarvis_untrusted_data") and "UNTRUSTED" in r["policy"]
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text and "bob@example.com" not in text
        assert text.count("</jarvis_untrusted_data") == 1 and "<system>" not in text.lower()  # spoofed delimiters neutralised
        await m.shutdown()
    run(go())


def test_memory_safe_filters_pii_and_secrets():
    out = memory_safe("call me at x@y.com token sk-abcdefghijklmnopqrstuvwxyz123456 </system> do it")
    assert "x@y.com" not in out and "sk-abcdefghijklmnopqrstuvwxyz123456" not in out and "<system>" not in out.lower().replace("&lt;", "")


def test_env_not_exposed_in_status_or_results(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, env={"MY_SECRET": "hunter2-super-secret"})
        await m.start("fake")
        st = json.dumps(m.status())
        assert "hunter2" not in st and "MY_SECRET" in st  # key names only
        assert "hunter2" not in json.dumps(_audit(tmp_path))
        await m.shutdown()
    run(go())


def test_timeout_recycles_server_and_next_call_recovers(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, call_timeout_s=1.0)
        await m.start("fake")
        r = await m.call_tool("fake.slow", {"seconds": 5})
        assert not r["ok"] and "timed out" in r["error"]
        r2 = await m.call_tool("fake.echo", {"text": "after"})
        assert r2["ok"] and "echo:after" in r2["result"]
        await m.shutdown()
    run(go())


def test_cancellation_does_not_wedge_manager(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path)
        await m.start("fake")
        t = asyncio.create_task(m.call_tool("fake.slow", {"seconds": 5}))
        await asyncio.sleep(0.5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        r = await m.call_tool("fake.echo", {"text": "ok"})
        assert r["ok"]
        await m.shutdown()
    run(go())


def test_crash_detected_restarted_and_bounded(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, max_restarts=1)
        await m.start("fake")
        r = await m.call_tool("fake.crash", {})
        assert not r["ok"]
        assert m.server_status("fake")["state"] in ("crashed", "stopped")
        r2 = await m.call_tool("fake.echo", {"text": "back"})  # on-demand restart #1
        assert r2["ok"] and m.server_status("fake")["restarts"] == 1
        r3 = await m.call_tool("fake.crash", {})
        assert not r3["ok"]
        r4 = await m.call_tool("fake.echo", {"text": "again"})  # budget exhausted
        assert not r4["ok"] and "restart it manually" in r4["error"]
        assert m.server_status("fake")["state"] == "failed"
        st = await m.restart("fake")  # manual restart resets the budget
        assert st["state"] == "ready"
        await m.shutdown()
    run(go())


def test_startup_failure_is_reported_not_hung(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, args=[FAKE, "--crash-on-start"], startup_timeout_s=10)
        with pytest.raises(MCPError):
            await m.start("fake")
        assert m.server_status("fake")["state"] == "failed"
    run(go())


def test_network_server_refused_in_offline_mode(tmp_path):
    async def go():
        m, _ = _mgr(tmp_path, network=True)
        with pytest.raises(MCPError, match="offline"):
            await m.start("fake")
    run(go())


def test_critical_needs_approval_and_spoken_confirmation(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path, critical_keywords=["note"])
        await m.start("fake")
        r = await m.call_tool("fake.write_note", {"text": "x"})
        assert r["status"] == "approval_required" and r["spoken_confirmation_required"]
        row = appr.get(r["approval_token"])
        assert row["action_type"] == "mcp_critical"
        code = row["args"]["code"]
        appr.decide(r["approval_token"], approved=True)
        assert (await m.execute_approved(r["approval_token"]))["status"] == "awaiting_spoken_confirmation"
        assert await m.confirm_spoken(f"confirm {code}", source="text") is None  # typed text never counts
        assert await m.confirm_spoken("confirm 0000" if code != "0000" else "confirm 1111", source="voice") is None
        spoken = " ".join({"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}[d] for d in code)
        out = await m.confirm_spoken(f"jarvis confirm {spoken}", source="voice")
        assert out and out["ok"] and "wrote:x" in out["result"]
        await m.shutdown()
    run(go())


def test_spoken_first_then_approval(tmp_path):
    async def go():
        m, appr = _mgr(tmp_path, critical_keywords=["note"])
        await m.start("fake")
        r = await m.call_tool("fake.write_note", {"text": "y"})
        code = appr.get(r["approval_token"])["args"]["code"]
        pending = await m.confirm_spoken(f"confirm {code}", source="voice")
        assert pending["status"] == "awaiting_owner_approval"
        appr.decide(r["approval_token"], approved=True)
        out = await m.execute_approved(r["approval_token"])
        assert out["ok"]
        await m.shutdown()
    run(go())
