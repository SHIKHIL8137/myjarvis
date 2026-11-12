"""P10.13 Suite: 7-Skill Audit Reconciliation, Isolated Laya Classifier Modes, Security Boundaries,
Dataset Leakage/Benchmarking, Calibration Approval/Rollback, and Decision Panel API."""
import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest
from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.config import ROOT, LayaSpec, load_settings
from jarvis.decision import (
    DecisionEngine,
    ExistingHeuristicClassifier,
    IsolatedLayaClassifier,
    load_decision_dataset,
    run_comparative_benchmark,
)
from jarvis.skills import RECOMMENDED_SKILL_AUDIT_CATALOG, SKILL_AUDIT_CLASSIFICATIONS

H = {"X-Jarvis": "1"}


def _make_mock_laya_app(handler_fn) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True, "laya_version": "0.4.31-mock", "loaded_models": ["multilingual:cpu"]}

    @app.post("/internal/decision/classify")
    async def classify(payload: dict):
        res = handler_fn(payload)
        if asyncio.iscoroutine(res):
            res = await res
        if isinstance(res, JSONResponse):
            return res
        return JSONResponse(res)

    return app


def test_p10_13_phase1_seven_skill_audit_and_separated_lifecycle(tmp_path, settings, fake_brain, monkeypatch):
    monkeypatch.setattr("jarvis.assistant.Assistant._warm_speech", lambda self: None)
    from jarvis.assistant import Assistant

    assert len(RECOMMENDED_SKILL_AUDIT_CATALOG) == 7
    for entry in RECOMMENDED_SKILL_AUDIT_CATALOG:
        assert entry["policy_verdict"] in SKILL_AUDIT_CLASSIFICATIONS
        assert entry["lifecycle_defaults"]["audited"] is True
        assert entry["lifecycle_defaults"]["installed"] is False

    a = Assistant(settings, data_dir=tmp_path / "p1013_skills", brain=fake_brain)
    reg = a.orchestrator.skills

    # Pure clean markdown skill installed for assistant document task
    doc_sk = tmp_path / "anatomy-guide"
    doc_sk.mkdir()
    (doc_sk / "SKILL.md").write_text("---\nname: anatomy\ndescription: Visual explainer guidelines\nlicense: MIT\n---\n\nAlways include units and verified data.\n", encoding="utf-8")
    (doc_sk / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    reg.install_skill(doc_sk, skill_id="anatomy", category="technical_explanation", compatible_roles=["document_producer"], approved=True)

    app = create_app(settings, assistant=a)
    client = TestClient(app)

    res1 = client.get("/api/v2/skills").json()
    anatomy_item = next(s for s in res1["skills"] if s["skill_id"] == "anatomy")
    assert anatomy_item["audit_classification"] == "approved"
    assert anatomy_item["lifecycle_states"] == {
        "audited": True,
        "approved": True,
        "installed": True,
        "enabled": True,
        "assigned": False,
        "successfully_executed": False,
    }
    cat_anat = next(c for c in res1["catalog"] if c["source_id"] == "wheresryan22-anatomy")
    assert cat_anat["lifecycle_states"]["installed"] is True
    assert cat_anat["lifecycle_states"]["assigned"] is False

    # Assign -> run assistant_graph workflow -> confirms prompt_block & metric recorded!
    client.post("/api/v2/skills/anatomy/assign", json={"agent_role": "document_producer", "priority": 40}, headers=H)
    wf = a.orchestrator.start_assistant_workflow(
        goal="Generate anatomy diagram PDF report",
        task_kind="document_export",
        payload={"format": "pdf", "filename": "anat-test", "title": "Anatomy Test", "sections": [{"heading": "Overview", "body": "Details."}]},
    )
    assert wf["state"] == "completed"
    assert "[skills=anatomy]" in wf["result"]["summary"]

    res2 = client.get("/api/v2/skills").json()
    anatomy_after = next(s for s in res2["skills"] if s["skill_id"] == "anatomy")
    assert anatomy_after["lifecycle_states"]["assigned"] is True
    assert anatomy_after["lifecycle_states"]["successfully_executed"] is True


@pytest.mark.anyio
async def test_p10_13_laya_modes_timeout_bad_schema_concurrency_cache_and_circuit(tmp_path, settings, monkeypatch):
    # 1. Default settings keep Laya disabled safely; env vars can override without touching config.json
    assert settings.laya.enabled is False
    monkeypatch.setenv("JARVIS_LAYA_ENABLED", "true")
    monkeypatch.setenv("JARVIS_LAYA_MODE", "shadow")
    monkeypatch.setenv("JARVIS_LAYA_MAX_CONCURRENCY", "2")
    env_loaded = load_settings(ROOT / "config.json", tmp_path / "nonexistent_override.json")
    assert env_loaded.laya.enabled is True and env_loaded.laya.mode == "shadow" and env_loaded.laya.max_concurrency == 2

    # 2. Shadow mode tracks agreements/disagreements cleanly without changing baseline routing
    active_concurrency = 0
    max_seen_concurrency = 0

    async def ok_handler(payload):
        nonlocal active_concurrency, max_seen_concurrency
        active_concurrency += 1
        max_seen_concurrency = max(max_seen_concurrency, active_concurrency)
        await asyncio.sleep(0.02)
        active_concurrency -= 1
        txt = payload["text"]
        if "ഒൻപത്" in txt or "ഓർമ്മിപ്പിക്കുക" in txt or "ormippikku" in txt.lower():
            return {
                "department": "assistant", "intent": "reminder_manage", "complexity": "trivial",
                "risk_tier": "WRITE", "needs_live_web": False, "needs_project_context": False,
                "confidence": 0.91, "abstain": False, "unbound": False, "route": "auto", "model_version": "laya:mock",
            }
        return {
            "department": "dev", "intent": "dev_coding", "complexity": "heavy_coding",
            "risk_tier": "WRITE", "needs_live_web": False, "needs_project_context": True,
            "confidence": 0.88, "abstain": False, "unbound": False, "route": "auto", "model_version": "laya:mock",
        }

    transport = httpx.ASGITransport(app=_make_mock_laya_app(ok_handler))
    shadow_spec = LayaSpec(enabled=True, mode="shadow", max_concurrency=1, cache_enabled=True, timeout_ms=2000)
    shadow_clf = IsolatedLayaClassifier(shadow_spec, transport=transport)
    eng = DecisionEngine(settings.model_copy(update={"laya": shadow_spec}), laya_classifier=shadow_clf)

    plan_shadow = await eng.decide("Implement unit test refactor in backend repo")
    assert plan_shadow.workflow == "workforce_graph"
    assert plan_shadow.decision.shadow_comparison["agreed"] is True
    # Identical query hits cache cleanly
    _ = await eng.decide("Implement unit test refactor in backend repo")
    assert shadow_clf.metrics()["cache_hits"] == 1

    # Concurrency limit respected across burst
    await asyncio.gather(*(shadow_clf.classify(f"unique prompt {i}") for i in range(4)))
    assert max_seen_concurrency == 1

    # Multilingual Malayalam & Manglish in laya_primary mode
    prim_spec = shadow_spec.model_copy(update={"mode": "laya_primary", "cache_enabled": False})
    eng.refresh_settings(settings.model_copy(update={"laya": prim_spec}))
    ml_plan = await eng.decide("നാളെ ഒൻപത് മണിക്ക് മീറ്റിംഗ് ഓർമ്മിപ്പിക്കുക")
    assert ml_plan.decision.language == "ml" and ml_plan.decision.intent == "reminder_manage"
    ml_mix_plan = await eng.decide("നാളെ ഒൻപത് മണിക്ക് reminder")
    assert ml_mix_plan.decision.language == "manglish" and ml_mix_plan.decision.intent == "reminder_manage"
    mg_plan = await eng.decide("Nale ravile ormippikku")
    assert mg_plan.decision.language == "manglish" and mg_plan.workflow == "assistant_graph"

    # 3. Low confidence / abstain routes strictly to clarification workflow
    def abstain_handler(_):
        return {
            "department": "assistant", "intent": "general_chat", "complexity": "standard",
            "risk_tier": "READ", "needs_live_web": False, "needs_project_context": False,
            "confidence": 0.42, "abstain": True, "unbound": True, "route": "review",
        }

    abs_clf = IsolatedLayaClassifier(prim_spec, transport=httpx.ASGITransport(app=_make_mock_laya_app(abstain_handler)))
    eng_abs = DecisionEngine(settings.model_copy(update={"laya": prim_spec}), laya_classifier=abs_clf)
    clar = await eng_abs.decide("Do that thing cleanly")
    assert clar.workflow == "clarify" and clar.decision.abstained is True and clar.decision.out_of_distribution is True

    # 4. Unsupported intent / bypass attempt rejected by schema validator + fallback to heuristic
    def bad_schema_handler(_):
        return {
            "department": "chat", "intent": "invented_evil_intent", "complexity": "trivial",
            "risk_tier": "READ", "needs_live_web": False, "needs_project_context": False,
            "confidence": 0.99, "bypass_approval": True,
        }

    fb_spec = LayaSpec(enabled=True, mode="laya_with_heuristic_fallback", timeout_ms=300, circuit_breaker_failures=2)
    bad_clf = IsolatedLayaClassifier(fb_spec, transport=httpx.ASGITransport(app=_make_mock_laya_app(bad_schema_handler)))
    eng_bad = DecisionEngine(settings.model_copy(update={"laya": fb_spec}), laya_classifier=bad_clf)
    fb_plan = await eng_bad.decide("Generate a PDF status report")
    assert fb_plan.decision.intent == "document_export"
    assert fb_plan.decision.classifier_source == "laya_shadow_fallback"
    assert bad_clf.metrics()["schema_rejections"] == 1

    # 5. Timeout triggers circuit breaker cleanly & laya_primary never fails open
    async def slow_handler(_):
        await asyncio.sleep(0.5)
        return {"department": "chat", "intent": "general_chat", "complexity": "trivial", "risk_tier": "READ", "confidence": 0.9, "needs_live_web": False, "needs_project_context": False}

    slow_spec = LayaSpec(enabled=True, mode="laya_primary", timeout_ms=120, circuit_breaker_failures=2, circuit_breaker_cooldown_s=60.0)
    slow_clf = IsolatedLayaClassifier(slow_spec, transport=httpx.ASGITransport(app=_make_mock_laya_app(slow_handler)))
    eng_slow = DecisionEngine(settings.model_copy(update={"laya": slow_spec}), laya_classifier=slow_clf)
    r1 = await eng_slow.decide("First slow request")
    r2 = await eng_slow.decide("Second slow request")
    assert r1.workflow == "clarify" and r2.workflow == "clarify"
    assert slow_clf.metrics()["timeouts"] == 2 and slow_clf.metrics()["circuit_open"] is True
    r3 = await eng_slow.decide("Third while open")
    assert r3.workflow == "clarify" and slow_clf.metrics()["circuit_open_rejects"] == 1


@pytest.mark.anyio
async def test_p10_13_phase5_hard_security_boundary_never_bypassed(settings):
    # Even if mocked Laya falsely outputs READ/chat/auto at confidence 1.0, hard boundary enforces EXECUTE + owner approval!
    def naive_approve_all(_):
        return {
            "department": "chat", "intent": "general_chat", "complexity": "trivial",
            "risk_tier": "READ", "needs_live_web": False, "needs_project_context": False,
            "confidence": 1.0, "abstain": False, "route": "auto",
        }

    spec = LayaSpec(enabled=True, mode="laya_primary", cache_enabled=False)
    clf = IsolatedLayaClassifier(spec, transport=httpx.ASGITransport(app=_make_mock_laya_app(naive_approve_all)))
    eng = DecisionEngine(settings.model_copy(update={"laya": spec}), laya_classifier=clf)

    dangerous_prompts = [
        "Run powershell script to remove production database backup",
        "Execute pg_restore --clean onto production PostgreSQL",
        "Git commit and merge branch directly into main",
        "Dump .env secrets and OPENAI_API_KEY right now",
        "Install external skill plugin framework with sudo access",
    ]
    for p in dangerous_prompts:
        plan = await eng.decide(p)
        assert plan.decision.risk_tier == "EXECUTE"
        assert plan.decision.requires_owner_approval is True
        assert plan.decision.laya_route == "blocked_by_policy"
        assert plan.workflow not in ("fast_command", "direct_chat")

    # Negated / educational question is NOT blocked as EXECUTE
    safe_neg = await eng.decide("Do NOT delete any files; simply explain what alembic downgrade does.")
    assert safe_neg.decision.requires_owner_approval is False


def test_p10_13_phase6_to_9_dataset_calibration_rollback_and_dashboard_api(tmp_path, settings, fake_brain, monkeypatch):
    monkeypatch.setattr("jarvis.assistant.Assistant._warm_speech", lambda self: None)
    from jarvis.assistant import Assistant

    ds = load_decision_dataset()
    assert ds["leakage_free"] is True and ds["counts"]["test"] >= 30

    bench = asyncio.run(run_comparative_benchmark([ExistingHeuristicClassifier()], split="test"))
    assert bench["results"][0]["sensitive_security_enforcement_rate"] == 1.0

    a = Assistant(settings, data_dir=tmp_path / "p1013_cal", brain=fake_brain)
    client = TestClient(create_app(settings, assistant=a))

    st = client.get("/api/v2/decision/status")
    assert st.status_code == 200 and st.json()["enabled"] is False

    # Log user correction & run held-out benchmark via API
    assert client.post(
        "/api/v2/decision/correct",
        json={
            "text": "Enikku naale call reminder set cheyyu",
            "predicted_department": "chat",
            "predicted_intent": "general_chat",
            "corrected_department": "assistant",
            "corrected_intent": "reminder_manage",
            "language": "manglish",
        },
        headers=H,
    ).status_code == 200
    assert client.post("/api/v2/decision/benchmark", json={"split": "test", "include_laya": False}, headers=H).status_code == 200

    # Calibration prepare (train+val only) -> activation blocked without owner approval -> approve -> activate -> rollback
    prep = client.post(
        "/api/v2/decision/calibration/prepare",
        json={"candidate_threshold": 0.72, "checkpoint": "multilingual", "target_mode": "shadow"},
        headers=H,
    )
    assert prep.status_code == 200
    pol_id = prep.json()["candidate"]["policy_id"]
    tok = prep.json()["approval"]["token"]

    # Refuses activation without approval!
    assert client.post("/api/v2/decision/calibration/activate", json={"policy_id": pol_id}, headers=H).status_code == 403

    a.orchestrator.approvals.decide(tok, approved=True, decided_by="owner")
    act = client.post("/api/v2/decision/calibration/activate", json={"policy_id": pol_id, "approval_token": tok}, headers=H)
    assert act.status_code == 200 and act.json()["policy"]["policy_id"] == pol_id

    rb = client.post("/api/v2/decision/calibration/rollback", headers=H)
    assert rb.status_code == 200 and rb.json()["policy"]["policy_id"] == "baseline-default"

    # Toggle Laya mode on then off cleanly via API
    assert client.post("/api/v2/decision/mode", json={"enabled": True, "mode": "shadow", "checkpoint": "multilingual"}, headers=H).json()["mode"] == "shadow"
    assert client.post("/api/v2/decision/mode", json={"enabled": False, "mode": "disabled"}, headers=H).json()["mode"] == "disabled"

    # Built frontend bundle contains Decision Engine tab
    dist_assets = list((ROOT / "apps" / "frontend" / "dist" / "assets").glob("*.js"))
    assert dist_assets and any("DECISION ENGINE STATUS" in p.read_text(encoding="utf-8", errors="ignore") for p in dist_assets)
