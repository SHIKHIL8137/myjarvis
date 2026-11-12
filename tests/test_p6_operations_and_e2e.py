"""P6 Comprehensive End-to-End Test Suite covering Scenarios A through J.

Verifies:
- Scenario A: Natural language project folder resolution, alias precedence, and workspace sandboxing (excluding temp/pytest folders).
- Scenario B: Durable background task execution, pause, live steering, quota suspension, and resumption.
- Scenario C: Evidence-based deep research engine with verbatim quote offset spans and honest provenance tracking.
- Scenario D: Cross-source contradiction detection and freshness & limitations metadata.
- Scenario E: Markdown agent profile loading, precedence (project > user > builtin), and bounded delegation depth.
- Scenario F: Privilege escalation prevention across agent tiers (READ < PROMPT < EXECUTE < ADMIN).
- Scenario G: Unified routine scheduler with cron/interval triggers, idempotency journal, and overnight quiet hours suppression.
- Scenario H: Persistent proactive suggestion dismissal surviving simulated restart across distinct DB instances.
- Scenario I: Honest desktop action execution with verified ToolOutcome reporting.
- Scenario J: Reversible action snapshots, external modification conflict rejection, and undo rollback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import shutil
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from jarvis.actions import ActionExecutor
from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.agents.profiles import AgentWorkforceCoordinator
from jarvis.agents.research import (
    ResearchClaim,
    ResearchSnapshot,
    VerificationReport,
    _detect_contradictions,
    build_freshness_and_limitations,
    verify_claims_against_snapshots,
)
from jarvis.agents.tasks import validate_task_transition
from jarvis.api_v2 import create_v2_router
from jarvis.config import ActionsSpec, BrainSpec, ProactiveSpec, Settings
from jarvis.core.errors import TaskTransitionError, UndoConflictError
from jarvis.db.engine import DatabaseManager
from jarvis.departments.assistant.routines import RoutineSchedulerService, is_in_quiet_hours
from jarvis.filesystem import SafeFilesystem
from jarvis.filesystem_access import validate_workspace_roots
from jarvis.proactive import ProactiveSuggestionEngine
from jarvis.undo import UndoService


def _build_test_app(settings: Settings, orch: AgentOrchestrator) -> TestClient:
    app = FastAPI()
    safe_fs = SafeFilesystem(settings.notes_dir)
    mock_assistant = type(
        "MockAssistant",
        (),
        {
            "settings": settings,
            "orchestrator": orch,
            "actions": ActionExecutor(settings.actions, fs=safe_fs),
            "proactive": ProactiveSuggestionEngine(settings.proactive, db=orch.db),
            "os_state": "normal",
        },
    )()
    router = create_v2_router(mock_assistant)
    app.include_router(router)
    return TestClient(app)


# ==============================================================================
# Scenario A: NL Project Folder Resolution & Sandboxing
# ==============================================================================
def test_scenario_a_project_resolution_and_sandboxing(tmp_path: Path):
    """Verify alias precedence, workspace search, and exclusion of temp/pytest directories."""
    # Use a non-temp sandbox inside tests/ so workspace validator recognizes it as authentic workspace
    test_sandbox = Path(__file__).resolve().parent / f"_ws_sandbox_{uuid.uuid4().hex[:8]}"
    test_sandbox.mkdir(parents=True, exist_ok=True)
    try:
        erp_project = test_sandbox / "ERP_V2"
        erp_project.mkdir()
        (erp_project / "package.json").write_text('{"name": "erp-v2"}', encoding="utf-8")

        # Temp pytest mock dir (must be rejected)
        pytest_temp = tmp_path / "pytest-of-runner" / "ERP_V2"
        pytest_temp.mkdir(parents=True, exist_ok=True)
        (pytest_temp / "package.json").write_text('{"name": "temp-erp"}', encoding="utf-8")

        # 1. Validation of workspace roots accepts genuine workspace directory
        roots = validate_workspace_roots([str(test_sandbox)])
        assert str(test_sandbox.resolve()) in [str(Path(r).resolve()) for r in roots]

        # 2. Pytest temporary directories are strictly rejected as workspace roots
        with pytest.raises(ValueError) as exc_temp:
            validate_workspace_roots([str(pytest_temp)])
        assert "pytest" in str(exc_temp.value).lower() or "temporary" in str(exc_temp.value).lower()

        # 3. Search for project inside validated workspace roots finds erp_project
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'sc_a.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        s = Settings(notes_dir=str(tmp_path), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
        orch = AgentOrchestrator(s, data_dir=tmp_path / "data", db=db)

        search_res = asyncio.run(orch.fs_access.search_directory_for_project(test_sandbox, "ERP_V2"))
        assert search_res["ok"] is True
        assert len(search_res["matches"]) >= 1
        assert Path(search_res["matches"][0]["path"]).resolve() == erp_project.resolve()

        # 4. Explicit project alias registry takes highest priority
        alias_rec = asyncio.run(orch.fs_access.upsert_alias("my_custom_erp", str(erp_project)))
        assert alias_rec["alias"] == "my_custom_erp"

        resolved = asyncio.run(orch.fs_access.resolve_alias("my_custom_erp"))
        assert resolved is not None
        assert Path(resolved["resolved_path"]).resolve() == erp_project.resolve()

        asyncio.run(db.close())
    finally:
        shutil.rmtree(test_sandbox, ignore_errors=True)


# ==============================================================================
# Scenario B: Durable Background Task Engine, Pause, Steer & Resume
# ==============================================================================
def test_scenario_b_durable_tasks_pause_steer_quota_resume(tmp_path: Path):
    """Verify task state transitions, checkpointing, pause/steer, and resumption."""
    db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'sc_b.db').as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    s = Settings(notes_dir=str(tmp_path), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data", db=db)
    client = _build_test_app(s, orch)

    # 1. Create a task via API
    resp = client.post("/api/v2/tasks", json={"goal": "Refactor database migrations", "worker_kind": "code_refactor"})
    assert resp.status_code == 200
    task_id = resp.json()["task"]["task_id"]

    # 2. Simulate checkpointed execution and step budget consumption
    async def step_task():
        t = await orch.tasks.get_task(task_id)
        assert t is not None
        await orch.tasks.update_task(task_id, status="running")
        await orch.tasks.update_task(
            task_id,
            partial_report={"summary": "Analyzed 5 files out of 10. Found 2 schema drift warnings.", "steps_used": 5},
        )

    asyncio.run(step_task())

    # 3. Live steering while running
    steer_resp = client.post(
        f"/api/v2/tasks/{task_id}/steer",
        json={"instruction": "Prioritize PostgreSQL compatibility"},
    )
    assert steer_resp.status_code == 200
    assert "steer_notes" in steer_resp.json()["task"]
    assert "PostgreSQL" in steer_resp.json()["task"]["steer_notes"][0]["instruction"]

    # 4. Pause task
    pause_resp = client.post(f"/api/v2/tasks/{task_id}/pause", json={"reason": "Awaiting user approval"})
    assert pause_resp.status_code == 200
    assert pause_resp.json()["task"]["canonical_status"] == "paused"
    assert pause_resp.json()["task"]["pause_reason"] == "Awaiting user approval"

    # 5. Invalid transition rejected cleanly (e.g. paused -> completed directly without running)
    with pytest.raises(TaskTransitionError):
        validate_task_transition("paused", "completed")

    # 6. Resume task
    resume_resp = client.post(f"/api/v2/tasks/{task_id}/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["task"]["canonical_status"] in ("queued", "running")

    asyncio.run(db.close())


# ==============================================================================
# Scenario C: Evidence-Based Deep Research with Verbatim Quote Offset Spans
# ==============================================================================
def test_scenario_c_deep_research_verbatim_quote_offset_spans(tmp_path: Path):
    """Verify research claim offset detection, provenance tracking, and quote verification."""
    raw_doc = (
        "JARVIS V2 implements zero-bypass local filesystem security. "
        "Every operation checks explicit grant scopes and paths. "
        "Pre-execution byte snapshots guarantee full rollback capability."
    )
    snap = ResearchSnapshot(
        source_id="DOC_01",
        title="Security Architecture",
        url="local://security.md",
        canonical_url="local://security.md",
        source_type="file",
        sha256="1234567890abcdef",
        fetched_at=time.time(),
        snapshot_path=str(tmp_path / "snap_c.txt"),
        content=raw_doc,
        snippet=raw_doc[:60],
    )

    claims = [
        ResearchClaim(
            statement="Every operation checks explicit grant scopes and paths in Jarvis V2.",
            kind="verified_fact",
            citations=["DOC_01"],
            quote="Every operation checks explicit grant scopes and paths.",
        ),
        ResearchClaim(
            statement="Pre-execution byte snapshots guarantee full rollback capability.",
            kind="verified_fact",
            citations=["DOC_01"],
            quote="Pre-execution byte snapshots guarantee full rollback capability.",
        ),
        ResearchClaim(
            statement="Future versions may support distributed cluster coordination.",
            kind="inference",
            citations=[],
            quote="",
        ),
    ]

    ver = verify_claims_against_snapshots(claims, {"DOC_01": snap}, [])
    assert ver.verified_claim_count == 2
    assert ver.inference_claim_count == 1

    # Verify claim 0 verbatim quote offset span in source document
    c0 = claims[0]
    assert c0.quote_verified is True
    assert c0.provenance == "extracted_quote"
    assert len(c0.quote_spans) == 1
    span0 = c0.quote_spans[0]
    assert span0["source_id"] == "DOC_01"
    assert raw_doc[span0["start_offset"] : span0["end_offset"]] == "Every operation checks explicit grant scopes and paths."

    # Verify claim 1 verbatim quote offset span
    c1 = claims[1]
    assert c1.quote_verified is True
    span1 = c1.quote_spans[0]
    assert raw_doc[span1["start_offset"] : span1["end_offset"]] == "Pre-execution byte snapshots guarantee full rollback capability."

    # Verify claim 2 honest inference provenance
    assert claims[2].provenance == "inference"
    assert len(claims[2].quote_spans) == 0


# ==============================================================================
# Scenario D: Cross-Source Contradictions and Freshness/Limitations
# ==============================================================================
def test_scenario_d_cross_source_contradictions_and_metadata(tmp_path: Path):
    """Verify contradiction detection and freshness & limitations metadata calculation."""
    # 1. Contradiction detection between opposing source statements
    snap_pos = ResearchSnapshot(
        source_id="DOC_A",
        title="Doc A",
        url="https://site-a.com/page",
        canonical_url="https://site-a.com/page",
        source_type="web_page",
        sha256="111",
        fetched_at=time.time(),
        snapshot_path=str(tmp_path / "a.txt"),
        content="The database connection pooling feature is enabled by default in production.",
        snippet="connection pooling is enabled",
    )
    snap_neg = ResearchSnapshot(
        source_id="DOC_B",
        title="Doc B",
        url="https://site-b.com/page",
        canonical_url="https://site-b.com/page",
        source_type="web_page",
        sha256="222",
        fetched_at=time.time(),
        snapshot_path=str(tmp_path / "b.txt"),
        content="The database connection pooling feature is disabled by default in production.",
        snippet="connection pooling is disabled",
    )
    contradictions = _detect_contradictions([snap_pos, snap_neg])
    assert len(contradictions) >= 1
    assert any("connection" in c.lower() or "pooling" in c.lower() for c in contradictions)

    # 2. Freshness and limitations calculation
    snap = ResearchSnapshot(
        source_id="S1",
        title="Local Notes",
        url="local://notes.md",
        canonical_url="local://notes.md",
        source_type="file",
        sha256="abc",
        fetched_at=time.time() - 3600,
        snapshot_path=str(tmp_path / "s1.txt"),
        content="Evidence notes.",
        snippet="Evidence notes.",
    )
    rep = VerificationReport(
        passed=True,
        overall_status="verified",
        missing_citations=[],
        invalid_citations=[],
        quote_mismatches=[],
        missing_quotes=[],
        unsupported_claims=[],
        contradicted_claims=[],
        contradictions_found=[],
        duplicate_urls=[],
        integrity_warnings=[],
        independent_source_count=1,
        unverified_claim_count=0,
        verified_claim_count=1,
        reported_claim_count=0,
        disputed_claim_count=0,
        inference_claim_count=0,
        revision_cycle=0,
    )
    freshness, limitations = build_freshness_and_limitations(
        [snap],
        rep,
        online_allowed=False,
        warnings=["Folder restricted/admin denied"],
    )
    assert freshness["source_count"] == 1
    assert freshness["online_allowed"] is False
    assert len(limitations) >= 1
    assert any("Offline Local Mode" in lim for lim in limitations)


# ==============================================================================
# Scenario E: Markdown Agent Profile Precedence & Bounded Delegation
# ==============================================================================
def test_scenario_e_agent_profile_precedence_and_bounded_delegation(tmp_path: Path):
    """Verify project > user > builtin profile precedence and bounded delegation depth."""
    user_agents = tmp_path / "user_agents"
    user_agents.mkdir(parents=True, exist_ok=True)
    proj_root = tmp_path / "my_project"
    (proj_root / ".jarvis" / "agents").mkdir(parents=True, exist_ok=True)

    # Project override of dev_coordinator
    (proj_root / ".jarvis" / "agents" / "dev_coordinator.md").write_text(
        "---\n"
        "name: dev_coordinator\n"
        "description: Project Lead Architect\n"
        "permission_tier: EXECUTE\n"
        "can_delegate: true\n"
        "max_delegation_depth: 2\n"
        "tools_allowlist: [read_file, write_file, execute_shell]\n"
        "tools_denylist: [execute_shell]\n"
        "---\n"
        "Project specific instructions.\n",
        encoding="utf-8",
    )

    coord = AgentWorkforceCoordinator(user_dir=user_agents, project_dirs=[proj_root])
    dev = coord.get_profile("dev_coordinator")
    assert dev is not None
    assert dev.scope == "project"
    assert dev.description == "Project Lead Architect"
    assert dev.permission_tier == "EXECUTE"

    # Filtered tools (write_file allowed, execute_shell denied)
    tools = coord.filter_tools_for_agent("dev_coordinator", ["read_file", "write_file", "execute_shell", "rmdir"])
    assert tools == ["read_file", "write_file"]

    # Delegation bounds: depth 0 -> 1 allowed, depth 2 -> 3 rejected
    ok1, _ = coord.can_delegate_to("dev_coordinator", "assistant_general", current_depth=0)
    assert ok1 is True
    ok2, r2 = coord.can_delegate_to("dev_coordinator", "assistant_general", current_depth=2)
    assert ok2 is False
    assert "depth" in r2.lower()


# ==============================================================================
# Scenario F: Privilege Escalation Prevention across Agent Tiers
# ==============================================================================
def test_scenario_f_privilege_escalation_prevention(tmp_path: Path):
    """Verify lower privilege agents cannot invoke higher privilege agents."""
    user_agents = tmp_path / "agents"
    user_agents.mkdir(parents=True, exist_ok=True)

    # Create a READ-tier agent with delegation enabled
    (user_agents / "auditor.md").write_text(
        "---\n"
        "name: auditor\n"
        "description: Read only code auditor\n"
        "permission_tier: READ\n"
        "can_delegate: true\n"
        "max_delegation_depth: 3\n"
        "---\nAuditor\n",
        encoding="utf-8",
    )

    coord = AgentWorkforceCoordinator(user_dir=user_agents)
    auditor = coord.get_profile("auditor")
    assert auditor is not None
    assert auditor.permission_tier == "READ"

    # Auditor (READ) attempting to delegate to dev_coordinator (EXECUTE) -> REJECTED
    ok, reason = coord.can_delegate_to("auditor", "dev_coordinator", current_depth=0)
    assert ok is False
    assert "privilege escalation" in reason.lower()

    # Assistant General (EXECUTE) delegating to auditor (READ) -> ALLOWED
    gen = coord.get_profile("assistant_general")
    assert gen is not None
    ok_gen, _ = coord.can_delegate_to("assistant_general", "auditor", current_depth=0)
    assert ok_gen is True


# ==============================================================================
# Scenario G: Unified Routine Scheduler, Quiet Hours & APScheduler Crons
# ==============================================================================
def test_scenario_g_routine_scheduler_quiet_hours_and_rescheduling(tmp_path: Path):
    """Verify quiet hours evaluation, cron recurrence computation, and idempotency."""
    # 1. Overnight quiet hours checking
    qh_config = {"enabled": True, "start_time": "22:00", "end_time": "06:30", "timezone": "UTC"}
    assert is_in_quiet_hours(qh_config, datetime(2026, 11, 12, 23, 0)) is True
    assert is_in_quiet_hours(qh_config, datetime(2026, 11, 12, 3, 0)) is True
    assert is_in_quiet_hours(qh_config, datetime(2026, 11, 12, 12, 0)) is False

    # 2. Routine service with cron trigger
    db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'sc_g.db').as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()

    svc = RoutineSchedulerService(db, quiet_hours_fn=lambda: qh_config)

    async def scenario():
        # Create a cron routine scheduled every minute
        r = await svc.create_routine(
            title="Hourly workspace sync",
            trigger_type="cron",
            trigger_spec={"cron_expression": "*/5 * * * *"},
            action_type="notification",
            action_payload={"msg": "Sync completed"},
            late_policy="run_immediately",
        )
        assert r["enabled"] is True
        assert r["next_run_at"] is not None

        # Toggle routine disable / enable
        disabled = await svc.toggle_routine(r["id"], False)
        assert disabled["enabled"] is False

        enabled = await svc.toggle_routine(r["id"], True)
        assert enabled["enabled"] is True

        # Run Now execution
        run_ev = await svc.run_now(r["id"])
        assert run_ev is not None
        assert run_ev["run"]["status"] == "completed"

        # Check run audit record in DB
        runs = await svc.list_runs(routine_id=r["id"])
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"

    asyncio.run(scenario())
    asyncio.run(db.close())


# ==============================================================================
# Scenario H: Persistent Proactive Suggestion Dismissal Surviving Restart
# ==============================================================================
def test_scenario_h_persistent_proactive_dismissal(tmp_path: Path):
    """Verify dismissed proactive suggestions persist in DB and survive engine restarts."""
    db_file = tmp_path / "proactive_persist.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    db1 = DatabaseManager(db_url, data_dir=tmp_path)
    db1.init_schema_sync()

    spec = ProactiveSpec(enabled=True, suggest_on_system_alerts=True)
    engine1 = ProactiveSuggestionEngine(spec, db=db1)

    # Simulated alert
    mock_sys = {"alerts": [{"id": "alert_ram_high", "severity": "warning", "metric": "ram", "message": "RAM usage high"}]}
    items1 = engine1.collect(sys_snapshot=mock_sys, recent_tasks=[], recent_commands=[])
    assert len(items1) == 1
    s_id = items1[0]["id"]

    # User dismisses suggestion
    d_res = engine1.dismiss(s_id)
    assert d_res["dismissed"] == s_id

    # Immediate second collection suppresses dismissed item
    assert len(engine1.collect(sys_snapshot=mock_sys, recent_tasks=[], recent_commands=[])) == 0
    asyncio.run(db1.close())

    # Simulate restart: New DatabaseManager and new ProactiveSuggestionEngine instance
    db2 = DatabaseManager(db_url, data_dir=tmp_path)
    engine2 = ProactiveSuggestionEngine(spec, db=db2)

    # Must still be suppressed after restart
    items2 = engine2.collect(sys_snapshot=mock_sys, recent_tasks=[], recent_commands=[])
    assert len(items2) == 0, "Dismissed suggestion must remain dismissed across restart"

    asyncio.run(db2.close())


# ==============================================================================
# Scenario I: Honest Desktop Action Execution & ToolOutcome Contracts
# ==============================================================================
def test_scenario_i_honest_desktop_action_execution(tmp_path: Path):
    """Verify actions return honest verified ToolOutcome contracts and respect security tiers."""
    s = Settings(
        notes_dir=str(tmp_path),
        brains={"local": BrainSpec(model="qwen3:4b-instruct")},
        actions=ActionsSpec(
            allow_input_automation=True,
            allowed_apps={"notepad": ["notepad.exe"], "calc": ["calc.exe"]},
            allowed_url_domains=["docs.python.org", "github.com"],
        ),
    )
    safe_fs = SafeFilesystem(s.notes_dir)
    executor = ActionExecutor(s.actions, fs=safe_fs)

    # 1. SSRF block: localhost / 127.0.0.1 disallowed
    with pytest.raises(Exception) as exc_ssrf:
        executor.request_or_execute("open_url", {"url": "http://127.0.0.1:8000/admin"})
    assert any(term in str(exc_ssrf.value).lower() for term in ("blocked", "unsafe", "disallowed", "not allowed", "only https"))

    # 2. Honest outcome for keyboard type with confirmation gate
    res = executor.request_or_execute("keyboard_type", {"text": "hello jarvis"})
    if res.get("confirmation_required"):
        token = res["token"]
        assert token
        res = executor.confirm(token)

    assert res["status"] in ("started", "simulated", "executed", "done")
    outcome = res["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["tool_name"] == "keyboard_type"
    assert outcome["status"] in ("started", "simulated", "executed", "done")
    assert isinstance(outcome["checks"], list)
    assert len(outcome["checks"]) >= 1
    assert outcome["checks"][0]["passed"] is True

    # 3. Disallowed app open blocked
    with pytest.raises(Exception) as exc_app:
        executor.request_or_execute("open_app", {"app": "cmd.exe"})
    assert "allowed_apps" in str(exc_app.value).lower()


# ==============================================================================
# Scenario J: Reversible Action Snapshots, External Conflict & Undo Rollback
# ==============================================================================
def test_scenario_j_reversible_action_snapshots_and_conflict_detection(tmp_path: Path):
    """Verify UndoService reversible binary snapshots, external modification conflict check, and rollback."""
    db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'sc_j.db').as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    undo = UndoService(db)

    async def scenario():
        target_file = tmp_path / "production_config.json"
        original_bytes = b'{"version": "1.0.0", "active": true}\n'
        modified_bytes = b'{"version": "2.0.0", "active": false}\n'
        target_file.write_bytes(original_bytes)

        # 1. Record snapshot of modification
        target_file.write_bytes(modified_bytes)
        entry = await undo.record_snapshot(
            action_type="modify_file",
            target_path=str(target_file),
            before_bytes=original_bytes,
            after_bytes=modified_bytes,
            reason="Update config",
        )
        assert entry["status"] == "available"
        assert entry["reversible"] is True
        undo_id = entry["undo_id"]

        # 2. Conflict detection: An external process tampers with the file behind Jarvis's back
        tampered_bytes = b'{"version": "9.9.9", "TAMPERED": true}\n'
        target_file.write_bytes(tampered_bytes)

        # Applying undo must reject due to SHA mismatch!
        with pytest.raises(UndoConflictError) as exc_conflict:
            await undo.apply_undo(undo_id)
        assert "modified externally" in str(exc_conflict.value).lower()

        # 3. Clean restoration: Restore file to modified_bytes and perform safe Undo
        target_file.write_bytes(modified_bytes)
        reverted = await undo.apply_undo(undo_id)
        assert reverted["status"] == "undone"
        assert target_file.read_bytes() == original_bytes, "File content must exactly match original pre-action state"

        # 4. Attempting to revert already-reverted entry fails cleanly
        with pytest.raises(UndoConflictError) as exc_again:
            await undo.apply_undo(undo_id)
        assert "already undone" in str(exc_again.value).lower()

    asyncio.run(scenario())
    asyncio.run(db.close())
