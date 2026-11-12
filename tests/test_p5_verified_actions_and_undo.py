"""P5 verification tests: Action execution lifecycle, verified outcome contracts, honest input verification, SafeFilesystem bypass fix, and UndoService rollback with conflict/expiry checks."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect

from jarvis.actions import ActionExecutor
from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.api_v2 import create_v2_router
from jarvis.config import ActionsSpec, BrainSpec, Settings
from jarvis.core.errors import UndoConflictError
from jarvis.db.engine import DatabaseManager
from jarvis.filesystem import SafeFilesystem
from jarvis.undo import UndoService


def _alembic_cfg(sync_url: str) -> Config:
    cfg = Config("d:/projects/myjarvis/alembic.ini")
    cfg.set_main_option("script_location", "d:/projects/myjarvis/jarvis/db/migrations")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def test_p5_migration_0011_roundtrip(tmp_path: Path):
    db_file = tmp_path / "p5_alembic.db"
    sync_url = f"sqlite:///{db_file.resolve().as_posix()}"
    cfg = _alembic_cfg(sync_url)

    command.upgrade(cfg, "0010_p4_routines_and_proactive")
    command.upgrade(cfg, "0011_p5_undo_journal")

    db = DatabaseManager(sync_url, data_dir=tmp_path)
    with db.sync_engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert {"undo_entries", "action_executions"}.issubset(tables)
    asyncio.run(db.close())

    command.downgrade(cfg, "0010_p4_routines_and_proactive")
    db2 = DatabaseManager(sync_url, data_dir=tmp_path)
    with db2.sync_engine.connect() as conn:
        tables_after = set(inspect(conn).get_table_names())
    assert "undo_entries" not in tables_after
    assert "action_executions" not in tables_after
    asyncio.run(db2.close())
    command.upgrade(cfg, "head")


def test_p5_honest_input_verification_contract(tmp_path: Path):
    spec = ActionsSpec(allow_input_automation=True, confirm_levels=[])
    fs = SafeFilesystem(tmp_path)
    executor = ActionExecutor(spec, fs)

    # Keyboard type and mouse click now run honest verification with ToolOutcome
    res = executor.request_or_execute("keyboard_type", {"text": "pytest input"})
    assert res["status"] in {"started", "completed", "unverified"}
    assert res["outcome"] is not None
    assert res["outcome"]["tool_name"] == "keyboard_type"
    assert len(res["outcome"]["checks"]) >= 1
    assert res["outcome"]["checks"][0]["name"] == "os_input_platform_verified"


def test_p5_undo_service_rollback_external_conflict_and_expiration(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'undo.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        undo_svc = UndoService(db, default_ttl_s=3600)

        # 1. Reversible file modify snapshot + successful rollback
        target_f = tmp_path / "code.py"
        target_f.write_text("x = 1\n", encoding="utf-8")
        before_b = target_f.read_bytes()
        target_f.write_text("x = 2\n", encoding="utf-8")
        after_b = target_f.read_bytes()

        snap = await undo_svc.record_snapshot(
            action_type="modify_file",
            target_path=str(target_f),
            before_bytes=before_b,
            after_bytes=after_b,
            reason="Edit constant",
        )
        assert snap["status"] == "available"
        assert snap["reversible"] is True

        undone = await undo_svc.apply_undo(snap["undo_id"])
        assert undone["status"] == "undone"
        assert target_f.read_text(encoding="utf-8") == "x = 1\n"

        # Attempting duplicate undo fails
        with pytest.raises(UndoConflictError):
            await undo_svc.apply_undo(snap["undo_id"])

        # 2. External conflict detection
        target_f.write_text("x = 2\n", encoding="utf-8")
        snap2 = await undo_svc.record_snapshot(
            action_type="modify_file",
            target_path=str(target_f),
            before_bytes=b"x = 1\n",
            after_bytes=target_f.read_bytes(),
        )
        # Modify externally behind Jarvis's back
        target_f.write_text("x = 999 # external tamper\n", encoding="utf-8")
        with pytest.raises(UndoConflictError) as exc_info:
            await undo_svc.apply_undo(snap2["undo_id"])
        assert "externally" in str(exc_info.value)
        refreshed = await undo_svc.get_undo_entry(snap2["undo_id"])
        assert refreshed["status"] == "conflict"

        # 3. Expiration detection
        snap_exp = await undo_svc.record_snapshot(
            action_type="modify_file",
            target_path=str(target_f),
            before_bytes=b"a",
            after_bytes=b"b",
            ttl_s=1,
        )
        await asyncio.sleep(1.2)
        with pytest.raises(UndoConflictError) as exc_exp:
            await undo_svc.apply_undo(snap_exp["undo_id"])
        assert "expired" in str(exc_exp.value)
        await db.close()

    asyncio.run(scenario())


def test_p5_documents_parse_bypass_fixed_and_api_v2_undo(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    secret_dir = tmp_path / "unauthorized"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("SUPER_SECRET_KEY=ABC123XYZ\n", encoding="utf-8")

    approved_dir = tmp_path / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    approved_file = approved_dir / "report.md"
    approved_file.write_text("# Quarterly Report\nVerified metrics.\n", encoding="utf-8")

    s = Settings(notes_dir=str(notes_dir), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data")
    asyncio.run(
        orch.fs_access.grant_direct(
            str(approved_dir),
            operations=["list_directory", "read_file"],
            scope="persistent",
            purpose="test",
        )
    )

    fake_assistant = type("A", (), {"orchestrator": orch, "actions": ActionExecutor(s.actions, SafeFilesystem(notes_dir))})()
    app = FastAPI()
    app.include_router(create_v2_router(fake_assistant))
    client = TestClient(app)

    # 1. Documents parse route now respects fs_access and rejects ungranted paths with 403
    denied = client.post("/api/v2/documents/parse", json={"path": str(secret_file)})
    assert denied.status_code == 403

    # 2. Approved documents parse succeeds via parse_document_untrusted
    allowed = client.post("/api/v2/documents/parse", json={"path": str(approved_file)})
    assert allowed.status_code == 200
    assert "Quarterly Report" in str(allowed.json())

    # 3. Undo API endpoints
    async def create_undo_snap():
        return await orch.undo.record_snapshot(
            action_type="create_file",
            target_path=str(approved_dir / "temp.txt"),
            reason="API test creation",
        )

    snap = asyncio.run(create_undo_snap())
    (approved_dir / "temp.txt").write_text("Temporary file", encoding="utf-8")

    list_res = client.get("/api/v2/undo")
    assert list_res.status_code == 200
    assert any(e["undo_id"] == snap["undo_id"] for e in list_res.json()["entries"])

    undo_res = client.post(f"/api/v2/undo/{snap['undo_id']}")
    assert undo_res.status_code == 200
    assert undo_res.json()["entry"]["status"] == "undone"
    assert not (approved_dir / "temp.txt").exists()

    asyncio.run(orch.db.close())
