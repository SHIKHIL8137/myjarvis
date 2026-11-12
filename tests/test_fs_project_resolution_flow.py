"""Comprehensive tests for natural-language project resolution, permission flow, resumption, and voice/typed parity."""
from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path

from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.filesystem_access import ALL_OPS, WRITE_OPS
from jarvis.intent import route_intent

H = {"X-Jarvis": "1"}


def _make_erp_project(base: Path, name: str = "ERP_V2") -> Path:
    proj = base / name
    (proj / "backend").mkdir(parents=True, exist_ok=True)
    (proj / "frontend").mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(
        json.dumps({"name": "erp-v2", "version": "2.1.0", "scripts": {"dev": "vite"}}),
        encoding="utf-8",
    )
    (proj / "README.md").write_text("# ERP V2\nEnterprise Resource Planning project.\n", encoding="utf-8")
    (proj / "backend" / "server.py").write_text("APP_ENV = 'local'\nPORT = 8000\n", encoding="utf-8")
    (proj / ".env").write_text("SECRET_KEY=super_secret_token_do_not_expose\n", encoding="utf-8")
    return proj


def _dummy_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def test_exact_alias_and_name_variations_and_registered_projects(assistant, settings, tmp_path):
    erp = _make_erp_project(tmp_path, "ERP_V2")
    jarvis_clone = _make_erp_project(tmp_path, "myjarvis")

    client = TestClient(create_app(settings, assistant=assistant))
    r_reg = client.post("/api/v2/projects", json={"name": "myjarvis", "root_path": str(jarvis_clone)}, headers=H)
    assert r_reg.status_code == 200
    r_alias = client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)
    assert r_alias.status_code == 200

    # 1. Exact alias variation "ERP V2" (spaces instead of underscore) resolves to ERP_V2 without Documents fallback
    r_open = client.post("/api/chat", json={"message": "Open the ERP V2 folder."}, headers=H)
    assert r_open.status_code == 200
    d_open = r_open.json()
    assert d_open["command"] == "permission_required"
    pr = d_open["permission_request"]
    assert Path(pr["canonical_path"]).resolve() == erp.resolve()
    assert Path(pr["exact_target_path"]).resolve() == erp.resolve()
    # 5. Folder opening uses list_directory, NEVER read_file
    assert pr["requested_operation"] == "list_directory"
    assert "list_directory" in pr["requested_operations"]
    assert "read_file" not in pr["requested_operations"]
    for op in ALL_OPS:
        if op not in pr["requested_operations"]:
            assert op in pr["not_requested_operations"]

    # Deny first request so clean for next test
    client.post(f"/api/v2/filesystem/requests/{pr['request_id']}/decide", json={"decision": "deny"}, headers=H)

    # 2. Registered project lookup: "Open my Jarvis project." resolves to myjarvis
    r_j = client.post("/api/chat", json={"message": "Open my Jarvis project."}, headers=H)
    assert r_j.status_code == 200
    dj = r_j.json()
    assert dj["command"] == "permission_required"
    assert Path(dj["permission_request"]["canonical_path"]).resolve() == jarvis_clone.resolve()
    assert "read_file" not in dj["permission_request"]["requested_operations"]


def test_workspace_root_search_and_multiple_matches_and_unknown_project(assistant, settings, tmp_path):
    ws1 = tmp_path / "ws_a"
    ws2 = tmp_path / "ws_b"
    erp_a = _make_erp_project(ws1, "ERP_V2")
    client = TestClient(create_app(settings, assistant=assistant))

    # Configure explicit workspace root without setting any alias
    r_pol = client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(ws1)]}, headers=H)
    assert r_pol.status_code == 200

    # Single workspace match works cleanly without any hardcoded Documents fallback
    r_find = client.post("/api/chat", json={"message": "Find my ERP V2 project."}, headers=H)
    assert r_find.status_code == 200
    df = r_find.json()
    assert df["command"] == "permission_required"
    assert Path(df["permission_request"]["canonical_path"]).resolve() == erp_a.resolve()
    client.post(
        f"/api/v2/filesystem/requests/{df['permission_request']['request_id']}/decide",
        json={"decision": "deny"},
        headers=H,
    )

    # Multiple matches across workspace roots -> asks user to clarify instead of picking arbitrarily
    erp_b = _make_erp_project(ws2, "ERP_V2")
    client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(ws1), str(ws2)]}, headers=H)
    r_multi = client.post("/api/chat", json={"message": "Open the ERP V2 folder."}, headers=H)
    assert r_multi.status_code == 200
    dm = r_multi.json()
    assert dm["command"] == "clarification_needed"
    assert str(erp_a.resolve()) in dm["answer"]
    assert str(erp_b.resolve()) in dm["answer"]

    # Unknown project name -> asks user instead of assuming Documents
    r_unk = client.post("/api/chat", json={"message": "Open the NonExistentOmega folder."}, headers=H)
    assert r_unk.status_code == 200
    du = r_unk.json()
    assert du["command"] == "clarification_needed"
    assert "Documents" not in du["answer"]


