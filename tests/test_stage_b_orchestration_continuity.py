"""Stage B tests: Task Classification, Multi-Step Checkpointed Execution, Command History, & NL Continuity."""
from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from jarvis.agents.multistep import classify_task_category
from jarvis.api import create_app
from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


def _make_project(root: Path) -> Path:
    erp = root / "ERP_V2"
    backend = erp / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (erp / "pyproject.toml").write_text('[project]\nname = "erp-v2"\nversion = "2.0.0"\n', encoding="utf-8")
    (backend / "database.py").write_text(
        'DATABASE_URL = "postgresql+psycopg://erp_user:SuperSecretPass123@localhost:5432/erp_db"\n'
        "POOL_SIZE = 10\n"
        "ECHO_SQL = False\n",
        encoding="utf-8",
    )
    (backend / "app.py").write_text("from .database import POOL_SIZE\nAPP_NAME = 'erp'\n", encoding="utf-8")
    return erp


def test_stage_b_task_classification_across_categories():
    assert classify_task_category("What is Python?") == "informational"
    assert classify_task_category("Search my Downloads folder for invoices", intent="filesystem_ops") == "filesystem_read"
    assert classify_task_category("Create a summary report in Documents", intent="filesystem_ops") == "filesystem_write"
    assert classify_task_category("Launch notepad") == "desktop_action"
    assert classify_task_category("Search the web for FastAPI release notes", intent="web_research") == "browser_web"
    assert classify_task_category("Refactor backend endpoint", intent="dev_coding", department="dev") == "coding_project"
    assert (
        classify_task_category("Open ERP_V2; inspect the backend; find the database configuration and then explain the connection")
        == "multi_step"
    )


