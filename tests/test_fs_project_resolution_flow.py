"""Comprehensive tests for natural-language project resolution, permission flow, resumption, and voice/typed parity."""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import time
import uuid
import wave
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.assistant import Assistant
from jarvis.filesystem_access import ALL_OPS, WRITE_OPS
from jarvis.intent import route_intent
from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


@pytest.fixture
def ws_sandbox():
    """Create a transient non-temp workspace sandbox inside tests/ so temp-exclusion guards genuinely allow it."""
    root = Path(__file__).resolve().parent / f"_ws_sandbox_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def test_workspace_root_search_and_multiple_matches_and_unknown_project(assistant, settings, tmp_path, ws_sandbox):
    ws1 = ws_sandbox / "ws_a"
    ws2 = ws_sandbox / "ws_b"
    erp_a = _make_erp_project(ws1, "ERP_V2")
    client = TestClient(create_app(settings, assistant=assistant))

    # Reject temp/pytest directories in workspace_roots policy configuration (422)
    temp_ws = tmp_path / "temp_leak_ws"
    temp_ws.mkdir(parents=True, exist_ok=True)
    r_bad = client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(temp_ws)]}, headers=H)
    assert r_bad.status_code == 422

    # Configure explicit non-temp workspace root without setting any alias
    r_pol = client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(ws1)]}, headers=H)
    assert r_pol.status_code == 200

    # Even if a pytest/temp dir or node_modules copy is injected in-memory, discovery ignores it completely
    _make_erp_project(temp_ws, "ERP_V2")
    _make_erp_project(ws1 / "node_modules", "ERP_V2")
    settings.filesystem.workspace_roots = [str(temp_ws), str(ws1)]

    # Single workspace match works cleanly without ambiguity from temp/generated dirs or Documents fallback
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

    # Multiple matches across legitimate workspace roots -> numbered clarification in awaiting_clarification state
    erp_b = _make_erp_project(ws2, "ERP_V2")
    r_pol2 = client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(ws1), str(ws2)]}, headers=H)
    assert r_pol2.status_code == 200
    r_multi = client.post("/api/chat", json={"message": "Open the ERP V2 folder."}, headers=H)
    assert r_multi.status_code == 200
    dm = r_multi.json()
    assert dm["command"] == "clarification_needed"
    assert dm["task"]["status"] == "awaiting_clarification"
    assert dm["clarification"]["mode"] == "select_candidate"
    assert len(dm["candidates"]) == 2
    assert dm["candidates"][0]["index"] == 1
    assert dm["candidates"][1]["index"] == 2
    assert str(erp_a.resolve()) in dm["answer"]
    assert str(erp_b.resolve()) in dm["answer"]

    # Ambiguous name reply ("ERP_V2") re-prompts with numbered choices without approving or falling back to general_chat
    r_amb_reply = client.post("/api/chat", json={"message": "ERP_V2"}, headers=H)
    assert r_amb_reply.status_code == 200
    dar = r_amb_reply.json()
    assert dar["command"] == "clarification_needed"
    assert dar["task_id"] == dm["task_id"]
    assert dar["task"]["status"] == "awaiting_clarification"

    # Cancel clarification cleanly before testing unknown project
    r_can = client.post("/api/chat", json={"message": "cancel"}, headers=H)
    assert r_can.json()["command"] == "clarification_cancelled"

    # Unknown project name -> asks user cleanly (mode=supply_parent_dir) instead of assuming Documents
    r_unk = client.post("/api/chat", json={"message": "Open the NonExistentOmega folder."}, headers=H)
    assert r_unk.status_code == 200
    du = r_unk.json()
    assert du["command"] == "clarification_needed"
    assert du["clarification"]["mode"] == "supply_parent_dir"
    assert du["task"]["status"] == "awaiting_clarification"
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


