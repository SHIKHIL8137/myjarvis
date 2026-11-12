"""P3 verification tests: Markdown agent profile loading, precedence, diagnostics, tool filtering, bounded delegation, and /v2/agents APIs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.agents.profiles import AgentWorkforceCoordinator
from jarvis.api_v2 import create_v2_router
from jarvis.config import BrainSpec, Settings


def test_p3_builtin_profiles_override_precedence_and_diagnostics(tmp_path: Path):
    user_dir = tmp_path / "user_agents"
    user_dir.mkdir(parents=True, exist_ok=True)
    # Override research_specialist at user scope
    (user_dir / "research_specialist.md").write_text(
        "---\n"
        "name: research_specialist\n"
        "description: Custom user research agent\n"
        "model_role: coding\n"
        "permission_tier: READ\n"
        "tools_allowlist: [read_file, deep_research]\n"
        "---\n"
        "User prompt body.\n",
        encoding="utf-8",
    )
    # Malformed agent file should be skipped with diagnostic without breaking others
    (user_dir / "broken.md").write_text("---\nname: broken\nmax_steps: [not_an_int\n---\nbad\n", encoding="utf-8")

    proj_dir = tmp_path / "proj"
    (proj_dir / ".jarvis" / "agents").mkdir(parents=True, exist_ok=True)
    # Project-local override takes highest precedence
    (proj_dir / ".jarvis" / "agents" / "research_specialist.md").write_text(
        "---\n"
        "name: research_specialist\n"
        "description: Project-local research specialist\n"
        "model_role: fast\n"
        "permission_tier: PROMPT\n"
        "tools_allowlist: [read_file, list_directory, deep_research]\n"
        "tools_denylist: [deep_research]\n"
        "---\n"
        "Project research prompt.\n",
        encoding="utf-8",
    )

    coord = AgentWorkforceCoordinator(user_dir=user_dir, project_dirs=[proj_dir])
    profiles = coord.list_profiles()
    names = {p.name for p in profiles}
    assert {"research_specialist", "dev_coordinator", "assistant_general"}.issubset(names)

    rs = coord.get_profile("research_specialist")
    assert rs is not None
    assert rs.scope == "project"
    assert rs.description == "Project-local research specialist"
    assert len(coord.get_diagnostics()) == 1
    assert "broken.md" in coord.get_diagnostics()[0]["path"]

    # Tool filtering applies allowlist minus denylist
    filtered = coord.filter_tools_for_agent("research_specialist", ["read_file", "list_directory", "deep_research", "execute_shell"])
    assert filtered == ["read_file", "list_directory"]


def test_p3_deterministic_selection_bounded_delegation_and_no_privilege_escalation(tmp_path: Path):
    user_dir = tmp_path / "agents"
    user_dir.mkdir(parents=True, exist_ok=True)
    # Low privilege delegator attempting to delegate to higher-tier agent
    (user_dir / "reader_coord.md").write_text(
        "---\n"
        "name: reader_coord\n"
        "permission_tier: READ\n"
        "can_delegate: true\n"
        "max_delegation_depth: 1\n"
        "---\nReader.\n",
        encoding="utf-8",
    )
    coord = AgentWorkforceCoordinator(user_dir=user_dir)

    assert coord.select_profile(intent="web_research", query="verify claims").name == "research_specialist"
    assert coord.select_profile(intent="dev_workforce", query="refactor repo bug").name == "dev_coordinator"

    # research_specialist has can_delegate: false
    ok_rs, reason_rs = coord.can_delegate_to("research_specialist", "assistant_general", current_depth=0)
    assert ok_rs is False
    assert "not authorized" in reason_rs.lower()

    # Privilege escalation: READ cannot delegate to PROMPT dev_coordinator
    ok_esc, reason_esc = coord.can_delegate_to("reader_coord", "dev_coordinator", current_depth=0)
    assert ok_esc is False
    assert "privilege escalation" in reason_esc.lower()

    # Equal tier succeeds at depth 0, fails when depth >= max_delegation_depth
    ok_same, _ = coord.can_delegate_to("reader_coord", "assistant_general", current_depth=0)
    assert ok_same is True
    ok_depth, reason_depth = coord.can_delegate_to("reader_coord", "assistant_general", current_depth=1)
    assert ok_depth is False
    assert "depth" in reason_depth.lower()


def test_p3_api_v2_agents_list_detail_and_reload(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(notes_dir=str(notes_dir), brains={"local": BrainSpec(model="qwen3:4b-instruct")})
    orch = AgentOrchestrator(s, data_dir=tmp_path / "data")
    fake_assistant = type("A", (), {"orchestrator": orch})()
    app = FastAPI()
    app.include_router(create_v2_router(fake_assistant))
    client = TestClient(app)

    res = client.get("/api/v2/agents")
    assert res.status_code == 200
    body = res.json()
    assert len(body["profiles"]) >= 3
    assert isinstance(body["diagnostics"], list)

    user_agents = tmp_path / "data" / "agents"
    user_agents.mkdir(parents=True, exist_ok=True)
    (user_agents / "custom_helper.md").write_text(
        "---\nname: custom_helper\ndescription: Added helper\n---\nHelper\n",
        encoding="utf-8",
    )
    reload_res = client.post("/api/v2/agents/reload")
    assert reload_res.status_code == 200
    assert any(p["name"] == "custom_helper" for p in reload_res.json()["profiles"])

    det = client.get("/api/v2/agents/custom_helper")
    assert det.status_code == 200
    assert det.json()["profile"]["description"] == "Added helper"

    missing = client.get("/api/v2/agents/does_not_exist")
    assert missing.status_code == 404
    asyncio.run(orch.db.close())

