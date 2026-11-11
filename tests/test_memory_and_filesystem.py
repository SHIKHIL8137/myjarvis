"""Tests for 6-category PersonalMemoryStore, >200-turn disk history search, SafeFilesystem, and ActionExecutor tiers."""
from pathlib import Path
import time
import pytest
from starlette.testclient import TestClient

from jarvis.actions import ActionExecutor, ActionPermissionError, PermissionLevel
from jarvis.api import create_app
from jarvis.config import ActionsSpec, FilesystemSpec, MemorySpec
from jarvis.filesystem import FilesystemPermissionError, SafeFilesystem
from jarvis.history import History
from jarvis.memory_store import PersonalMemoryStore, infer_memory_category

H = {"X-Jarvis": "1"}


def test_personal_memory_categories_duplicates_secrets_backup_restore(tmp_path):
    store = PersonalMemoryStore(tmp_path / "mem.jsonl", MemorySpec(backup_dir=str(tmp_path / "backups")))

    r1, created = store.add("My preferred editor is Neovim", category="preference")
    assert created and r1.category == "preference"

    # Duplicate near-identical preference is deduplicated
    r_dup, created_dup = store.add("My preferred editor is Neovim", category="preference")
    assert not created_dup and r_dup.id == r1.id

    r2, _ = store.add("Project myjarvis runs on port 4700", category="project")
    r3, _ = store.add("Today remind me to review logs", category="temporary")
    r4, _ = store.add("My desk stands in room 204", category="explicit")
    r5, _ = store.add("Keep responses concise during voice mode", category="feedback")
    assert len(store.list_all()) == 5
    cat, _ = infer_memory_category("I always prefer dark theme")
    assert cat == "preference"

    # Secret rejection
    with pytest.raises(ValueError, match="sensitive credential"):
        store.add("My OpenAI key is sk-testsecret1234567890abcdef1234567890")

    # Pending inferred memory confirmation flow
    pend = store.propose_inferred("User prefers Python type hints", category="preference", confidence=0.82)
    assert not pend.confirmed
    assert all(r.id != pend.id for r in store.list_all(include_pending=False))
    confirmed = store.confirm_pending(pend.id, approved=True)
    assert confirmed.confirmed

    # Update, search, export, backup, delete, restore
    updated = store.update(r4.id, "My studio desk stands in room 308")
    assert "308" in updated.text
    hits = store.search("room 308", category="explicit")
    assert hits and hits[0][0].id == r4.id

    exp = store.export_data()
    assert exp["total"] >= 6
    bak = store.backup()
    assert bak.exists()

    removed_tmp = store.clear_temporary()
    assert removed_tmp == 1
    store.delete(r1.id)
    assert all(r.id != r1.id for r in store.list_all())

    restored = store.restore(bak)
    assert restored >= 6
    assert any(r.id == r1.id for r in store.list_all())


def test_history_searches_disk_beyond_ram_limit(tmp_path):
    h = History(tmp_path / "history.jsonl", turns=4)
    h.add("user", "Unique ancient anchor: xylophone telemetry pipeline")
    h.add("assistant", "Recorded ancient anchor.")
    for i in range(410):
        h.add("user", f"filler question number {i}")
    # First turn was evicted from in-memory slice when it crossed 400 -> 200, yet searchable on disk
    assert len(h.items) <= 215 and all("xylophone telemetry" not in m["content"] for m in h.items)
    hits = h.search_disk("xylophone telemetry", days=1, limit=5)
    assert hits and "xylophone telemetry" in hits[0]["content"]