def test_clarification_selection_replies_persistence_expiry_and_tool_contract(assistant, settings, tmp_path, ws_sandbox):
    """Covers ordinal/name/project/parent-dir replies, DB persistence, expiration, follow-ups, and tool contracts."""
    ws1 = ws_sandbox / "work"
    ws2 = ws_sandbox / "archive"
    erp_work = _make_erp_project(ws1, "ERP_V2")
    _make_erp_project(ws2, "ERP_V2_archive")
    jarvis_dir = _make_erp_project(ws_sandbox / "own", "myjarvis")

    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/projects", json={"name": "myjarvis", "root_path": str(jarvis_dir)}, headers=H)
    assert client.put("/api/v2/filesystem/policy", json={"workspace_roots": [str(ws1), str(ws2)]}, headers=H).status_code == 200

    # 1. Ambiguous prompt ("Open the ERP folder.") -> both ERP_V2 and ERP_V2_archive match -> reply "first one" restores SAME task_id & asks permission!
    d0 = client.post("/api/chat", json={"message": "Open the ERP folder."}, headers=H).json()
    assert d0["command"] == "clarification_needed"
    orig_tid = d0["task_id"]
    first_path = Path(d0["candidates"][0]["path"]).resolve()

    d_sel1 = client.post("/api/chat", json={"message": "first one"}, headers=H).json()
    assert d_sel1["command"] == "permission_required"
    assert d_sel1["task_id"] == orig_tid
    assert d_sel1["task"]["status"] == "awaiting_permission"
    assert Path(d_sel1["permission_request"]["canonical_path"]).resolve() == first_path
    assert "read_file" not in d_sel1["permission_request"]["requested_operations"]

    # Deny permission, start ambiguous prompt again, reply "2" / "second one"
    client.post(f"/api/v2/filesystem/requests/{d_sel1['permission_request']['request_id']}/decide", json={"decision": "deny"}, headers=H)
    d1 = client.post("/api/chat", json={"message": "Open the ERP folder."}, headers=H).json()
    assert d1["command"] == "clarification_needed"
    second_path = Path(d1["candidates"][1]["path"]).resolve()
    d_sel2 = client.post("/api/chat", json={"message": "second one"}, headers=H).json()
    assert d_sel2["command"] == "permission_required"
    assert Path(d_sel2["permission_request"]["canonical_path"]).resolve() == second_path
    client.post(f"/api/v2/filesystem/requests/{d_sel2['permission_request']['request_id']}/decide", json={"decision": "deny"}, headers=H)

    # 2. Unique candidate name ("ERP_V2") and alternate project ("My Jarvis project") both resolve cleanly
    d2 = client.post("/api/chat", json={"message": "Open the ERP folder."}, headers=H).json()
    assert d2["command"] == "clarification_needed"
    d_uniq = client.post("/api/chat", json={"message": "ERP_V2"}, headers=H).json()
    assert d_uniq["command"] == "permission_required"
    assert Path(d_uniq["permission_request"]["canonical_path"]).resolve() == erp_work.resolve()
    client.post(f"/api/v2/filesystem/requests/{d_uniq['permission_request']['request_id']}/decide", json={"decision": "deny"}, headers=H)

    d3 = client.post("/api/chat", json={"message": "Open the ERP folder."}, headers=H).json()
    assert d3["command"] == "clarification_needed"
    d_mj = client.post("/api/chat", json={"message": "My Jarvis project"}, headers=H).json()
    assert d_mj["command"] == "permission_required"
    assert Path(d_mj["permission_request"]["canonical_path"]).resolve() == jarvis_dir.resolve()
    client.post(f"/api/v2/filesystem/requests/{d_mj['permission_request']['request_id']}/decide", json={"decision": "deny"}, headers=H)

    # 3. Persistence across fresh Assistant instance + parent-directory reply ("ws_sandbox/projects") + execution + bare follow-ups
    proj_parent = ws_sandbox / "projects"
    erp_real = _make_erp_project(proj_parent, "ERP_Omega")
    d_miss = client.post("/api/chat", json={"message": "Open the ERP Omega folder."}, headers=H).json()
    assert d_miss["command"] == "clarification_needed"
    miss_tid = d_miss["task_id"]

    # Simulate process/assistant reload reading only from SQLite DB
    reloaded = Assistant(settings, data_dir=assistant.data_dir, brain=FakeBrain(settings))
    reloaded_client = TestClient(create_app(settings, assistant=reloaded))
    d_parent = reloaded_client.post("/api/chat", json={"message": str(proj_parent)}, headers=H).json()
    assert d_parent["command"] == "permission_required"
    assert d_parent["task_id"] == miss_tid
    assert Path(d_parent["permission_request"]["canonical_path"]).resolve() == erp_real.resolve()

    res_exec = reloaded_client.post(
        f"/api/v2/filesystem/requests/{d_parent['permission_request']['request_id']}/decide",
        json={"decision": "allow_session", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res_exec["granted"] is True
    assert res_exec["execution_ok"] is True
    assert res_exec["task"]["status"] == "completed"

    # Follow-up continuity on active project without repeating ERP_Omega
    d_f1 = reloaded_client.post("/api/chat", json={"message": "Show its folder structure."}, headers=H).json()
    assert d_f1["command"] == "filesystem_ops"
    assert "backend" in d_f1["answer"]

    # New operation (search_files) on active project still enforces least-privilege permission for search_files
    d_f2 = reloaded_client.post("/api/chat", json={"message": "Find the backend."}, headers=H).json()
    assert d_f2["command"] == "permission_required"
    assert Path(d_f2["permission_request"]["canonical_path"]).resolve() == erp_real.resolve()
    assert d_f2["permission_request"]["requested_operation"] == "search_files"
    res_f2 = reloaded_client.post(
        f"/api/v2/filesystem/requests/{d_f2['permission_request']['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res_f2["execution_ok"] is True
    assert "backend" in res_f2["reply"]

    # New operation (read_file) on active project targets ERP_Omega/package.json cleanly
    d_f3 = reloaded_client.post("/api/chat", json={"message": "Read package.json."}, headers=H).json()
    assert d_f3["command"] == "permission_required"
    assert Path(d_f3["permission_request"]["canonical_path"]).resolve() == erp_real.resolve()
    assert Path(d_f3["permission_request"]["exact_target_path"]).resolve() == (erp_real / "package.json").resolve()
    assert d_f3["permission_request"]["requested_operation"] == "read_file"
    res_f3 = reloaded_client.post(
        f"/api/v2/filesystem/requests/{d_f3['permission_request']['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    ).json()
    assert res_f3["execution_ok"] is True
    assert "erp-v2" in res_f3["reply"]

    # 4. Clarification expiration handling
    d_exp0 = reloaded_client.post("/api/chat", json={"message": "Open the GhostProj999 folder."}, headers=H).json()
    assert d_exp0["command"] == "clarification_needed"
    reloaded.pending_clarification["expires_at"] = time.time() - 30.0
    d_exp1 = reloaded_client.post("/api/chat", json={"message": "1"}, headers=H).json()
    assert d_exp1["command"] == "clarification_expired"
    assert d_exp1["ok"] is False
    assert d_exp1["task"]["status"] == "failed"

    # 5. Tool contract rejects unresolved relative project names BEFORE calling fs_access.authorize
    tool_raw = asyncio.run(
        reloaded._execute_fs_tool("fs_list_directory", {"path": "UnresolvedRelName_XYZ"})
    )
    tool_parsed = json.loads(tool_raw)
    assert tool_parsed["ok"] is False
    assert tool_parsed["outcome"] == "not_found"


def test_real_repo_settings_file_never_modified_by_tests():
    """Verify data/settings.json stays clean and isolated during pytest."""
    repo_settings = Path(__file__).resolve().parent.parent / "data" / "settings.json"
    if repo_settings.exists():
        raw = json.loads(repo_settings.read_text(encoding="utf-8"))
        ws = (raw.get("filesystem") or {}).get("workspace_roots") or []
        assert not any("pytest-of-" in str(p).lower() or "\\temp\\" in str(p).lower() for p in ws)