def test_stage_b_multistep_checkpoint_permission_resume_and_safe_replay(assistant, settings, tmp_path):
    erp = _make_project(tmp_path)

    class SynthesisBrain(FakeBrain):
        async def stream_events(self, messages, tools=None):
            joined = "\n".join(str(m.get("content") or "") for m in messages)
            assert "SuperSecretPass123" not in joined
            assert "POOL_SIZE = 10" in joined
            yield {
                "type": "token",
                "text": "Verified ERP_V2 backend database.py connects via PostgreSQL (localhost:5432/erp_db) with POOL_SIZE = 10.",
            }
            self.last_stats = {"ttft": 0.05, "tokens": 18, "tps": 60.0, "total": 0.1, "model": self.spec.model, "truncated": False}

    assistant.brain = SynthesisBrain(settings)
    client = TestClient(create_app(settings, assistant=assistant))

    r_alias = client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)
    assert r_alias.status_code == 200

    # 1. Explicit sequential multi-step instruction pauses at step-1 for scoped FS permission
    prompt = "Open ERP_V2; inspect the backend; find the database configuration and then explain the database connection"
    r1 = client.post("/api/chat", json={"message": prompt}, headers=H)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["command"] == "permission_required"
    req_id = d1["permission_request"]["request_id"]
    assert Path(d1["permission_request"]["canonical_path"]).resolve() == erp.resolve()
    assert d1["task"]["status"] == "awaiting_permission"

    # Continuity check before approval: explain blocker honestly without auto-approving
    r_what = client.post("/api/chat", json={"message": "what happened to my last request"}, headers=H)
    assert r_what.status_code == 200
    assert "awaiting_permission" in r_what.json()["answer"]

    r_cont_blocked = client.post("/api/chat", json={"message": "continue previous task"}, headers=H)
    assert r_cont_blocked.status_code == 200
    assert "waiting for filesystem permission" in r_cont_blocked.json()["answer"].lower()

    # 2. Approve allow_once -> resumes multistep_resume, runs step-1..step-4, redacts secret, saves checkpoints
    r2 = client.post(
        f"/api/v2/filesystem/requests/{req_id}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["granted"] is True
    assert "POOL_SIZE = 10" in d2["reply"]
    assert "SuperSecretPass123" not in d2["reply"]
    assert d2["task"]["status"] == "completed"
    assert len(d2["task"].get("completed_step_keys") or []) >= 4
    assert any(Path(p).name == "database.py" for p in d2["paths"])

    # Once-grant must already be consumed
    assert client.get("/api/v2/filesystem/permissions?status=active").json()["permissions"] == []

    # 3. Command history recorded & continuity meta commands are non-replayable
    hist = client.get("/api/v2/commands/history").json()
    assert hist["count"] >= 2
    orig_cmd = next(c for c in hist["commands"] if c["raw_text"] == prompt)
    assert orig_cmd["task_category"] == "multi_step"
    assert orig_cmd["status"] == "completed"
    assert orig_cmd["replayable"] is True

    # 4. NL "repeat last command" must replay the real command AND enforce fresh permission check!
    r_rep = client.post("/api/chat", json={"message": "repeat last command"}, headers=H)
    assert r_rep.status_code == 200
    d_rep = r_rep.json()
    assert d_rep["command"] == "permission_required"
    rep_req_id = d_rep["permission_request"]["request_id"]
    assert rep_req_id != req_id

    # 5. NL "cancel current task" cancels the newly awaiting task & consumes its pending permission state
    r_can = client.post("/api/chat", json={"message": "cancel current task"}, headers=H)
    assert r_can.status_code == 200
    assert "Cancelled" in r_can.json()["answer"]

    # 6. NL "show recent tasks" and "show failed tasks" + API replay endpoint
    r_rec = client.post("/api/chat", json={"message": "show recent tasks"}, headers=H)
    assert r_rec.status_code == 200
    assert "Recent commands/tasks" in r_rec.json()["answer"]

    # Simulate a failed task record to verify "show failed tasks"
    async def _seed_failed():
        ft = await assistant.orchestrator.tasks.create_task("Broken step test", status="running", current_step="Failed at step-2")
        await assistant.orchestrator.tasks.update_task(ft["task_id"], status="failed", error_info="Simulated timeout error", current_step="Failed at step-2")

    asyncio.run(_seed_failed())
    r_fail = client.post("/api/chat", json={"message": "show failed tasks"}, headers=H)
    assert r_fail.status_code == 200
    assert "Simulated timeout error" in r_fail.json()["answer"]

    # Non-replayable meta command rejects API replay with 409; valid command increments replay_count
    meta_cmd = next(c for c in client.get("/api/v2/commands/history").json()["commands"] if not c["replayable"])
    assert client.post(f"/api/v2/commands/history/{meta_cmd['id']}/replay", headers=H).status_code == 409

    r_api_rep = client.post(f"/api/v2/commands/history/{orig_cmd['id']}/replay", headers=H)
    assert r_api_rep.status_code == 200
    assert r_api_rep.json()["result"]["command"] == "permission_required"


def test_stage_b_multistep_cancellation_and_checkpoint_resume(assistant, settings, tmp_path):
    erp = _make_project(tmp_path)

    class SimpleSynthBrain(FakeBrain):
        async def stream_events(self, messages, tools=None):
            yield {"type": "token", "text": "Resumed synthesis complete with POOL_SIZE = 10."}
            self.last_stats = {"ttft": 0.05, "tokens": 10, "tps": 50.0, "total": 0.1, "model": self.spec.model, "truncated": False}

    assistant.brain = SimpleSynthBrain(settings)
    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)
    client.post(
        "/api/v2/filesystem/permissions",
        json={"folder_path": str(erp), "operations": ["list_directory", "search_files", "read_file", "read_metadata"], "scope": "session"},
        headers=H,
    )

    async def _run_cancel_and_resume():
        t = await assistant.orchestrator.tasks.create_task("Open ERP_V2; inspect backend; find database and then explain it", status="planning")
        ev = asyncio.Event()
        ev.set()  # Cancel before step-1 runs
        res_cancel = await assistant._run_multistep_workflow(
            "Open ERP_V2; inspect backend; find database and then explain it",
            resolved_target=str(erp),
            alias_name="ERP_V2",
            task_id=t["task_id"],
            cancel_event=ev,
        )
        assert res_cancel["interrupted"] is True

        # Reset task to paused with checkpoint and verify "continue previous task" completes it
        await assistant.orchestrator.tasks.update_task(t["task_id"], status="paused", current_step="Paused mid-plan")
        res_cont = await assistant.chat("continue previous task")
        assert res_cont["ok"] is True
        assert "Resumed synthesis" in res_cont["answer"]
        done_task = await assistant.orchestrator.tasks.get_task(t["task_id"])
        assert done_task["status"] == "completed"

    asyncio.run(_run_cancel_and_resume())

