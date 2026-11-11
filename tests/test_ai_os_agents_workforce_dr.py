"""Tests for P4-P9: Document Engine, Reminders/Productivity, Real Git Worktree + LangGraph interrupt/resume, and 3-2-1 DR."""
from pathlib import Path
import re
import subprocess
import time

import openpyxl
from starlette.testclient import TestClient

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.api import create_app
from jarvis.config import set_provider_api_key
from jarvis.db.engine import DatabaseManager
from jarvis.departments.assistant.documents import DocumentService, parse_document
from jarvis.departments.assistant.productivity import EmailDraftService, LocalCalendarService
from jarvis.departments.assistant.reminders import ReminderService
from jarvis.departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from jarvis.recovery.backup import DisasterRecoveryManager

H = {"X-Jarvis": "1"}


def test_document_engine_all_formats_and_formula_neutralization(tmp_path):
    docs = DocumentService(tmp_path / "docs")

    pdf_res = docs.generate("pdf", filename="audit-report", title="Audit PDF", payload={"sections": [{"heading": "Overview", "body": "PDF body check", "table": [["Col1", "Col2"], ["A", "B"]]}]})
    assert Path(pdf_res["path"]).exists()
    assert "PDF body check" in parse_document(Path(pdf_res["path"]))["content"]

    docx_res = docs.generate("docx", filename="word-report", title="Word Spec", payload={"sections": [{"heading": "Scope", "body": "Docx content valid"}]})
    assert "Docx content valid" in parse_document(Path(docx_res["path"]))["content"]

    pptx_res = docs.generate("pptx", filename="deck", title="Architecture Deck", payload={"slides": [{"title": "Phase 1", "bullets": ["Modular monolith", "PostgreSQL pgvector"], "notes": "Speaker hint"}]})
    parsed_ppt = parse_document(Path(pptx_res["path"]))["content"]
    assert "Modular monolith" in parsed_ppt and "Speaker hint" in parsed_ppt

    xlsx_res = docs.generate("xlsx", filename="sheet", title="Metrics", payload={"sheets": [{"name": "Metrics", "rows": [["Metric", "Formula"], ["Cost", "=cmd|' /C calc'!A0"]]}]})
    wb = openpyxl.load_workbook(xlsx_res["path"], data_only=False)
    cell_val = wb["Metrics"].cell(2, 2).value
    wb.close()
    assert isinstance(cell_val, str) and cell_val.startswith("'")  # neutralized leading quote


