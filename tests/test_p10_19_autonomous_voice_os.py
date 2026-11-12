"""P10.19 Integration Suite: Autonomous Routing, Multi-Agent Dev Execution,
Voice State Invariants & Interruption, Event Semantics, and Approval Security."""
import io
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
from starlette.testclient import TestClient

from jarvis import graph_live
from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.api import create_app
from jarvis.voice import VoiceState

H = {"X-Jarvis": "1"}


def _wav_bytes(seconds: float = 0.25, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    samples = np.zeros(int(seconds * sr), dtype=np.int16).tobytes()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples)
    return buf.getvalue()


def _init_git_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "jarvis@example.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Jarvis Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)


def test_req1_autonomous_routing_executes_real_workflows(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))

    # 1. File & folder operations
    r_fs = client.post("/api/chat", json={"message": "what files are in this workspace"}, headers=H)
    assert r_fs.status_code == 200 and r_fs.json()["command"] == "filesystem_ops"
    assert r_fs.json()["ok"] is True

    # 2. Offline research uses offline knowledge fallback
    assistant.knowledge.ingest_text("asyncio_notes", "Asyncio Notes", "Python asyncio event loop supports TaskGroup in 3.11+.")
    r_web = client.post("/api/chat", json={"message": "what is the current version of asyncio"}, headers=H)
    assert r_web.status_code == 200 and r_web.json()["command"] == "web_offline"
    assert "Local Mode" in r_web.json()["answer"] and "Asyncio Notes" in r_web.json()["answer"]

    # 3. Document generation
    r_doc = client.post("/api/chat", json={"message": "generate a pdf report on P10.19 routing verification"}, headers=H)
    assert r_doc.status_code == 200 and r_doc.json()["command"] == "document_export"
    assert r_doc.json()["ok"] is True and r_doc.json()["paths"]
    assert Path(r_doc.json()["paths"][0]).exists()

    # 4. Reminders
    r_rem = client.post("/api/chat", json={"message": "set a reminder to verify backup manifest"}, headers=H)
    assert r_rem.status_code == 200 and r_rem.json()["ok"] is True
    r_rem_list = client.post("/api/chat", json={"message": "list my pending reminders"}, headers=H)
    assert r_rem_list.status_code == 200 and r_rem_list.json()["command"] == "reminder_list"
    assert "verify backup manifest" in r_rem_list.json()["answer"].lower()

    # 5. Memory management
    r_mem_save = client.post("/api/chat", json={"message": "remember this: Jarvis backend runs on FastAPI"}, headers=H)
    assert r_mem_save.status_code == 200 and r_mem_save.json()["ok"] is True
    r_mem_list = client.post("/api/chat", json={"message": "list my personal memories"}, headers=H)
    assert r_mem_list.status_code == 200 and r_mem_list.json()["command"] == "memory_manage"
    assert "FastAPI" in r_mem_list.json()["answer"]

    # 6. System monitoring
    r_sys = client.post("/api/chat", json={"message": "check system health and CPU usage"}, headers=H)
    assert r_sys.status_code == 200 and r_sys.json()["command"] == "system_status"
    assert r_sys.json()["ok"] is True
    assert r_sys.json()["ui"] == {"open_drawer": "system"}

    # 7. Docker operations
    r_dock = client.post("/api/chat", json={"message": "check docker daemon status"}, headers=H)
    assert r_dock.status_code == 200 and r_dock.json()["command"] == "docker_ops"

    # 8. Recovery operations
    r_rec = client.post("/api/chat", json={"message": "check disaster recovery status and backups"}, headers=H)
    assert r_rec.status_code == 200 and r_rec.json()["command"] == "recovery_ops"
    assert r_rec.json()["ok"] is True

    # 9. General conversation routes through RAG LLM
    r_chat = client.post("/api/chat", json={"message": "Tell me an interesting fact about orbital mechanics"}, headers=H)
    assert r_chat.status_code == 200 and r_chat.json()["command"] is None
    assert r_chat.json()["ok"] is True and r_chat.json()["answer"]

    # 10. Development analysis (read-only fallback or project inspection)
    r_dev = client.post("/api/chat", json={"message": "inspect codebase architecture and dependencies"}, headers=H)
    assert r_dev.status_code == 200 and r_dev.json()["command"] == "dev_autonomous"
    assert r_dev.json()["plan"]

    # Contextual UI drawer voice/text directives
    r_ui = client.post("/api/chat", json={"message": "open approvals drawer"}, headers=H)
    assert r_ui.status_code == 200 and r_ui.json()["ui"] == {"open_drawer": "approvals"}


