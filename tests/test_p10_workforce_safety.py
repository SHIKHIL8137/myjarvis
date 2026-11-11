"""P10.1 Verification: Workforce QA gates, full-manifest & untracked scanning, worktree isolation, & sandbox policy."""
from pathlib import Path
import subprocess

import pytest

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.agents.workforce_graph import WorkforceState, validate_transition
from jarvis.integrations import git_worktree


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@jarvis.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Jarvis Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_state_machine_rejects_invalid_transitions():
    with pytest.raises(ValueError, match="Invalid workforce state transition"):
        validate_transition(WorkforceState.QA_FAILED, WorkforceState.MERGING)
    with pytest.raises(ValueError, match="Invalid workforce state transition"):
        validate_transition(WorkforceState.PLANNING, WorkforceState.COMPLETED)
    assert validate_transition(WorkforceState.QA_PENDING, WorkforceState.SECURITY_REVIEW) == "SECURITY_REVIEW"


def test_failed_qa_and_exhausted_retries_never_reach_approval_or_merge(tmp_path, settings):
    repo = _init_repo(tmp_path, "qa_fail_repo")
    settings.workforce.max_repair_retries = 1
    attempts = {"n": 0}

    def bad_coder(prompt, *, workdir, model=None, bin_name="opencode"):
        attempts["n"] += 1
        (Path(workdir) / "app.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
        return {"ok": True, "final_text": "still buggy"}

    orch = AgentOrchestrator(settings, data_dir=tmp_path / "data", coder_runner=bad_coder)
    proj = orch.projects.register(name="qa_fail_repo", root_path=repo)
    res = orch.start_workforce_workflow(
        project_id=proj["id"],
        task_prompt="Fix add",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert attempts["n"] == 2  # initial try + 1 retry
    assert res["interrupted"] is False
    assert res["state"] == "failed"
    assert res["result"]["workflow_state"] == "FAILED"
    assert not res["result"].get("approval_token")
    assert orch.approvals.list_pending() == []
    assert "return a - b" in (repo / "app.py").read_text(encoding="utf-8")
    orch.db.sync_engine.dispose()


def test_untracked_env_secret_and_staged_binary_manifest_blocks_merge(tmp_path, settings):
    repo = _init_repo(tmp_path, "sec_repo")
    leaked_secret = "sk-ant-api03-verysecretvalue998877665544"

    def sneaky_coder(prompt, *, workdir, model=None, bin_name="opencode"):
        wt = Path(workdir)
        (wt / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        # Untracked file NOT present in `git diff HEAD`!
        (wt / ".env").write_text(f"API_KEY={leaked_secret}\n", encoding="utf-8")
        (wt / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        return {"ok": True, "final_text": "Fixed + wrote .env"}

    orch = AgentOrchestrator(settings, data_dir=tmp_path / "data", coder_runner=sneaky_coder)
    proj = orch.projects.register(name="sec_repo", root_path=repo)
    res = orch.start_workforce_workflow(
        project_id=proj["id"],
        task_prompt="Fix add",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert res["interrupted"] is False
    assert res["state"] == "blocked_security"
    assert res["result"]["workflow_state"] == "BLOCKED"
    assert res["result"]["security_scan"]["clean"] is False
    # Secret pattern caught across untracked file WITHOUT echoing raw secret in findings!
    findings_text = str(res["result"]["security_scan"]["findings"])
    assert ".env" in findings_text
    assert leaked_secret not in findings_text
    assert any(e["path"] == "blob.bin" and e["is_binary"] for e in res["result"]["security_scan"]["manifest"])
    assert orch.approvals.list_pending() == []
    orch.db.sync_engine.dispose()


def test_post_review_modification_invalidates_approval_and_blocks_merge(tmp_path, settings):
    repo = _init_repo(tmp_path, "tamper_repo")

    def good_coder(prompt, *, workdir, model=None, bin_name="opencode"):
        (Path(workdir) / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"ok": True, "final_text": "clean fix"}

    orch = AgentOrchestrator(settings, data_dir=tmp_path / "data", coder_runner=good_coder)
    proj = orch.projects.register(name="tamper_repo", root_path=repo)
    res = orch.start_workforce_workflow(
        project_id=proj["id"],
        task_prompt="Fix add cleanly",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert res["interrupted"] is True
    tok = res["result"]["approval_token"]
    wt = Path(res["result"]["worktree_path"])

    # Tamper with worktree file AFTER security review / approval creation!
    (wt / "app.py").write_text("def add(a, b):\n    return a + b + 999\n", encoding="utf-8")
    resumed = orch.resume_workflow_after_approval(token=tok, approved=True, decided_by="owner")
    assert resumed["state"] == "blocked"
    assert resumed["result"]["workflow_state"] == "BLOCKED"
    assert "return a - b" in (repo / "app.py").read_text(encoding="utf-8")
    orch.db.sync_engine.dispose()


def test_worktree_isolation_collision_uncommitted_cleanup_and_sandbox_policy(tmp_path):
    repo = _init_repo(tmp_path, "iso_repo")

    wt1 = git_worktree.create_worktree(repo, "task-alpha", ensure_unique=False)
    # Same task_id without ensure_unique must NOT silently wipe existing worktree!
    with pytest.raises(git_worktree.GitWorktreeError, match="already exists"):
        git_worktree.create_worktree(repo, "task-alpha", ensure_unique=False)

    # Concurrent ensure_unique gives isolated directory & branch
    wt2 = git_worktree.create_worktree(repo, "task-alpha", ensure_unique=True)
    assert wt1["worktree_path"] != wt2["worktree_path"]
    assert wt1["branch"] != wt2["branch"]

    # Wrong task owner cannot access another task's worktree
    with pytest.raises(git_worktree.GitWorktreeError, match="owned by another task"):
        git_worktree.verify_worktree_ownership(repo, Path(wt1["worktree_path"]), "other-task")

    # Path traversal cleanup blocked
    with pytest.raises(git_worktree.GitWorktreeError, match="outside isolated worktree root"):
        git_worktree.cleanup_worktree(repo, repo / "..")

    # Dirty worktree cleanup refused without explicit authorization
    (Path(wt1["worktree_path"]) / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")
    with pytest.raises(git_worktree.GitWorktreeError, match="uncommitted changes"):
        git_worktree.cleanup_worktree(repo, Path(wt1["worktree_path"]), wt1["branch"], authorized_uncommitted=False)
    # With explicit authorization it cleans safely
    assert git_worktree.cleanup_worktree(repo, Path(wt1["worktree_path"]), wt1["branch"], authorized_uncommitted=True)["cleaned"]
    assert git_worktree.cleanup_worktree(repo, Path(wt2["worktree_path"]), wt2["branch"])["cleaned"]

    # ExecutionPolicy blocks denied commands & directories outside worktree and reports honest isolation caps
    pol = git_worktree.ExecutionPolicy(allowed_workdirs=[repo], sandbox_backend="restricted_subprocess")
    caps = pol.capabilities()
    assert caps["backend"] == "restricted_subprocess" and caps["os_network_isolation"] is False
    assert caps["limitations"]
    with pytest.raises(git_worktree.GitWorktreeError, match="denied"):
        pol.validate_access(repo, "execute", exe_name="powershell")
    with pytest.raises(git_worktree.GitWorktreeError, match="outside allowed"):
        pol.validate_access(tmp_path, "execute", exe_name="pytest")

