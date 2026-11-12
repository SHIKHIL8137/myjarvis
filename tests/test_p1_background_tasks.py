"""P1 verification tests: Reliable Background Task Engine, transitions, scheduling, checkpoints, budgets, API & 0009 migration."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.agents.tasks import AssistantTaskService, validate_task_transition
from jarvis.config import BrainSpec, Settings
from jarvis.core.errors import TaskTransitionError
from jarvis.db.engine import DatabaseManager
from jarvis.db.models import FilesystemPermissionRequestRow


def _alembic_cfg(sync_url: str) -> Config:
    cfg = Config("d:/projects/myjarvis/alembic.ini")
    cfg.set_main_option("script_location", "d:/projects/myjarvis/jarvis/db/migrations")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def test_p1_migration_0009_roundtrip(tmp_path: Path, capsys):
    db_file = tmp_path / "p1_alembic.db"
    sync_url = f"sqlite:///{db_file.resolve().as_posix()}"
    cfg = _alembic_cfg(sync_url)

    command.upgrade(cfg, "0008_p11_command_history")
    command.upgrade(cfg, "0009_p1_task_engine_fields")

    db = DatabaseManager(sync_url, data_dir=tmp_path)
    with db.sync_engine.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("assistant_tasks")}
    assert {"scheduled_for", "pause_reason", "partial_report", "correlation_id", "parent_task_id"}.issubset(cols)
    asyncio.run(db.close())

    command.downgrade(cfg, "0008_p11_command_history")
    db2 = DatabaseManager(sync_url, data_dir=tmp_path)
    with db2.sync_engine.connect() as conn:
        cols_after = {c["name"] for c in inspect(conn).get_columns("assistant_tasks")}
    assert "scheduled_for" not in cols_after
    assert "partial_report" not in cols_after
    asyncio.run(db2.close())

    command.upgrade(cfg, "head")
    pg_cfg = _alembic_cfg("postgresql+psycopg://user:pass@localhost:5432/jarvis")
    command.upgrade(pg_cfg, "0008_p11_command_history:head", sql=True)
    sql_out = capsys.readouterr().out
    assert "ALTER TABLE assistant_tasks ADD COLUMN scheduled_for" in sql_out
    assert "ix_assistant_tasks_correlation_id" in sql_out


def test_p1_state_transitions_pause_resume_and_expiry_revalidation(tmp_path: Path):
    # Canonical status transitions
    assert validate_task_transition("done", "done") == "completed"
    assert validate_task_transition("waiting", "running") == "running"
    with pytest.raises(TaskTransitionError):
        validate_task_transition("completed", "running")

    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'transitions.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)

        t = await tasks.create_task("Long report", intent="multistep", status="queued", worker_kind="multistep")
        await tasks.update_task(t["task_id"], status="running", current_step="Step 1")
        await tasks.record_step_completion(t["task_id"], "step:0", current_step="Done 1", delta_steps=1)

        paused = await tasks.pause_task(t["task_id"], reason="Owner requested pause")
        assert paused is not None
        assert paused["status"] == "paused"
        assert paused["pause_reason"] == "Owner requested pause"
        assert paused["partial_report"]["completed_steps"] == ["step:0"]

        # Safe resume succeeds
        resumed = await tasks.resume_task(t["task_id"], reason="Continue now")
        assert resumed is not None
        assert resumed["status"] == "queued"
        assert resumed["canonical_status"] == "queued"

        # Protected task with expired FS grant refuses resume and transitions to awaiting_permission
        t_prot = await tasks.create_task("Write export", intent="filesystem_ops", status="paused")
        async with db.async_session() as s:
            s.add(
                FilesystemPermissionRequestRow(
                    request_id="fsreq-expired",
                    task_id=t_prot["task_id"],
                    requested_path=str(tmp_path / "out.txt"),
                    canonical_path=str(tmp_path / "out.txt"),
                    requested_operations=["write"],
                    not_requested_operations=[],
                    sensitive_flags=[],
                    deferred_action={},
                    reason="write file",
                    status="expired",
                    expires_at=time.time() - 10,
                )
            )
            await s.commit()
        with pytest.raises(TaskTransitionError):
            await tasks.resume_task(t_prot["task_id"])
        refreshed = await tasks.get_task(t_prot["task_id"])
        assert refreshed["status"] == "awaiting_permission"
        assert refreshed["meta"]["approval_expired"] is True
        await db.close()

    asyncio.run(scenario())


def test_p1_scheduled_claim_checkpoint_resume_steer_and_budget_partial_report(tmp_path: Path):
    async def scenario():
        s = Settings(notes_dir=str(tmp_path / "notes"), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
        orch = AgentOrchestrator(s, data_dir=tmp_path / "data")

        # 1. Future scheduled task must not be claimed prematurely
        fut = await orch.task_runner.enqueue(
            "Future scheduled task",
            worker_kind="multistep",
            scheduled_for=time.time() + 120.0,
            payload={"steps": ["Alpha", "Beta"]},
        )
        assert fut["status"] == "scheduled"
        claimed_none = await orch.task_runner.run_once()
        assert claimed_none is None

        # 2. Due scheduled task with simulated quota suspension at step 1 -> checkpoint saved -> steer -> resumed & completed
        due = await orch.tasks.create_task(
            "Checkpointed task",
            worker_kind="multistep",
            status="scheduled",
            scheduled_for=time.time() - 1.0,
            step_budget=5,
            correlation_id="corr-p1-1",
            payload={"steps": ["Gather", "Analyze", "Finalize"], "simulate_quota_at_step": 1, "quota_reason": "rate_limit"},
        )
        r1 = await orch.task_runner.run_once()
        assert r1 is not None
        assert r1["status"] == "waiting_quota"
        assert len(r1["completed_step_keys"]) == 1
        assert r1["partial_report"] is not None
        assert r1["correlation_id"] == "corr-p1-1"

        # Remove quota fault cleanly and add steering note, then resume
        await orch.tasks.update_task(
            due["task_id"],
            payload_patch={"simulate_quota_at_step": None},
        )
        await orch.tasks.append_steer(due["task_id"], "Prioritize accuracy")
        await orch.tasks.resume_task(due["task_id"], reason="Quota cleared")

        r2 = await orch.task_runner.run_once()
        assert r2 is not None
        assert r2["status"] == "completed"
        assert r2["canonical_status"] == "done"
        # Step 0 was skipped idempotently -> total steps_used == 3 (1 before + 2 after)
        assert r2["steps_used"] == 3
        assert "Prioritize accuracy" in r2["result"]

        # 3. Step budget exhaustion produces structured partial report
        await orch.task_runner.enqueue(
            "Budget bounded job",
            worker_kind="assistant",
            step_budget=1,
            payload={"steps": ["Step 1", "Step 2"]},
        )
        rb1 = await orch.task_runner.run_once()
        assert rb1 is not None
        assert rb1["status"] == "waiting_quota"
        assert "step_budget" in rb1["quota_reason"]
        assert rb1["partial_report"] is not None
        assert rb1["partial_report"]["budget_usage"]["steps_used"] >= 1
        assert rb1["partial_report"]["budget_usage"]["step_budget"] == 1
        await orch.db.close()

    asyncio.run(scenario())


def test_p1_api_v2_create_pause_steer_resume_and_invalid_transition_409(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(notes_dir=str(notes_dir), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data")
    fake_assistant = type("A", (), {"orchestrator": orch})()

    from jarvis.api_v2 import create_v2_router

    app = FastAPI()
    app.include_router(create_v2_router(fake_assistant))
    # Also verify full create_app ErrorEnvelope formatting via api_v2 exception handler if wired
    client = TestClient(app)

    create_res = client.post(
        "/api/v2/tasks",
        json={"goal": "Run background check", "worker_kind": "multistep", "step_budget": 4},
    )
    assert create_res.status_code == 200
    tid = create_res.json()["task"]["task_id"]
    assert create_res.json()["task"]["canonical_status"] == "queued"

    pause_res = client.post(f"/api/v2/tasks/{tid}/pause", json={"reason": "Hold on"})
    assert pause_res.status_code == 200
    assert pause_res.json()["task"]["status"] == "paused"

    steer_res = client.post(f"/api/v2/tasks/{tid}/steer", json={"instruction": "Focus on tests", "resume_if_paused": False})
    assert steer_res.status_code == 200
    assert steer_res.json()["task"]["status"] == "paused"

    resume_res = client.post(f"/api/v2/tasks/{tid}/resume", json={"note": "Proceed"})
    assert resume_res.status_code == 200
    assert resume_res.json()["task"]["status"] == "queued"

    cancel_res = client.post(f"/api/v2/tasks/{tid}/cancel", json={"reason": "Done testing"})
    assert cancel_res.status_code == 200
    assert cancel_res.json()["task"]["status"] == "cancelled"

    bad_pause = client.post(f"/api/v2/tasks/{tid}/pause", json={"reason": "Cannot pause cancelled"})
    assert bad_pause.status_code == 409
    asyncio.run(orch.db.close())