def test_filesystem_search_read_inspect_and_security_boundaries(tmp_path):
    root = tmp_path / "workspace"
    sub = root / "src"
    sub.mkdir(parents=True)
    (sub / "app.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo Project\nUses FastAPI.", encoding="utf-8")
    (root / ".env").write_text("SECRET_TOKEN=supersecret\n", encoding="utf-8")
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("internal git config", encoding="utf-8")

    outside = tmp_path / "outside.txt"
    outside.write_text("top secret outside root", encoding="utf-8")

    spec = FilesystemSpec(allowed_roots=[str(root)], allow_write=True)
    fs = SafeFilesystem(spec, default_base=tmp_path)

    # Filename, extension, folder, and content searches
    assert any(m["name"] == "app.py" for m in fs.search("app"))
    assert any(m["name"] == "app.py" for m in fs.search("", ext=".py"))
    assert any(m["name"] == "src" for m in fs.search("src", folders_only=True))
    assert any(m["name"] == "README.md" for m in fs.search("", content_query="FastAPI"))

    # Restricted files & outside-root paths are hidden and blocked
    assert not any(m["name"] == ".env" for m in fs.search(".env"))
    assert not any(m["name"] == "config" for m in fs.search("config"))

    with pytest.raises(FilesystemPermissionError):
        fs.read_document(str(outside))
    with pytest.raises(FilesystemPermissionError):
        fs.read_document(str(root / ".env"))

    doc = fs.read_document(str(root / "README.md"))
    assert "Demo Project" in doc["content"]
    info = fs.inspect_project(str(root))
    assert info["total_files"] == 2 and ".py" in info["languages"]

    # Create, modify, move, soft-delete to .trash
    created = fs.create_or_modify(str(root / "notes_new.md"), "# Created\nLine 1")
    assert Path(created["path"]).exists()
    moved = fs.move_file(created["path"], str(root / "notes_renamed.md"))
    assert Path(moved["to"]).exists()
    deleted = fs.delete_file(moved["to"])
    assert ".trash" in deleted["moved_to"] and not Path(moved["to"]).exists()


def test_action_permission_tiers_and_confirmation_flow(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("alpha", encoding="utf-8")

    fs = SafeFilesystem(FilesystemSpec(allowed_roots=[str(root)], allow_write=True), default_base=tmp_path)
    act = ActionExecutor(ActionsSpec(), fs)

    # READ tier executes immediately
    r = act.request_or_execute("read_file", {"path": str(root / "a.md")})
    assert r["result"]["content"] == "alpha" and r["level"] == PermissionLevel.READ.value

    # MODIFY tier requires confirmation token
    req = act.request_or_execute("create_file", {"path": str(root / "b.md"), "content": "beta"})
    assert req["confirmation_required"] is True and not (root / "b.md").exists()
    done = act.confirm(req["token"])
    assert (root / "b.md").read_text(encoding="utf-8") == "beta" and done["level"] == PermissionLevel.MODIFY.value

    # Disallowed apps and arbitrary shell commands are rejected
    with pytest.raises(ActionPermissionError):
        act.request_or_execute("open_app", {"app": "arbitrary-malware-app"}, preapproved=True)
    with pytest.raises(ActionPermissionError):
        act.request_or_execute("run_command", {"command": "rm -rf /"}, preapproved=True)


def test_workspace_api_memory_files_actions(settings, assistant, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "spec.md").write_text("Architecture Notes\nUses SQLite.", encoding="utf-8")
    settings.filesystem.allowed_roots = [str(root)]

    with TestClient(create_app(settings, assistant)) as c:
        m = c.post("/api/memory/items", json={"text": "Always use pathlib cleanly"}, headers=H)
        assert m.status_code == 200
        rid = m.json()["record"]["id"]
        assert len(c.get("/api/memory/items", params={"q": "pathlib"}).json()["items"]) >= 1
        assert c.put(f"/api/memory/items/{rid}", json={"text": "Always prefer pathlib and pydantic"}, headers=H).status_code == 200
        assert c.post("/api/memory/backup", headers=H).json()["ok"] is True

        f = c.get("/api/files/search", params={"q": "spec"}).json()
        assert f["matches"] and f["matches"][0]["name"] == "spec.md"
        w = c.post("/api/files/write", json={"path": str(root / "out.txt"), "content": "hello"}, headers=H).json()
        assert w["confirmation_required"] is True
        conf = c.post(f"/api/actions/{w['token']}/confirm", headers=H).json()
        assert (root / "out.txt").read_text(encoding="utf-8") == "hello"
        assert conf["status"] == "completed" and c.get("/api/tasks").json()["tasks"][0]["status"] == "completed"