def test_folder_structure_file_reading_followup_and_allow_once_resumption(assistant, settings, tmp_path):
    erp = _make_erp_project(tmp_path, "ERP_V2")
    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)

    # 1. "Show me the folder structure of ERP V2." -> list_directory + read_metadata -> approve allow_once -> readable structure & consumes allow_once
    r_struct = client.post("/api/chat", json={"message": "Show me the folder structure of ERP V2."}, headers=H)
    d1 = r_struct.json()
    assert d1["command"] == "permission_required"
    req1 = d1["permission_request"]
    assert req1["requested_operation"] == "list_directory"
    assert "read_file" not in req1["requested_operations"]

    res1 = client.post(
        f"/api/v2/filesystem/requests/{req1['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res1["granted"] is True
    assert res1["execution_ok"] is True
    assert res1["task"]["status"] == "completed"
    assert "backend" in res1["reply"]
    assert "package.json" in res1["reply"]

    # allow_once MUST be consumed after task completion; next project-context follow-up still asks permission!
    # 2. Follow-up: "Read the package.json in that project." reuses active project (ERP_V2) and targets package.json with read_file!
    r_pkg = client.post("/api/chat", json={"message": "Read the package.json in that project."}, headers=H)
    d2 = r_pkg.json()
    assert d2["command"] == "permission_required"
    req2 = d2["permission_request"]
    assert Path(req2["canonical_path"]).resolve() == erp.resolve()
    assert Path(req2["exact_target_path"]).resolve() == (erp / "package.json").resolve()
    assert req2["requested_operation"] == "read_file"
    assert "read_file" in req2["requested_operations"]
    for wop in WRITE_OPS:
        assert wop not in req2["requested_operations"]
        assert wop in req2["not_requested_operations"]

    res2 = client.post(
        f"/api/v2/filesystem/requests/{req2['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res2["granted"] is True
    assert res2["execution_ok"] is True
    assert "erp-v2" in res2["reply"]
    assert "2.1.0" in res2["reply"]

    # 3. Follow-up subfolder search: "Find the backend folder inside ERP V2."
    r_sub = client.post("/api/chat", json={"message": "Find the backend folder inside ERP V2."}, headers=H)
    d3 = r_sub.json()
    assert d3["command"] == "permission_required"
    req3 = d3["permission_request"]
    res3 = client.post(
        f"/api/v2/filesystem/requests/{req3['request_id']}/decide",
        json={"decision": "allow_session", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res3["granted"] is True
    assert res3["execution_ok"] is True
    assert "backend" in res3["reply"]

    # 12. Write operation remains blocked even after session read permission!
    r_write = client.post("/api/chat", json={"message": f"Create a new file named hack.txt inside {erp}"}, headers=H)
    assert r_write.status_code == 200
    dw = r_write.json()
    assert dw["ok"] is False
    assert dw["command"] == "permission_required"
    assert "create_file" in dw["permission_request"]["requested_operations"]
    # Deny so no write occurs
    r_deny = client.post(
        f"/api/v2/filesystem/requests/{dw['permission_request']['request_id']}/decide",
        json={"decision": "deny"},
        headers=H,
    ).json()
    assert r_deny["granted"] is False
    assert not (erp / "hack.txt").exists()


def test_invalid_paths_and_disappeared_target_fail_accurately(assistant, settings, tmp_path):
    erp = _make_erp_project(tmp_path, "ERP_V2")
    vanished = tmp_path / "TempFolder"
    vanished.mkdir()
    (vanished / "info.txt").write_text("hello", encoding="utf-8")

    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)

    # Non-existent explicit path -> invalid_path immediately, no misleading permission card
    missing = tmp_path / "DoesNotExist_404"
    r_miss = client.post("/api/chat", json={"message": f"Open {missing}"}, headers=H)
    assert r_miss.status_code == 200
    dm = r_miss.json()
    assert dm["command"] == "invalid_path"
    assert dm["ok"] is False

    # Target exists at request time, then deleted BEFORE user clicks Allow -> granted=True, execution_ok=False, task=failed!
    r_van = client.post("/api/chat", json={"message": f"Open {vanished}"}, headers=H)
    dv = r_van.json()
    assert dv["command"] == "permission_required"
    req_v = dv["permission_request"]

    # Remove directory before owner approves
    (vanished / "info.txt").unlink()
    vanished.rmdir()

    res_v = client.post(
        f"/api/v2/filesystem/requests/{req_v['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res_v["granted"] is True
    assert res_v["execution_ok"] is False
    assert res_v["task"]["status"] == "failed"
    assert "failed" in res_v["reply"].lower()


def test_security_boundaries_and_explorer_approval_and_voice_parity(assistant, settings, tmp_path):
    erp = _make_erp_project(tmp_path, "ERP_V2")
    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)

    # 11a. Traversal blocked
    r_trav = client.post("/api/chat", json={"message": f"Read {erp}\\..\\..\\Windows\\System32\\drivers\\etc\\hosts"}, headers=H)
    assert r_trav.json()["command"] == "filesystem_blocked"

    # 11b. Sensitive .env file blocked (either by Layer 3 policy or FilesystemSecurity policy)
    r_env = client.post("/api/chat", json={"message": f"Read {erp / '.env'}"}, headers=H)
    assert r_env.json()["command"] in ("filesystem_blocked", "approval_required")
    assert "SECRET_KEY" not in r_env.text
    chk_env = asyncio.run(assistant.orchestrator.fs_access.authorize(str(erp / ".env"), "read_file"))
    assert chk_env.blocked is True

    # 11c. Explorer opening never launches silently without permission and ActionExecutor policy
    r_exp_unperm = client.post("/api/chat", json={"message": "Open the ERP V2 folder in Explorer."}, headers=H)
    de0 = r_exp_unperm.json()
    assert de0["command"] == "permission_required"
    assert de0["ok"] is False

    # Grant session read permission and enable OPEN confirmation -> Explorer still requires desktop confirmation
    client.post(
        f"/api/v2/filesystem/requests/{de0['permission_request']['request_id']}/decide",
        json={"decision": "allow_session", "decided_by": "owner", "execute_deferred": False},
        headers=H,
    )
    settings.actions.confirm_levels = ["OPEN", "MODIFY", "DESTRUCTIVE", "SYSTEM"]
    r_exp = client.post("/api/chat", json={"message": "Open the ERP V2 folder in Explorer."}, headers=H)
    de = r_exp.json()
    assert de["command"] == "open_app"
    assert de["ok"] is False
    assert "approval required" in de["answer"].lower()
    assert any(p.get("action") == "open_app" for p in assistant.actions.pending_approvals())

    # 10a. Voice vs typed routing parity
    voice_proj = _make_erp_project(tmp_path, "Voice_Demo")
    client.post("/api/v2/filesystem/aliases", json={"alias": "Voice_Demo", "folder_path": str(voice_proj)}, headers=H)
    assert asyncio.run(route_intent(assistant, "Open the Voice Demo folder.")) is None  # Does not bypass DecisionEngine

    typed_cmp = client.post("/api/chat", json={"message": "Open the Voice Demo folder."}, headers=H).json()
    assert typed_cmp["command"] == "permission_required"

    orig_transcribe = assistant.stt.transcribe
    try:
        assistant.stt.transcribe = lambda *args, **kwargs: {
            "text": "Open the Voice Demo folder.",
            "language": "en",
            "detected_language": "en",
            "language_probability": 0.96,
            "confidence": 0.94,
            "segments": [],
        }
        rv = client.post("/api/voice/command?speak=false", content=_dummy_wav_bytes(), headers={**H, "Content-Type": "audio/wav"})
        assert rv.status_code == 200
        dv = rv.json()
        assert dv["command"] == typed_cmp["command"] == "permission_required"
        assert Path(dv["permission_request"]["canonical_path"]).resolve() == Path(typed_cmp["permission_request"]["canonical_path"]).resolve() == voice_proj.resolve()
        assert dv["permission_request"]["requested_operation"] == typed_cmp["permission_request"]["requested_operation"] == "list_directory"

        # 10b. Low-confidence / unclear voice command asks for clarification cleanly without touching filesystem
        assistant.stt.transcribe = lambda *args, **kwargs: {
            "text": "open erp v2",
            "language": "en",
            "detected_language": "en",
            "language_probability": 0.4,
            "confidence": 0.22,
            "clarification_needed": True,
            "clarification_reason": "Low transcription confidence",
            "segments": [],
        }
        before_seq = assistant.bus._seq
        asyncio.run(
            assistant._voice_turn(
                {"text": "open the erp v2 folder", "confidence": 0.22, "clarification_needed": True, "clarification_reason": "Low confidence"},
            )
        )
        new_events = [ev for ev in assistant.bus._history if int(ev.get("seq") or 0) > before_seq]
        assert any(ev.get("type") == "clarification_required" for ev in new_events)
        assert not any(ev.get("type") == "permission_requested" for ev in new_events)
    finally:
        assistant.stt.transcribe = orig_transcribe
