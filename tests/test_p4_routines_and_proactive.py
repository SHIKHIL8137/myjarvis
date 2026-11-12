"""P4 verification tests: Unified routines, cron reminders, quiet hours, late policies, idempotency, persistent proactive dismissals & /v2/routines API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.api_v2 import create_v2_router
from jarvis.config import BrainSpec, ProactiveSpec, Settings
from jarvis.db.engine import DatabaseManager
from jarvis.departments.assistant.reminders import ReminderService
from jarvis.departments.assistant.routines import RoutineSchedulerService, is_in_quiet_hours
from jarvis.proactive import ProactiveSuggestionEngine


def _alembic_cfg(sync_url: str) -> Config:
    cfg = Config("d:/projects/myjarvis/alembic.ini")
    cfg.set_main_option("script_location", "d:/projects/myjarvis/jarvis/db/migrations")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def test_p4_migration_0010_roundtrip(tmp_path: Path):
    db_file = tmp_path / "p4_alembic.db"
    sync_url = f"sqlite:///{db_file.resolve().as_posix()}"
    cfg = _alembic_cfg(sync_url)

    command.upgrade(cfg, "0009_p1_task_engine_fields")
    command.upgrade(cfg, "0010_p4_routines_and_proactive")

    db = DatabaseManager(sync_url, data_dir=tmp_path)
    with db.sync_engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert {"routines", "routine_runs", "proactive_dismissals"}.issubset(tables)
    asyncio.run(db.close())

    command.downgrade(cfg, "0009_p1_task_engine_fields")
    db2 = DatabaseManager(sync_url, data_dir=tmp_path)
    with db2.sync_engine.connect() as conn:
        tables_after = set(inspect(conn).get_table_names())
    assert "routines" not in tables_after
    assert "proactive_dismissals" not in tables_after
    asyncio.run(db2.close())
    command.upgrade(cfg, "head")


def test_p4_quiet_hours_and_cron_reminder_rescheduling(tmp_path: Path):
    # Overnight quiet hours wrap: 22:00 -> 07:00
    qh = {"enabled": True, "start": "22:00", "end": "07:00"}
    assert is_in_quiet_hours(qh, datetime(2026, 11, 12, 23, 30)) is True
    assert is_in_quiet_hours(qh, datetime(2026, 11, 12, 6, 15)) is True
    assert is_in_quiet_hours(qh, datetime(2026, 11, 12, 14, 0)) is False

    db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'rem.db').as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    rems = ReminderService(db)
    base_ts = datetime(2026, 11, 12, 10, 0).timestamp()
    r = rems.create(title="Daily standup", due_at=base_ts - 5, recurrence_cron="0 9 * * *")
    fired = rems.poll_due(now=base_ts)
    assert len(fired) == 1
    assert fired[0]["id"] == r["id"]
    # Rescheduled automatically to next 09:00 instead of remaining triggered
    listed = rems.list_reminders(status="scheduled")
    assert len(listed) == 1
    assert listed[0]["due_at"] > base_ts
    asyncio.run(db.close())


def test_p4_routines_idempotency_late_policy_quiet_hours_and_conditional(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'rt.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        qh_state = {"enabled": False, "start": "22:00", "end": "07:00"}
        svc = RoutineSchedulerService(db, quiet_hours_fn=lambda: qh_state)

        # 1. Once trigger due cleanly + idempotent second poll does NOT double fire
        noon_ts = datetime(2026, 11, 12, 12, 0).timestamp()
        rt_once = await svc.create_routine(
            title="Check backups",
            trigger_type="once",
            trigger_spec={"due_at": noon_ts + 10},
            late_policy="run_once_if_within_grace",
            grace_period_s=120,
        )
        ev1 = await svc.poll_due_routines(now=noon_ts + 20)
        assert len(ev1) == 1
        assert ev1[0]["run"]["status"] == "completed"
        ev2 = await svc.poll_due_routines(now=noon_ts + 25)
        assert ev2 == []

        # 2. Late missed policy 'skip' skips when delay > grace
        rt_late = await svc.create_routine(
            title="Expired alert",
            trigger_type="once",
            trigger_spec={"due_at": noon_ts + 10},
            late_policy="skip",
            grace_period_s=30,
        )
        ev_late = await svc.poll_due_routines(now=noon_ts + 200)
        assert len(ev_late) == 1
        assert ev_late[0]["status"] == "skipped_late"
        runs_late = await svc.list_runs(routine_id=rt_late["id"])
        assert runs_late[0]["status"] == "skipped_late"

        # 3. Quiet hours suppresses voice/voice+ui routines
        qh_state["enabled"] = True
        late_night_ts = datetime(2026, 11, 12, 23, 15).timestamp()
        await svc.create_routine(
            title="Nightly ping",
            trigger_type="once",
            trigger_spec={"due_at": late_night_ts - 5},
            channel="voice+ui",
        )
        ev_qh = await svc.poll_due_routines(now=late_night_ts)
        assert len(ev_qh) == 1
        assert ev_qh[0]["status"] == "skipped_quiet_hours"

        # 4. Conditional trigger fires only when metric breaches threshold
        qh_state["enabled"] = False
        await svc.create_routine(
            title="High RAM Alert",
            trigger_type="conditional",
            trigger_spec={"metric": "ram_used_pct", "op": ">=", "threshold": 85},
            channel="ui",
        )
        assert await svc.poll_due_routines(now=noon_ts + 50, metrics={"ram_used_pct": 50}) == []
        ev_cond = await svc.poll_due_routines(now=noon_ts + 60, metrics={"ram_used_pct": 92})
        assert len(ev_cond) == 1
        assert ev_cond[0]["routine"]["title"] == "High RAM Alert"

        # Assert original rt_once run count stayed 1
        once_after = await svc.get_routine(rt_once["id"])
        assert once_after["run_count"] == 1
        await db.close()

    asyncio.run(scenario())


def test_p4_persistent_proactive_dismissal_across_restarts(tmp_path: Path):
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'proactive.db').as_posix()}"
    db1 = DatabaseManager(db_url, data_dir=tmp_path)
    db1.init_schema_sync()
    spec = ProactiveSpec(enabled=True, cooldown_s=3600.0)
    eng1 = ProactiveSuggestionEngine(spec, db=db1)
    eng1.dismiss("sys_ram")
    asyncio.run(db1.close())

    # Simulate restart with fresh engine instance loading from DB
    db2 = DatabaseManager(db_url, data_dir=tmp_path)
    eng2 = ProactiveSuggestionEngine(spec, db=db2)
    items = eng2.collect(
        sys_snapshot={"alerts": [{"id": "ram", "metric": "ram_used_pct", "severity": "warning", "message": "RAM 95%"}]}
    )
    assert all(it["id"] != "sys_ram" for it in items)
    asyncio.run(db2.close())


def test_p4_api_v2_routines_crud_toggle_and_run_now(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(notes_dir=str(notes_dir), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data")
    fake_assistant = type("A", (), {"orchestrator": orch, "settings": s})()
    app = FastAPI()
    app.include_router(create_v2_router(fake_assistant))
    client = TestClient(app)

    create_res = client.post(
        "/api/v2/routines",
        json={
            "title": "Morning briefing",
            "trigger_type": "daily",
            "trigger_spec": {"hour": 8, "minute": 30},
            "action_type": "notification",
        },
    )
    assert create_res.status_code == 200
    rid = create_res.json()["routine"]["id"]

    list_res = client.get("/api/v2/routines")
    assert list_res.status_code == 200
    assert any(r["id"] == rid for r in list_res.json()["routines"])

    tog_res = client.post(f"/api/v2/routines/{rid}/toggle", json={"enabled": False})
    assert tog_res.status_code == 200
    assert tog_res.json()["routine"]["enabled"] is False

    run_res = client.post(f"/api/v2/routines/{rid}/run-now")
    assert run_res.status_code == 200
    assert run_res.json()["routine"]["run_count"] == 1

    runs_res = client.get(f"/api/v2/routines/runs?routine_id={rid}")
    assert runs_res.status_code == 200
    assert len(runs_res.json()["runs"]) == 1
    asyncio.run(orch.db.close())