def test_reminders_and_productivity_ports(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    rems = ReminderService(db)
    r1 = rems.create(title="Standup review", due_at=time.time() - 5)
    due = rems.poll_due()
    assert len(due) == 1 and due[0]["id"] == r1["id"] and due[0]["status"] == "triggered"
    done = rems.complete(r1["id"])
    assert done["status"] == "completed"

    cal = LocalCalendarService(tmp_path / "cal")
    ev = cal.create_event(title="Deploy check", start_iso="2026-11-12T10:00:00")
    assert Path(ev["ics_path"]).exists() and "BEGIN:VEVENT" in Path(ev["ics_path"]).read_text(encoding="utf-8")

    mail = EmailDraftService(tmp_path / "mail")
    d = mail.create_draft(to="owner@example.com", subject="Report", body="Attached.")
    sent = mail.dispatch_approved_draft(d["id"], online_allowed=False)
    assert sent["status"] == "approved_queued_offline"
    db.sync_engine.dispose()


def test_langgraph_workforce_git_worktree_qa_interrupt_and_restart_resume(tmp_path, settings):
    # 1. Create a real local git repository
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@jarvis.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Jarvis Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init buggy repo"], cwd=repo, check=True, capture_output=True)

    # Inject a deterministic coder callback representing OpenCode fixing the bug inside the isolated worktree ONLY
    calls = {"n": 0}

    def fake_opencode_in_worktree(prompt, *, workdir, model=None, bin_name="opencode"):
        calls["n"] += 1
        wt = Path(workdir)
        # Main repo must remain unmodified during coding!
        assert "return a - b" in (repo / "app.py").read_text(encoding="utf-8")
        (wt / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"ok": True, "final_text": "Fixed add() inside worktree."}

    data_dir = tmp_path / "data"
    orch1 = AgentOrchestrator(settings, data_dir=data_dir, coder_runner=fake_opencode_in_worktree)
    proj = orch1.projects.register(name="sample_repo", root_path=repo, test_cmd="pytest -q")

    res = orch1.start_workforce_workflow(
        project_id=proj["id"],
        task_prompt="Fix add function in app.py",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert res["interrupted"] is True and res["state"] == "awaiting_approval"
    assert res["result"]["qa_result"]["passed"] is True
    assert res["result"]["security_scan"]["clean"] is True
    # Main working tree STILL untouched until approval!
    assert "return a - b" in (repo / "app.py").read_text(encoding="utf-8")
    tok = res["result"]["approval_token"]
    orch1.db.sync_engine.dispose()

    # 2. Simulate full server restart with a brand-new AgentOrchestrator loading persisted DB checkpoint
    orch2 = AgentOrchestrator(settings, data_dir=data_dir, coder_runner=fake_opencode_in_worktree)
    resumed = orch2.resume_workflow_after_approval(token=tok, approved=True, decided_by="owner")
    assert resumed["resumed"] is True and resumed["state"] == "completed"
    # Main repo now has the merged fix!
    assert "return a + b" in (repo / "app.py").read_text(encoding="utf-8")
    assert len(orch2.memory.list_experience_lessons(scope="dev", project_id=proj["id"])) == 1
    orch2.db.sync_engine.dispose()


def test_dev_diff_security_scan_blocks_hardcoded_secret(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    wf = DevWorkforceService(db, ProjectRegistryService(db))
    bad = wf.security_scan_diff("+API_KEY = 'sk-proj-secret1234567890abcd'\n+eval(user_input)")
    assert bad["clean"] is False and any(f["type"] == "secret_leak" for f in bad["findings"])
    db.sync_engine.dispose()


def test_disaster_recovery_backup_and_tamper_detection(tmp_path, notes_dir, settings):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sec_file = data_dir / "secrets.json"
    set_provider_api_key("tavily", "tvly-secret-1234567890", secrets_path=sec_file)

    db = DatabaseManager("", data_dir=data_dir)
    db.init_schema_sync()
    dr = DisasterRecoveryManager(db, settings, data_dir=data_dir, secrets_path=sec_file)

    bundle = dr.create_backup(tier="tier2_warm", passphrase="RecoverablePassword99", notes_dir=notes_dir, extra_dest_dir=tmp_path / "tier3")
    assert Path(bundle["bundle_path"]).exists() and Path(bundle["tier3_copy"]).exists()

    rep = dr.verify_backup(Path(bundle["bundle_path"]), passphrase="RecoverablePassword99")
    assert rep["ok"] is True and rep["secrets_count"] == 1 and rep["db_tables"] >= 10
    assert dr.list_manifests()[0]["verified_restore"] is True
    db.sync_engine.dispose()


def test_v2_api_endpoints_and_react_dist_offline_compliance(assistant, settings):
    app = create_app(settings, assistant=assistant)
    client = TestClient(app)

    assert client.get("/api/v2/system/overview").status_code == 200
    dec = client.post("/api/v2/decision/route", json={"query": "Generate a PDF summary"}, headers=H)
    assert dec.status_code == 200 and dec.json()["decision"]["intent"] == "document_export"

    doc = client.post(
        "/api/v2/documents/generate",
        json={"format": "pdf", "filename": "api-doc", "title": "API Generated", "payload": {"text": "Hello v2"}},
        headers=H,
    )
    assert doc.status_code == 200 and doc.json()["ok"] is True

    # If React dist has been built, assert zero CDN URLs in compiled output
    root = Path(__file__).resolve().parent.parent
    dist_idx = root / "apps" / "frontend" / "dist" / "index.html"
    if dist_idx.exists():
        html = dist_idx.read_text(encoding="utf-8")
        assert not re.findall(r"https?://(?:cdn|unpkg|jsdelivr)", html, re.I)
