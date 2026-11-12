"""Focused verification tests for P10.22 P0.2: Durable Background Task Runner & Safe Recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.agents.task_runner import (
    ApprovalRequiredTaskError,
    BackgroundTaskRunner,
    QuotaSuspendedError,
    TaskExecutionResult,
    TransientTaskError,
)
from jarvis.agents.tasks import AssistantTaskService
from jarvis.api_v2 import create_v2_router
from jarvis.config import BrainSpec, Settings
from jarvis.db.engine import DatabaseManager
from jarvis.db.models import FilesystemPermissionRequestRow


def _alembic_cfg(sync_url: str) -> Config:
    cfg = Config("d:/projects/myjarvis/alembic.ini")
    cfg.set_main_option("script_location", "d:/projects/myjarvis/jarvis/db/migrations")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def test_alembic_migration_0005_roundtrip_and_postgres_sql(tmp_path: Path, capsys):
    db_file = tmp_path / "alembic_test.db"
    sync_url = f"sqlite:///{db_file.resolve().as_posix()}"
    cfg = _alembic_cfg(sync_url)

    command.upgrade(cfg, "0004_p10_20_filesystem_and_tasks")
    command.upgrade(cfg, "head")

    db = DatabaseManager(sync_url, data_dir=tmp_path)
    with db.sync_engine.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("assistant_tasks")}
    assert {
        "worker_kind",
        "attempts",
        "max_attempts",
        "not_before",
        "next_retry_at",
        "deadline_at",
        "step_budget",
        "steps_used",
        "token_budget",
        "tokens_used",
        "tool_budget",
        "tools_used_count",
        "quota_reason",
        "payload",
        "checkpoint_json",
        "steer_notes",
        "completed_step_keys",
        "claimed_by",
        "claimed_at",
    }.issubset(cols)
    asyncio.run(db.close())

    # Downgrade to 0004 and re-upgrade to head
    command.downgrade(cfg, "0004_p10_20_filesystem_and_tasks")
    db2 = DatabaseManager(sync_url, data_dir=tmp_path)
    with db2.sync_engine.connect() as conn:
        cols_after_down = {c["name"] for c in inspect(conn).get_columns("assistant_tasks")}
    assert "worker_kind" not in cols_after_down
    asyncio.run(db2.close())

    command.upgrade(cfg, "head")

    # Verify PostgreSQL SQL emission works cleanly without syntax issues
    pg_cfg = _alembic_cfg("postgresql+psycopg://user:pass@localhost:5432/jarvis")
    command.upgrade(pg_cfg, "0004_p10_20_filesystem_and_tasks:head", sql=True)
    sql_out = capsys.readouterr().out
    assert "ALTER TABLE assistant_tasks ADD COLUMN worker_kind" in sql_out
    assert "ix_assistant_tasks_not_before" in sql_out


def test_durable_recovery_across_simulated_restart_and_idempotent_checkpoint(tmp_path: Path):
    async def scenario():
        db_url = f"sqlite+aiosqlite:///{(tmp_path / 'restart.db').as_posix()}"
        db1 = DatabaseManager(db_url, data_dir=tmp_path)
        db1.init_schema_sync()
        tasks1 = AssistantTaskService(db1)

        # Safe deep research task already finished step_1 before simulated crash while running step_2
        t_safe = await tasks1.create_task(
            "Research Python 3.14 features",
            intent="web_research",
            status="running",
            worker_kind="deep_research",
            checkpoint_json={"subqueries": ["python 3.14"]},
            meta={"safe_resumable": True},
        )
        await tasks1.record_step_completion(
            t_safe["task_id"],
            "step_plan",
            checkpoint_patch={"plan_ready": True},
            current_step="Investigating sources",
        )
        await db1.close()

        # Re-open DB (simulated reboot)
        db2 = DatabaseManager(db_url, data_dir=tmp_path)
        tasks2 = AssistantTaskService(db2)
        runner = BackgroundTaskRunner(tasks2, worker_id="boot-worker")

        executed_steps: list[str] = []

        async def research_handler(ctx, task):
            if not ctx.is_step_done("step_plan"):
                executed_steps.append("step_plan")
                await ctx.complete_step("step_plan", checkpoint_patch={"plan_ready": True})
            if not ctx.is_step_done("step_verify"):
                executed_steps.append("step_verify")
                await ctx.complete_step("step_verify", checkpoint_patch={"verified": True}, current_step="Verified")
            return TaskExecutionResult(summary="Resumed and finished", result="Final output")

        runner.register_handler("deep_research", research_handler)
        recovery = await runner.start()
        assert t_safe["task_id"] in recovery["requeued"]

        res = await runner.run_once()
        await runner.stop()
        assert res is not None
        assert res["status"] == "completed"
        assert res["result"] == "Final output"
        assert executed_steps == ["step_verify"]  # step_plan was NOT duplicated
        await db2.close()

    asyncio.run(scenario())


def test_unsafe_and_expired_permission_tasks_never_auto_resume(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'safety.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)

        t_unsafe = await tasks.create_task(
            "Delete old build folder",
            intent="filesystem_ops",
            status="running",
            worker_kind="assistant",
        )
        t_approval = await tasks.create_task(
            "Run shell cleanup",
            intent="shell_exec",
            status="awaiting_approval",
            worker_kind="assistant",
        )
        t_perm_exp = await tasks.create_task(
            "Read Desktop files",
            intent="filesystem_ops",
            status="awaiting_permission",
            worker_kind="assistant",
        )
        async with db.async_session() as s:
            s.add(
                FilesystemPermissionRequestRow(
                    request_id="req-expired-1",
                    task_id=t_perm_exp["task_id"],
                    requested_path=str(tmp_path),
                    canonical_path=str(tmp_path),
                    requested_operations=["read_file"],
                    status="pending",
                    expires_at=time.time() - 60.0,
                )
            )
            await s.commit()

        t_dead = await tasks.create_task(
            "Expired research",
            intent="web_research",
            status="queued",
            worker_kind="deep_research",
            deadline_at=time.time() - 5.0,
        )

        rep = await tasks.recover_interrupted_on_boot()
        assert t_unsafe["task_id"] in rep["paused_unsafe"]
        assert t_approval["task_id"] in rep["halted_approval"]
        assert t_perm_exp["task_id"] in rep["halted_approval"]
        assert t_dead["task_id"] in rep["expired"]

        unsafe_row = await tasks.get_task(t_unsafe["task_id"])
        perm_row = await tasks.get_task(t_perm_exp["task_id"])
        dead_row = await tasks.get_task(t_dead["task_id"])
        assert unsafe_row["status"] == "paused"
        assert unsafe_row["meta"]["blocked_auto_resume_reason"] == "unsafe_or_side_effectful_action"
        assert perm_row["status"] == "awaiting_permission"
        assert perm_row["meta"]["approval_expired"] is True
        assert dead_row["status"] == "failed"
        await db.close()

    asyncio.run(scenario())


def test_concurrent_workers_only_claim_task_once(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'conc.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)
        task = await tasks.create_task(
            "Single queued task",
            intent="web_research",
            status="queued",
            worker_kind="deep_research",
        )
        c1, c2 = await asyncio.gather(
            tasks.claim_next_task("worker-a", worker_kinds=["deep_research"]),
            tasks.claim_next_task("worker-b", worker_kinds=["deep_research"]),
        )
        winners = [c for c in (c1, c2) if c is not None]
        assert len(winners) == 1
        assert winners[0]["task_id"] == task["task_id"]
        assert winners[0]["attempts"] == 1
        await db.close()

    asyncio.run(scenario())


def test_retries_backoff_quota_suspension_and_steering(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'retry.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)
        runner = BackgroundTaskRunner(tasks, backoff_base_s=0.05, backoff_max_s=0.1)

        calls = 0

        async def flaky_handler(ctx, task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientTaskError("temporary timeout")
            if calls == 2:
                raise QuotaSuspendedError("rate limit 429", resume_after_s=0.01)
            notes = await ctx.latest_steer_instructions()
            return {"summary": "Finished after steer", "result": f"notes={notes}"}

        runner.register_handler("deep_research", flaky_handler)
        t = await runner.enqueue("Investigate rate limits", worker_kind="deep_research", max_attempts=4)

        # Attempt 1 -> scheduled retry with not_before
        r1 = await runner.run_once()
        assert r1["status"] == "queued"
        assert r1["next_retry_at"] is not None

        # Steer live while waiting retry
        await tasks.append_steer(t["task_id"], "Focus only on official docs")

        await asyncio.sleep(0.12)
        # Attempt 2 -> waiting_quota
        r2 = await runner.run_once()
        assert r2["status"] == "waiting_quota"
        assert "rate limit" in r2["quota_reason"]

        await asyncio.sleep(0.03)
        # Attempt 3 -> completed with steering note observed
        r3 = await runner.run_once()
        assert r3["status"] == "completed"
        assert "Focus only on official docs" in r3["result"]

        # Exhausted retry failure + budget exceeded suspension
        t_budget = await runner.enqueue("Too many steps", worker_kind="deep_research", step_budget=1)
        await tasks.record_step_completion(t_budget["task_id"], "s1", delta_steps=1)
        rb = await runner.run_once()
        assert rb["status"] == "waiting_quota"
        assert "step_budget" in rb["quota_reason"]

        # Approval error never retries
        async def gate_handler(ctx, task):
            raise ApprovalRequiredTaskError("Need permission", status="awaiting_permission")

        runner.register_handler("gated_job", gate_handler)
        await runner.enqueue("Need perm", worker_kind="gated_job")
        rg = await runner.run_once()
        assert rg["status"] == "awaiting_permission"
        await db.close()

    asyncio.run(scenario())


def test_api_v2_task_steer_and_resume_routes(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(notes_dir=str(notes_dir), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data")
    fake_assistant = type("A", (), {"orchestrator": orch})()

    app = FastAPI()
    app.include_router(create_v2_router(fake_assistant))
    client = TestClient(app)

    t = asyncio.run(
        orch.tasks.create_task(
            "Background research",
            intent="web_research",
            status="queued",
            worker_kind="deep_research",
        )
    )
    asyncio.run(orch.tasks.suspend_for_quota(t["task_id"], reason="provider rate limit"))
    steer_res = client.post(
        f"/api/v2/tasks/{t['task_id']}/steer",
        json={"instruction": "Skip blogs and use RFCs", "resume_if_paused": True},
    )
    assert steer_res.status_code == 200
    body = steer_res.json()["task"]
    assert body["status"] == "queued"
    assert len(body["steer_notes"]) == 1
    assert body["steer_notes"][0]["instruction"] == "Skip blogs and use RFCs"

    # Approval-gated task MUST refuse manual force-resume via /resume
    t_ap = asyncio.run(
        orch.tasks.create_task("Write host file", intent="filesystem_ops", status="awaiting_approval")
    )
    bad_resume = client.post(f"/api/v2/tasks/{t_ap['task_id']}/resume")
    assert bad_resume.status_code == 409
    asyncio.run(orch.db.close())
