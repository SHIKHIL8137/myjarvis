"""P10.20 End-to-End Integration, Filesystem Security, Consolidated Settings & Model Routing Suite.

Covers Scenarios A through H:
- Scenario A: Unauthorized host folder search -> inline permission -> allow_once -> execute -> graph -> consume
- Scenario B: Immediate follow-up after allow_once re-prompts instead of reusing prior grant
- Scenario C: Owner denial -> cancelled task -> zero file reads -> Access Denied reply
- Scenario D: Strict Read vs Write separation + always_read_only rejection for mutating ops + real report file creation
- Scenario E: Attack & boundary suite (.., UNC, drive root, system dirs, SSH/browser profiles, symlinks, .env, non-owner, audit log)
- Scenario F: Consolidated Settings, plaintext key rejection, DPAPI save/delete, custom OpenAI-compat routing, health, grant/update/revoke
- Scenario G: Centralized manager 'Check all pending tasks' -> accurate real task inventory
- Scenario H: External 'ERP' project review gated by permission -> allow_once -> AST/test review -> task metadata -> consume once
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from jarvis import config as cfg_mod
from jarvis.api import create_app
import jarvis.filesystem_access as fs_mod
from jarvis.filesystem_access import READ_OPS, WRITE_OPS
from jarvis.gateway.providers import OpenAICompatAdapter

H = {"X-Jarvis": "1"}


def _setup_home_folders(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "user_home"
    docs = home / "Documents"
    downloads = home / "Downloads"
    docs.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    (docs / "invoice_2026_01.txt").write_text("Invoice #101 - Total $450.00\n", encoding="utf-8")
    (docs / "invoice_feb.pdf").write_bytes(b"%PDF-1.4\n% Invoice Feb 2026\n")
    (docs / "notes.md").write_text("# Personal Notes\nNothing sensitive here.\n", encoding="utf-8")
    (docs / ".env").write_text("SECRET_TOKEN=super-secret-value\n", encoding="utf-8")
    (downloads / "setup_guide.pdf").write_bytes(b"%PDF-1.4\n% Guide\n")
    (downloads / "draft_report.txt").write_text("Downloaded draft\n", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home, docs, downloads


def _wait_task(client: TestClient, task_id: str) -> dict:
    r = client.get(f"/api/v2/tasks/{task_id}")
    assert r.status_code == 200
    return r.json()


def test_scenario_a_b_c_host_folder_permission_lifecycle(assistant, settings, tmp_path, monkeypatch):
    _, docs, downloads = _setup_home_folders(tmp_path, monkeypatch)
    client = TestClient(create_app(settings, assistant=assistant))

    # --- Scenario A: Unauthorized search -> inline permission -> allow_once -> execute -> consume ---
    r_a = client.post("/api/chat", json={"message": "Search my Documents folder for invoices"}, headers=H)
    assert r_a.status_code == 200
    da = r_a.json()
    assert da["command"] == "permission_required"
    req_a = da.get("permission_request")
    task_a = da.get("task")
    assert req_a and req_a["status"] == "pending"
    assert Path(req_a["canonical_path"]).resolve() == docs.resolve()
    assert set(req_a["requested_operations"]) <= set(READ_OPS)
    for wop in WRITE_OPS + ("execute_in_directory", "delete_file"):
        assert wop in req_a["not_requested_operations"]
    assert task_a and task_a["status"] == "awaiting_permission"

    # Nothing leaked before approval
    assert "invoice_2026_01" not in da["answer"]

    # Owner approves allow_once
    r_dec_a = client.post(
        f"/api/v2/filesystem/requests/{req_a['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_dec_a.status_code == 200
    dec_a = r_dec_a.json()
    assert dec_a["granted"] is True
    assert "invoice_2026_01.txt" in dec_a["reply"]
    assert "invoice_feb.pdf" in dec_a["reply"]
    assert ".env" not in dec_a["reply"]  # Sensitive file stays hidden even when folder is approved!
    assert dec_a["task"]["status"] == "completed"

    # Once permission must already be consumed!
    perms_after_a = client.get("/api/v2/filesystem/permissions").json()["permissions"]
    perm_row = next(p for p in perms_after_a if p["permission_id"] == dec_a["permission"]["permission_id"])
    assert perm_row["status"] == "consumed"

    # Real graph node updated for the completed task
    snap = client.get("/api/v2/graph").json()
    node_ids = {n["id"] for n in snap["nodes"]}
    assert f"task:{task_a['task_id']}" in node_ids

    # --- Scenario B: Follow-up after Allow Once MUST NOT silently reuse the prior grant ---
    r_b1 = client.post("/api/chat", json={"message": "Find all PDF documents"}, headers=H)
    assert r_b1.status_code == 200
    db1 = r_b1.json()
    assert "invoice_feb.pdf" not in db1["answer"]
    assert db1.get("command") in ("permission_required", "clarification_needed") or "specify which folder" in db1["answer"].lower()

    r_b2 = client.post("/api/chat", json={"message": "Find all PDF documents in my Documents folder"}, headers=H)
    assert r_b2.status_code == 200
    db2 = r_b2.json()
    assert db2["command"] == "permission_required"
    assert db2["permission_request"]["request_id"] != req_a["request_id"]
    assert "invoice_feb.pdf" not in db2["answer"]

    # --- Scenario C: Deny Request cancels task & reads zero files ---
    r_c = client.post("/api/chat", json={"message": "Find documents in my Downloads folder"}, headers=H)
    assert r_c.status_code == 200
    dc = r_c.json()
    assert dc["command"] == "permission_required"
    req_c = dc["permission_request"]
    assert Path(req_c["canonical_path"]).resolve() == downloads.resolve()

    r_dec_c = client.post(
        f"/api/v2/filesystem/requests/{req_c['request_id']}/decide",
        json={"decision": "deny", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_dec_c.status_code == 200
    dec_c = r_dec_c.json()
    assert dec_c["granted"] is False
    assert "denied" in dec_c["reply"].lower()
    assert "setup_guide.pdf" not in dec_c["reply"]
    assert dec_c["task"]["status"] == "cancelled"


def test_scenario_d_read_write_separation_and_always_read_only_guard(assistant, settings, tmp_path, monkeypatch):
    _, docs, _ = _setup_home_folders(tmp_path, monkeypatch)
    client = TestClient(create_app(settings, assistant=assistant))

    # Ask to list Documents -> grant Always allow (read-only)
    r_read_req = client.post("/api/chat", json={"message": "List files in my Documents folder"}, headers=H)
    req_r = r_read_req.json()["permission_request"]
    r_grant_ro = client.post(
        f"/api/v2/filesystem/requests/{req_r['request_id']}/decide",
        json={"decision": "always_read_only", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_grant_ro.status_code == 200
    grant_ro = r_grant_ro.json()
    assert grant_ro["granted"] is True
    assert set(grant_ro["permission"]["granted_operations"]) <= set(READ_OPS)
    assert grant_ro["permission"]["scope"] == "persistent"

    # Follow-up read works without re-prompting
    r_read2 = client.post("/api/chat", json={"message": "Search my Documents folder for notes"}, headers=H)
    assert r_read2.status_code == 200
    assert r_read2.json()["command"] == "filesystem_ops"
    assert "notes.md" in r_read2.json()["answer"]

    # Now ask to CREATE a report in Documents -> read permission MUST NOT allow write!
    r_write_req = client.post("/api/chat", json={"message": "Create a summary report in my Documents folder"}, headers=H)
    assert r_write_req.status_code == 200
    dw = r_write_req.json()
    assert dw["command"] == "permission_required"
    req_w = dw["permission_request"]
    assert any(op in WRITE_OPS for op in req_w["requested_operations"])
    assert "delete_file" in req_w["not_requested_operations"]

    # Attempting to click/call always_read_only on a write request MUST be blocked with 403!
    r_bad_ro = client.post(
        f"/api/v2/filesystem/requests/{req_w['request_id']}/decide",
        json={"decision": "always_read_only", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_bad_ro.status_code == 403
    assert "read_only" in r_bad_ro.text.lower() or "write" in r_bad_ro.text.lower()

    # Approving write_once actually creates the real file & records generated_files on the task!
    r_good_w = client.post(
        f"/api/v2/filesystem/requests/{req_w['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_good_w.status_code == 200
    gw = r_good_w.json()
    assert gw["granted"] is True
    assert gw["task"]["status"] == "completed"
    assert gw["paths"]
    created = Path(gw["paths"][0])
    assert created.exists() and created.parent.resolve() == docs.resolve()
    assert str(created) in gw["task"]["generated_files"]

    # Subsequent delete or write STILL blocked without its own explicit grant
    chk_del = asyncio.run(assistant.orchestrator.fs_access.authorize(created, "delete_file"))
    assert chk_del.allowed is False
    chk_write_again = asyncio.run(assistant.orchestrator.fs_access.authorize(created, "write_file"))
    assert chk_write_again.allowed is False


def test_scenario_e_security_boundaries_attacks_and_audit_log(assistant, settings, tmp_path, monkeypatch):
    home, docs, downloads = _setup_home_folders(tmp_path, monkeypatch)
    fs = assistant.orchestrator.fs_access
    client = TestClient(create_app(settings, assistant=assistant))

    # Grant full read/write on docs ONLY to test escape, sensitive files, extensions & sibling walls
    asyncio.run(fs.grant_direct(docs, ["read_file", "list_directory", "search_files", "write_file", "create_file"], granted_by="owner"))

    # 1. Path traversal '..'
    r1 = asyncio.run(fs.authorize(f"{docs}\\..\\Downloads\\draft_report.txt", "read_file"))
    assert r1.allowed is False and r1.blocked is True and r1.code == "traversal_blocked"

    # 2. UNC / Windows device paths
    r2 = asyncio.run(fs.authorize("\\\\evil-server\\share\\secrets.txt", "read_file"))
    assert r2.allowed is False and r2.blocked is True and r2.code == "unc_or_device_path"
    r2b = asyncio.run(fs.authorize("\\\\.\\PhysicalDrive0", "read_file"))
    assert r2b.allowed is False and r2b.blocked is True

    # 3. Drive / FS root
    r3 = asyncio.run(fs.authorize("C:\\", "list_directory"))
    assert r3.allowed is False and r3.blocked is True and r3.code == "drive_root_blocked"

    # 4. Protected OS directories & SSH / browser credential directories
    for protected in (
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "/etc/shadow",
        str(home / ".ssh" / "id_rsa"),
        str(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Login Data"),
    ):
        res = asyncio.run(fs.authorize(protected, "read_file"))
        assert res.allowed is False and res.blocked is True

    # 5. Sensitive files INSIDE authorized folder (.env, id_ed25519, secrets.json)
    (docs / "secrets.json").write_text('{"api_key":"secret"}', encoding="utf-8")
    for sens in (docs / ".env", docs / "secrets.json", docs / "id_ed25519"):
        res_sens = asyncio.run(fs.authorize(sens, "read_file"))
        assert res_sens.allowed is False and res_sens.blocked is True and res_sens.code == "sensitive_file_blocked"

    # 6. Blocked executable write extensions
    res_exe = asyncio.run(fs.authorize(docs / "dropper.exe", "create_file"))
    assert res_exe.allowed is False and res_exe.blocked is True and res_exe.code == "blocked_extension"

    # 7. Sibling and parent boundary enforcement
    res_sib = asyncio.run(fs.authorize(downloads / "draft_report.txt", "read_file"))
    assert res_sib.allowed is False and res_sib.blocked is False and res_sib.code == "permission_required"
    res_parent = asyncio.run(fs.authorize(home, "list_directory"))
    assert res_parent.allowed is False

    # 8. Symlink / reparse escape inside authorized folder
    outside_secret = downloads / "outside_secret.txt"
    escape_link = docs / "escape_link.txt"
    symlink_created = False
    try:
        os.symlink(outside_secret, escape_link)
        symlink_created = True
    except OSError:
        # Windows without Developer Mode/Admin: simulate reparse detection & realpath resolution cleanly
        escape_link.write_text("placeholder", encoding="utf-8")
        monkeypatch.setattr(fs_mod, "_has_reparse_or_symlink", lambda p: Path(p).name == "escape_link.txt")
    res_sym = asyncio.run(fs.authorize(escape_link, "read_file"))
    assert res_sym.allowed is False and res_sym.blocked is True
    assert res_sym.code in ("symlink_blocked", "symlink_escape_blocked")
    if symlink_created or escape_link.exists():
        escape_link.unlink()

    # 9. Non-owner self-approval rejection via API & direct manager
    req = asyncio.run(fs.create_permission_request(downloads, ["list_directory"], reason="test non-owner"))
    r_non_owner = client.post(
        f"/api/v2/filesystem/requests/{req['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "llm", "execute_deferred": False},
        headers=H,
    )
    assert r_non_owner.status_code == 403
    assert "owner" in r_non_owner.text.lower()

    # 10. Audit log recorded across DB for allowed, blocked, denied, rejected_non_owner
    audit = client.get("/api/v2/filesystem/audit?limit=50").json()["events"]
    decisions = {ev["decision"] for ev in audit}
    assert "blocked" in decisions
    assert "denied_ungranted" in decisions
    assert "rejected_non_owner" in decisions

    # 11. Filesystem audit logging must not pollute socket audit buffer read by /api/drill/connections
    r_drill_conn = client.get("/api/drill/connections?since=0")
    assert r_drill_conn.status_code == 200 and "all" in r_drill_conn.json()


def test_scenario_f_settings_dpapi_custom_provider_and_permissions_api(assistant, settings, tmp_path, monkeypatch):
    _, docs, _ = _setup_home_folders(tmp_path, monkeypatch)
    client = TestClient(create_app(settings, assistant=assistant))

    # Avoid mutating real host secrets.dpapi or data/settings.json during tests
    dpapi_file = tmp_path / "test_secrets.json"
    overrides_file = tmp_path / "test_settings.json"
    monkeypatch.setattr(cfg_mod, "SECRETS_PATH", dpapi_file)
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda p: cfg_mod.save_overrides(p, overrides_path=overrides_file))

    # 1. GET /api/v2/settings returns all 4 consolidated control plane groups
    r_get = client.get("/api/v2/settings")
    assert r_get.status_code == 200
    s_data = r_get.json()
    for section in ("providers", "provider_keys", "gateway", "tts", "wake", "audio", "stt", "filesystem", "preferences"):
        assert section in s_data
    assert "custom" in s_data["provider_keys"]

    # 2. Reject plaintext api_key inside settings JSON
    r_plain = client.put("/api/v2/settings", json={"providers": {"api_key": "sk-plaintext-leak"}}, headers=H)
    assert r_plain.status_code == 422

    # 3. Save & Delete DPAPI key via /v2/gateway/providers/{provider}/key without returning raw secret in GET settings
    r_save_key = client.post("/api/v2/gateway/providers/custom/key", json={"api_key": "sk-custom-test-key-12345"}, headers=H)
    assert r_save_key.status_code == 200 and r_save_key.json()["configured"] is True

    s_after_key = client.get("/api/v2/settings").json()
    assert s_after_key["provider_keys"]["custom"] is True
    assert "sk-custom-test-key-12345" not in json.dumps(s_after_key)

    # 4. Update role routing, custom base URL/model & theme preferences
    r_put = client.put(
        "/api/v2/settings",
        json={
            "providers": {
                "active_provider": "custom",
                "fast_model": "custom:fast-edge-1b",
                "advanced_model": "custom:reasoning-32b",
                "coding_model": "custom:coder-14b",
                "embedding_model": "nomic-embed-text",
                "fallback_provider": "ollama",
                "custom_base_url": "https://custom.local/v1",
                "custom_model": "custom-model-v1",
            },
            "preferences": {"theme": "midnight-obsidian", "compact_mode": True, "reduced_motion": True},
        },
        headers=H,
    )
    assert r_put.status_code == 200
    upd = r_put.json()["settings"]
    assert upd["providers"]["custom_base_url"] == "https://custom.local/v1"
    assert upd["providers"]["coding_model"] == "custom:coder-14b"
    assert upd["preferences"]["theme"] == "midnight-obsidian"
    assert upd["preferences"]["compact_mode"] is True

    # 4b. Local Ollama health check invokes Brain.health() cleanly without expecting nonexistent .ollama attr
    async def _fake_ollama_health():
        return {"up": True, "version": "0.40.16", "models": ["qwen3:4b-instruct"], "loaded": ["qwen3:4b-instruct"]}

    monkeypatch.setattr(assistant.brain, "health", _fake_ollama_health, raising=False)
    r_ollama_h = client.post("/api/v2/gateway/providers/ollama/health", headers=H)
    assert r_ollama_h.status_code == 200 and r_ollama_h.json()["ok"] is True
    assert r_ollama_h.json()["models_count"] == 1

    # 5. Offline security gate blocks outbound cloud health check until online + allow_cloud enabled
    r_health_off = client.post("/api/v2/gateway/providers/custom/health", headers=H)
    assert r_health_off.status_code == 200 and r_health_off.json()["ok"] is False
    assert r_health_off.json().get("category") == "offline_or_cloud_disabled"

    # Enable Online + allow_cloud explicitly and verify mocked provider health & role routing
    r_online = client.put(
        "/api/v2/settings",
        json={"offline_enforced": False, "internet": {"enabled": True}, "providers": {"allow_cloud": True}},
        headers=H,
    )
    assert r_online.status_code == 200

    async def mock_health(self, api_key=None, transport_factory=None):
        return {"ok": True, "provider": self.name, "latency_ms": 12.5, "detail": "mocked /models ok"}

    monkeypatch.setattr(OpenAICompatAdapter, "health_check", mock_health)
    r_health = client.post("/api/v2/gateway/providers/custom/health", headers=H)
    assert r_health.status_code == 200 and r_health.json()["ok"] is True

    r_route = client.post(
        "/api/v2/gateway/route-test",
        json={"role": "coding", "require_tools": True, "project_privacy_level": "public"},
        headers=H,
    )
    assert r_route.status_code == 200
    rt = r_route.json()
    assert rt["primary"] == "custom:coder-14b"
    assert rt["selected"] == "custom:coder-14b"
    assert rt["role"] == "coding"

    # Anthropic adapter construction & health_check availability verification
    ant = assistant.orchestrator.gateway._build_cloud_adapter("anthropic")
    assert ant is not None and hasattr(ant, "health_check")

    # Delete provider key clears configured flag
    r_del_key = client.delete("/api/v2/gateway/providers/custom/key", headers=H)
    assert r_del_key.status_code == 200 and r_del_key.json()["configured"] is False

    # 6. Direct permission grant, update & revoke API
    r_grant = client.post(
        "/api/v2/filesystem/permissions",
        json={"folder_path": str(docs), "operations": ["list_directory", "read_file"], "scope": "persistent"},
        headers=H,
    )
    assert r_grant.status_code == 200
    pid = r_grant.json()["permission"]["permission_id"]

    r_upd = client.put(f"/api/v2/filesystem/permissions/{pid}", json={"granted_operations": ["list_directory", "read_file", "search_files"]}, headers=H)
    assert r_upd.status_code == 200
    assert "search_files" in r_upd.json()["permission"]["granted_operations"]

    r_rev = client.post(f"/api/v2/filesystem/permissions/{pid}/revoke", headers=H)
    assert r_rev.status_code == 200 and r_rev.json()["permission"]["status"] == "revoked"
    chk_after_rev = asyncio.run(assistant.orchestrator.fs_access.authorize(docs, "read_file"))
    assert chk_after_rev.allowed is False


def test_scenario_g_central_task_status_and_cancellation(assistant, settings, tmp_path, monkeypatch):
    _setup_home_folders(tmp_path, monkeypatch)
    client = TestClient(create_app(settings, assistant=assistant))

    # Create a real pending task via ungranted Downloads access
    r1 = client.post("/api/chat", json={"message": "Search my Downloads folder for reports"}, headers=H)
    assert r1.status_code == 200
    tid = r1.json()["task"]["task_id"]

    # Scenario G: 'Check all pending tasks' -> accurate centralized status list
    r_status = client.post("/api/chat", json={"message": "Check all pending tasks"}, headers=H)
    assert r_status.status_code == 200
    ds = r_status.json()
    assert ds["command"] == "task_status"
    assert tid in ds["answer"] or "awaiting_permission" in ds["answer"]

    # Cancel task via API
    r_cancel = client.post(f"/api/v2/tasks/{tid}/cancel", headers=H)
    assert r_cancel.status_code == 200 and r_cancel.json()["task"]["status"] == "cancelled"


def test_scenario_h_erp_external_project_permission_gated_review(assistant, settings, tmp_path, monkeypatch):
    _setup_home_folders(tmp_path, monkeypatch)
    erp_dir = tmp_path / "external_projects" / "ERP"
    erp_dir.mkdir(parents=True, exist_ok=True)
    (erp_dir / "ledger.py").write_text(
        "def post_entry(amount):\n    eval('1 + 1')  # potential smell\n    return round(float(amount), 2)\n",
        encoding="utf-8",
    )
    (erp_dir / "test_ledger.py").write_text("from ledger import post_entry\n\ndef test_post():\n    assert post_entry(10) == 10.0\n", encoding="utf-8")

    assistant.orchestrator.projects.register(name="ERP", root_path=str(erp_dir), default_branch="main", test_cmd="pytest -q")
    client = TestClient(create_app(settings, assistant=assistant))

    # Step 1: Ask to review ERP project -> must request read permission first (zero silent reading!)
    r_erp = client.post("/api/chat", json={"message": "Review my ERP project and check its code and tests"}, headers=H)
    assert r_erp.status_code == 200
    de = r_erp.json()
    assert de["command"] == "permission_required"
    req_e = de["permission_request"]
    task_e = de["task"]
    assert Path(req_e["canonical_path"]).resolve() == erp_dir.resolve()
    assert task_e["status"] == "awaiting_permission"

    # Step 2: Approve allow_once -> runs AST code review & test inventory, updates task metadata, updates graph, consumes grant
    r_dec_e = client.post(
        f"/api/v2/filesystem/requests/{req_e['request_id']}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r_dec_e.status_code == 200
    res_e = r_dec_e.json()
    assert res_e["granted"] is True
    assert "ERP" in res_e["reply"]
    assert "ledger.py" in res_e["reply"]
    assert "test_ledger.py" in res_e["reply"]
    done_task = res_e["task"]
    assert done_task["status"] == "completed"
    assert done_task["plan"]
    assert "reviewer" in done_task["selected_agents"]

    # Once permission on ERP consumed after completion!
    perms = client.get("/api/v2/filesystem/permissions").json()["permissions"]
    erp_perm = next(p for p in perms if p["permission_id"] == res_e["permission"]["permission_id"])
    assert erp_perm["status"] == "consumed"


def test_p10_20_consolidated_frontend_dist_served(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "<title>J.A.R.V.I.S" in html or '<div id="root">' in html
    # Built asset referenced in index.html must load cleanly and reflect 2-destination shell
    import re
    m = re.search(r'src="(/assets/[^"]+\.js)"', html)
    assert m, f"Expected Vite built JS bundle in index.html, got: {html}"
    r_js = client.get(m.group(1))
    assert r_js.status_code == 200
    js = r_js.text
    assert "JARVIS ASSISTANT" in js
    assert "SETTINGS" in js
    assert "Secondary Modules" not in js


def test_p10_20_settings_devices_inventory_and_output_device_patch(assistant, settings, monkeypatch):
    client = TestClient(create_app(settings, assistant=assistant))

    # Normal device inventory exposes structured inputs/outputs cleanly
    r = client.get("/api/v2/settings/devices")
    assert r.status_code == 200
    body = r.json()
    assert "audio" in body
    assert isinstance(body["audio"].get("inputs"), list)
    assert isinstance(body["audio"].get("outputs"), list)
    assert "available" in body["audio"]

    # Handles sounddevice errors cleanly without failing Settings load
    import sounddevice as sd
    monkeypatch.setattr(sd, "query_devices", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no host audio")))
    r_err = client.get("/api/v2/settings/devices")
    assert r_err.status_code == 200
    err_audio = r_err.json()["audio"]
    assert err_audio["inputs"] == []
    assert err_audio["outputs"] == []
    assert err_audio["available"] is False
    assert "no host audio" in (err_audio.get("error") or "")

    # Updating Speaker Output Device persists on tts.output_device
    r_put = client.put("/api/v2/settings", json={"tts": {"output_device": 2}}, headers=H)
    assert r_put.status_code == 200
    assert r_put.json()["settings"]["tts"]["output_device"] == 2

