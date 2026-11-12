"""Phase P0 Architecture & Contract Foundation verification tests."""
import asyncio
import json
import logging

from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.core.contracts import (
    ActionExecutionContract,
    ActionOutcomeStatus,
    AgentProfileContract,
    ErrorEnvelopeContract,
    LatePolicy,
    ResearchReportContract,
    RoutineContract,
    RoutineTriggerType,
    TaskRecordContract,
    TaskStatusCanonical,
    normalize_task_status,
)
from jarvis.core.errors import ErrorEnvelope, TaskTransitionError, UndoConflictError
from jarvis.core.logging_ctx import (
    CorrelationAndRedactionFilter,
    StructuredJsonFormatter,
    correlation_scope,
    get_correlation_context,
    sanitize_log_text,
)
from jarvis.skills.compatibility import inspect_runtime_compatibility  # noqa: F401
from jarvis.skills.registry import SkillRegistryService
from jarvis.skills.router import AgentCapabilityRouter

H = {"X-Jarvis": "1"}


def test_p0_domain_contracts_and_status_normalization():
    assert normalize_task_status("planning") == TaskStatusCanonical.QUEUED
    assert normalize_task_status("awaiting_permission") == TaskStatusCanonical.WAITING
    assert normalize_task_status("awaiting_approval") == TaskStatusCanonical.WAITING
    assert normalize_task_status("completed") == TaskStatusCanonical.DONE
    assert normalize_task_status("scheduled") == TaskStatusCanonical.SCHEDULED
    assert normalize_task_status("paused") == TaskStatusCanonical.PAUSED

    task = TaskRecordContract(task_id="t-1", user_request="Check repo", status="completed", canonical_status=normalize_task_status("completed"))
    assert task.canonical_status == TaskStatusCanonical.DONE

    act = ActionExecutionContract(action_type="create_file", status=ActionOutcomeStatus.DONE, verified=True)
    assert act.status == ActionOutcomeStatus.DONE

    rep = ResearchReportContract(task_id="r-1", question="What is WAL?")
    assert rep.stage.value == "COMPLETE"

    agent = AgentProfileContract(name="researcher", description="Read-only research worker")
    assert agent.model_role == "fast"

    routine = RoutineContract(routine_id="rt-1", title="Morning Brief", trigger_type=RoutineTriggerType.DAILY, cron_or_time="08:00", late_policy=LatePolicy.SKIP)
    assert routine.trigger_type == RoutineTriggerType.DAILY

    err = ErrorEnvelopeContract(error="boom", code="bad_request", correlation_id="req-123")
    assert err.correlation_id == "req-123"


def test_p0_correlation_logging_and_secret_redaction():
    raw = "GET https://api.example.com/v1?api_key=SECRETVALUE123 auth Bearer tok-abc1234567890 sk-ant-api03-abcdef1234567890"
    cleaned = sanitize_log_text(raw)
    assert "SECRETVALUE123" not in cleaned
    assert "tok-abc1234567890" not in cleaned
    assert "sk-ant-api03-abcdef1234567890" not in cleaned

    rec = logging.LogRecord(
        name="jarvis.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Calling key=%s url=%s",
        args=("sk-proj-secret1234567890123456", "https://svc.test/run?token=topsecret99"),
        exc_info=None,
    )
    filt = CorrelationAndRedactionFilter()
    with correlation_scope(correlation_id="corr-xyz", task_id="task-9", routine_id="rt-4", agent_id="planner"):
        assert filt.filter(rec) is True
        ctx = get_correlation_context()
        assert ctx["correlation_id"] == "corr-xyz"
        assert ctx["task_id"] == "task-9"
        fmt = StructuredJsonFormatter()
        line = fmt.format(rec)
        parsed = json.loads(line)
        assert parsed["correlation_id"] == "corr-xyz"
        assert parsed["task_id"] == "task-9"
        assert parsed["routine_id"] == "rt-4"
        assert parsed["agent_id"] == "planner"
        assert "sk-proj-secret1234567890123456" not in line
        assert "topsecret99" not in line


def test_p0_error_hierarchy_and_http_correlation_and_envelope(settings):
    with correlation_scope(correlation_id="req-fixed-1"):
        e1 = TaskTransitionError("cannot jump paused -> done", details={"from": "paused", "to": "done"})
        assert e1.status_code == 409 and e1.code == "invalid_task_transition"
        env = ErrorEnvelope(str(e1), code=e1.code, details=e1.details).to_dict(include_detail=True)
        assert env == {
            "error": "cannot jump paused -> done",
            "detail": "cannot jump paused -> done",
            "code": "invalid_task_transition",
            "correlation_id": "req-fixed-1",
            "details": {"from": "paused", "to": "done"},
        }
        e2 = UndoConflictError("file changed externally")
        assert e2.status_code == 409 and e2.code == "undo_conflict"

    app = create_app(settings)
    with TestClient(app) as client:
        ok = client.get("/api/health", headers={"X-Correlation-ID": "corr-p0-check"})
        assert ok.status_code == 200
        assert ok.headers.get("x-correlation-id") == "corr-p0-check"

        missing = client.get("/api/v2/tasks/nonexistent-task-id", headers={"X-Correlation-ID": "corr-p0-404"})
        assert missing.status_code == 404
        body = missing.json()
        assert body["code"] == "not_found"
        assert body["correlation_id"] == "corr-p0-404"
        assert body["error"] and body["detail"]
        assert missing.headers.get("x-correlation-id") == "corr-p0-404"

        bad_val = client.post("/api/chat", json={"message": ""}, headers={**H, "X-Correlation-ID": "corr-p0-422"})
        assert bad_val.status_code == 422
        vbody = bad_val.json()
        assert vbody["code"] == "validation_error"
        assert vbody["correlation_id"] == "corr-p0-422"


def test_p0_capability_router_method_contract(tmp_path):
    from jarvis.db.engine import DatabaseManager

    db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'p0.db').as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    reg = SkillRegistryService(db, skills_root=tmp_path / "skills")
    router = AgentCapabilityRouter(reg)
    res = router.select_skills_for_task(task_prompt="Review API architecture and debug tests", intent="dev_review", department="dev")
    assert isinstance(res, dict) and "selected" in res
    asyncio.run(db.close())