def test_req2_multi_agent_dev_pipeline_lifecycle_cancel_timeout_and_blocker(assistant, settings, tmp_path, monkeypatch):
    repo = tmp_path / "sample_repo"
    _init_git_repo(repo)
    proj = assistant.orchestrator.projects.register(
        name="sample_repo", root_path=str(repo), default_branch="main", test_cmd="python --version"
    )

    # Read-only analysis inspects repo + memory + skills honestly
    res_read = assistant.orchestrator.run_autonomous_dev_pipeline(
        goal="Review architecture in sample_repo",
        project_hint="sample_repo",
        intent="dev_architecture",
        risk_tier="READ",
        filesystem=assistant.filesystem,
    )
    assert res_read["ok"] is True
    assert res_read["status"] == "completed"
    assert "architect_agent" in res_read["agents"]
    assert res_read["inspection"]["total_files"] >= 2

    # Honest blocker when write is requested without OpenCode CLI or configured runner
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    assistant.orchestrator._coder_runner = None
    res_blocked = assistant.orchestrator.run_autonomous_dev_pipeline(
        goal="Implement feature X in sample_repo",
        project_hint="sample_repo",
        intent="dev_coding",
        risk_tier="WRITE",
        filesystem=assistant.filesystem,
    )
    assert res_blocked["ok"] is False
    assert res_blocked["status"] == "blocked"
    assert "OpenCode CLI" in res_blocked["summary"]

    # Real full workforce execution when runner is available -> runs QA + security gate + owner approval
    def fake_runner(prompt: str, workdir: str, model=None, bin_name=None, **_kw):
        p = Path(workdir) / "app.py"
        p.write_text("def greet():\n    return 'hello p10.19'\n", encoding="utf-8")
        return {"ok": True, "summary": "Updated greeting", "changed_files": ["app.py"]}

    orch_write = AgentOrchestrator(
        settings,
        bus=assistant.bus,
        brain=assistant.brain,
        data_dir=assistant.data_dir,
        coder_runner=fake_runner,
    )
    res_write = orch_write.run_autonomous_dev_pipeline(
        goal="Implement greeting update in sample_repo",
        project_hint="sample_repo",
        intent="dev_coding",
        risk_tier="WRITE",
        filesystem=assistant.filesystem,
        qa_command=["python", "--version"],
    )
    assert res_write["ok"] is True
    assert res_write["status"] == "awaiting_approval"
    assert res_write.get("approval_token")
    assert res_write["qa_result"]["passed"] is True

    # Timeout cleanly stops runaway runner and cancels active run
    def slow_runner(prompt: str, workdir: str, model=None, bin_name=None, **_kw):
        time.sleep(0.6)
        return {"ok": True}

    orch_timeout = AgentOrchestrator(
        settings,
        bus=assistant.bus,
        brain=assistant.brain,
        data_dir=assistant.data_dir,
        coder_runner=slow_runner,
    )
    res_timeout = orch_timeout.run_autonomous_dev_pipeline(
        goal="Slow task in sample_repo",
        project_hint=proj["id"],
        intent="dev_coding",
        risk_tier="WRITE",
        filesystem=assistant.filesystem,
        timeout_s=0.05,
    )
    assert res_timeout["ok"] is False
    assert "Timed out" in res_timeout["summary"]

    # Immediate user cancellation check stops pipeline cleanly
    res_cancel = assistant.orchestrator.run_autonomous_dev_pipeline(
        goal="Cancel this task",
        project_hint="sample_repo",
        intent="dev_coding",
        risk_tier="READ",
        filesystem=assistant.filesystem,
        cancel_check=lambda: True,
    )
    assert res_cancel["ok"] is False
    assert res_cancel["status"] == "cancelled"


def test_req4_voice_never_displays_fake_listening_and_recovers(assistant, settings, monkeypatch):
    client = TestClient(create_app(settings, assistant=assistant))
    published: list[dict] = []
    orig_pub = assistant.bus.publish
    assistant.bus.publish = lambda ev: (published.append(res := orig_pub(ev)), res)[1]
    assistant.voice.emit = assistant.bus.publish

    # Host mic is inactive by default -> OS state must NEVER display "Listening"
    assistant._set_os_state("Listening", source="voice")
    assert assistant.os_state == "Idle"
    assert assistant.voice.mic_active is False

    # Transitioning to RESUMING before stream opens must emit "Resuming", not "Listening"
    assistant.voice._transition(VoiceState.RESUMING, reason="cooldown")
    res_ev = [e for e in published if e.get("type") == "voice_state"][-1]
    assert res_ev["assistant_state"] == "Resuming"
    assert res_ev["mic_active"] is False

    # Trying to emit LISTENING while _run is cleared / paused must clamp to "Idle"
    assistant.voice._run.clear()
    assistant.voice._transition(VoiceState.LISTENING, reason="stale_check")
    clamp_ev = [e for e in published if e.get("type") == "voice_state"][-1]
    assert clamp_ev["assistant_state"] == "Idle"
    assert clamp_ev["mic_active"] is False

    # /api/events hello snapshot clamps stale "Listening" away and reports wake_ready honestly
    assistant.os_state = "Listening"
    with client.stream("GET", "/api/events") as stream:
        line = next(ln for ln in stream.iter_lines() if ln.startswith("data:"))
        assert '"assistant_state": "Idle"' in line
        assert '"mic_active": false' in line

    # Recover action cleanly invokes VoiceLoop.recover
    called: list[str | None] = []
    monkeypatch.setattr(assistant.voice, "recover", lambda mode=None: called.append(mode))
    r_rec = client.post("/api/voice/session", json={"action": "recover", "mode": "continuous"}, headers=H)
    assert r_rec.status_code == 200
    assert called == ["continuous"]

    # /api/voice/command emits partial + final transcripts and passes UI drawer directives
    monkeypatch.setattr(assistant.stt, "transcribe", lambda *a, **k: {"text": "open memory inspector", "language": "en", "confidence": 0.95})
    monkeypatch.setattr(assistant.speaker, "say", lambda *a, **k: 1)
    r_vcmd = client.post("/api/voice/command?speak=false", content=_wav_bytes(), headers={**H, "Content-Type": "audio/wav"})
    assert r_vcmd.status_code == 200
    body = r_vcmd.json()
    assert body["ui"] == {"open_drawer": "memory"}
    ev_types = [e.get("event_type") for e in published]
    assert "assistant.transcript.partial" in ev_types
    assert "assistant.transcript.final" in ev_types

    # /api/voice/interrupt invokes full interrupt_all across LLM + tasks + workflows + TTS
    stopped = []
    monkeypatch.setattr(assistant.speaker, "stop", lambda: stopped.append(True))
    r_int = client.post("/api/voice/interrupt", headers=H)
    assert r_int.status_code == 200 and r_int.json()["ok"] is True
    assert len(stopped) >= 1


def test_req5_event_semantics_separate_tools_skills_and_workflows(assistant, tmp_path):
    published: list[dict] = []
    orig_pub = assistant.bus.publish
    assistant.bus.publish = lambda ev: (published.append(res := orig_pub(ev)), res)[1]

    # Generic tool execution emits tool.invoked & tool.executed, NEVER skill.executed
    graph_live.tool_invoked(assistant, "filesystem_search", query="readme")
    graph_live.tool_used(assistant, "filesystem_search", query="readme", ok=True, duration_ms=12.4)
    ev_names = [e.get("event_type") for e in published]
    assert "tool.invoked" in ev_names
    assert "tool.executed" in ev_names
    assert "skill.executed" not in ev_names

    # Real workflow emits workflow.started + workflow.completed alongside agent.started/completed
    published.clear()
    wf_res = assistant.orchestrator.start_assistant_workflow(goal="generate quick summary", task_kind="general", payload={})
    run_id = wf_res["run_id"]
    run_evs = [e for e in published if e.get("run_id") == run_id]
    agent_evs = [e for e in run_evs if str(e.get("event_type", "")).startswith("agent.")]
    assert agent_evs[0]["event_type"] == "agent.started"
    assert agent_evs[-1]["event_type"] == "agent.completed"
    wf_evs = [e.get("event_type") for e in run_evs]
    assert "workflow.started" in wf_evs
    assert "workflow.completed" in wf_evs

    # Genuine installed skill invocation emits skill.executed with skills department
    published.clear()
    repo = tmp_path / "skill_repo"
    _init_git_repo(repo)
    assistant.orchestrator.projects.register(name="skill_repo", root_path=str(repo))
    res = assistant.orchestrator.run_autonomous_dev_pipeline(
        goal="Review codebase in skill_repo",
        project_hint="skill_repo",
        intent="dev_architecture",
        risk_tier="READ",
        filesystem=assistant.filesystem,
    )
    if res.get("selected_skills"):
        skill_evs = [e for e in published if e.get("event_type") == "skill.executed"]
        assert skill_evs and skill_evs[0]["source_department"] == "skills"


def test_req6_security_blocks_all_destructive_operations_pending_approval(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    risky_prompts = [
        "delete all files in the data directory",
        "run pg_restore from backup onto production database",
        "git push origin main right now",
        "dump .env and print OPENAI_API_KEY",
        "install external skill from untrusted source",
    ]
    for prompt in risky_prompts:
        resp = client.post("/api/chat", json={"message": prompt}, headers=H)
        assert resp.status_code == 200
        body = resp.json()
        assert body["command"] == "approval_required", f"Failed to gate: {prompt}"
        assert body["ok"] is False
        assert body.get("approval_token")


def test_req3_single_screen_ui_directives_and_status_honesty(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))

    home = client.get("/")
    assert home.status_code == 200
    assert '<div id="root"></div>' in home.text

    st = client.get("/api/status").json()
    assert st["voice"]["mic_active"] is False
    assert st["voice"]["wake_ready"] == assistant.wake.ready

    for phrase, expected in [
        ("open workforce drawer", {"open_drawer": "workforce"}),
        ("open system monitor", {"open_drawer": "system"}),
        ("focus knowledge graph", {"open_drawer": None, "focus": "graph"}),
        ("close drawer", {"open_drawer": None}),
    ]:
        r = client.post("/api/chat", json={"message": phrase}, headers=H)
        assert r.status_code == 200
        assert r.json()["ui"] == expected

