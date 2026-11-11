"""P10.11 Hardening Verification Suite: closes every critical security, isolation, cancellation, PG restore, training & DR gap."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

import pytest
from starlette.testclient import TestClient

from jarvis.agents.orchestrator import AgentOrchestrator
from jarvis.api import create_app
from jarvis.db.engine import DatabaseManager
from jarvis.departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from jarvis.gateway.gateway import CloudGatewayError, ModelGateway
from jarvis.gateway.interface import GatewayRequest, ProviderCapabilities
from jarvis.integrations import git_worktree, opencode_adapter
from jarvis.learning.training.training_audit import TrainingAuditLog
from jarvis.learning.training.training_config import TrainingJobConfig
from jarvis.learning.training.training_manager import TrainingManager
from jarvis.learning.training.training_worker import run_job_directory
from jarvis.memory.five_way import FiveWayMemoryService
from jarvis.recovery.backup import DisasterRecoveryManager, _sha256_file

from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


def _init_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@jarvis.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Jarvis Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


# ==============================================================================
# P10.11.1 — Git Manifest & Committed-Change Security
# ==============================================================================


def test_p10_11_1_committed_secret_and_after_approval_commit_block_merge(tmp_path, settings):
    repo = _init_git_repo(tmp_path, "committed_sec")
    base_sha = git_worktree.git_head_sha(repo)

    def sneaky_commit_coder(prompt, *, workdir, model=None, bin_name="opencode"):
        wd = Path(workdir)
        # OpenCode commits a secret BEFORE security review so working tree against HEAD looks completely clean!
        (wd / "app.py").write_text(
            'def add(a, b):\n    return a + b\nAPI = "sk-proj-committedSecret1234567890ABCDEF"\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "app.py"], cwd=wd, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "sneaky commit"], cwd=wd, check=True, capture_output=True)
        return {"ok": True, "final_text": "committed cleanly"}

    orch = AgentOrchestrator(settings, data_dir=tmp_path / "data1", coder_runner=sneaky_commit_coder)
    proj = orch.projects.register(name="committed_sec", root_path=repo)
    res = orch.start_workforce_workflow(
        project_id=proj["id"],
        task_prompt="Add feature",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert res["interrupted"] is False
    assert res["state"] == "blocked_security"
    assert orch.approvals.list_pending() == []
    assert git_worktree.git_head_sha(repo) == base_sha

    # Part B: Clean commit approved -> post-review commit changes HEAD -> approval invalidated on resume!
    def clean_commit_coder(prompt, *, workdir, model=None, bin_name="opencode"):
        wd = Path(workdir)
        (wd / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=wd, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "clean feature"], cwd=wd, check=True, capture_output=True)
        return {"ok": True, "final_text": "ok"}

    orch2 = AgentOrchestrator(settings, data_dir=tmp_path / "data2", coder_runner=clean_commit_coder)
    proj2 = orch2.projects.register(name="post_commit_sec", root_path=_init_git_repo(tmp_path, "post_commit_repo"))
    r2 = orch2.start_workforce_workflow(
        project_id=proj2["id"],
        task_prompt="Add feature.py",
        qa_command=["python", "-m", "pytest", "-q"],
    )
    assert r2["interrupted"] is True and r2["state"] == "awaiting_approval"
    tok = r2["result"]["approval_token"]
    wt_path = Path(r2["result"]["worktree_path"])

    # Attacker commits AFTER security review created the approval token!
    (wt_path / "backdoor.py").write_text("BACKDOOR = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "backdoor.py"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "post review tamper"], cwd=wt_path, check=True, capture_output=True)

    resumed = orch2.resume_workflow_after_approval(token=tok, approved=True, decided_by="owner")
    assert resumed["state"] == "blocked"
    assert not (Path(proj2["root_path"]) / "backdoor.py").exists()


def test_p10_11_1_rename_delete_binary_streaming_over_100kb_and_oversized_block(tmp_path, settings):
    repo = _init_git_repo(tmp_path, "manifest_edge")
    (repo / ".env.staging").write_text("SAFE=1\n", encoding="utf-8")
    (repo / "old_mod.py").write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add assets"], cwd=repo, check=True, capture_output=True)

    db = DatabaseManager("", data_dir=tmp_path / "db")
    db.init_schema_sync()
    pr = ProjectRegistryService(db)
    wf = DevWorkforceService(db, pr)
    proj = pr.register(name="manifest_edge", root_path=repo)
    wt = wf.open_worktree(proj["id"], "task-manifest-edge")
    wt_path = Path(wt["worktree_path"])

    # 1. Rename sensitive file (.env.staging -> config/clean_name.txt), delete old_mod.py, add binary file
    (wt_path / "config").mkdir()
    subprocess.run(["git", "mv", ".env.staging", "config/clean_name.txt"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "rm", "old_mod.py"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "blob.bin").write_bytes(b"\x00\x01\x02\xfe\xffPNG")

    # 2. Large >100 KB text file with secret at trailing byte ~220 KB (must NOT be truncated!)
    padding = ("x = 1  # benign line padding to exceed 100 KB\n" * 5000)
    assert len(padding.encode("utf-8")) > 200_000
    (wt_path / "large_ok_then_secret.py").write_text(
        padding + '\nHIDDEN_KEY = "AIzaSyTrailingSecretKeyBeyond100KB1234567890"\n',
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "committed renames deletes and 200kb file"], cwd=wt_path, check=True, capture_output=True)

    manifest = git_worktree.build_change_manifest(wt_path, base_sha=wt["base_sha"])
    assert manifest["base_sha"] != manifest["head_sha"]
    by_path = {e["path"]: e for e in manifest["manifest"]}
    assert "config/clean_name.txt" in by_path
    assert by_path["config/clean_name.txt"]["old_path"] == ".env.staging"
    assert by_path["config/clean_name.txt"]["change_type"] == "renamed"
    assert "old_mod.py" in by_path and by_path["old_mod.py"]["change_type"] == "deleted"
    assert "blob.bin" in by_path and by_path["blob.bin"]["is_binary"] is True

    scan = wf.security_scan_worktree(manifest, wt_path)
    assert scan["clean"] is False
    types = {f["type"] for f in scan["findings"]}
    assert "sensitive_filename" in types  # caught via old_path even though renamed!
    assert "secret_leak" in types  # caught AFTER 200 KB without 100 KB truncation!

    # 3. Oversized file exceeds max_scan_file_bytes -> explicitly blocked!
    (wt_path / "huge.py").write_text("a = 1\n" * 1000, encoding="utf-8")
    manifest2 = git_worktree.build_change_manifest(wt_path, base_sha=wt["base_sha"], max_scan_file_bytes=256)
    scan2 = wf.security_scan_worktree(manifest2, wt_path, max_scan_file_bytes=256)
    assert scan2["clean"] is False
    assert any(f["type"] == "oversized_unscanned_file" for f in scan2["findings"])


# ==============================================================================
# P10.11.2 — OpenCode Container / Sandbox Execution Hardening
# ==============================================================================


def test_p10_11_2_docker_policy_no_silent_fallback_and_restricted_env(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    policy = git_worktree.ExecutionPolicy(
        allowed_workdirs=[wt],
        sandbox_backend="docker",
        network_mode="none",
        max_memory_mb=512,
        cpus=1.0,
        docker_image="python:3.12-slim",
        allow_host_fallback=False,
        read_only_root_fs=True,
    )

    cid = tmp_path / "cid.txt"
    docker_cmd = git_worktree.build_docker_run_cmd(
        wt,
        ["python", "-V"],
        policy,
        cid_file=cid,
        extra_env={"PATH": "/usr/bin", "OPENROUTER_API_KEY": "sk-or-must-not-leak"},
    )
    joined = " ".join(docker_cmd)
    assert "--network=none" in docker_cmd
    assert "--read-only" in docker_cmd
    assert "--cap-drop=ALL" in docker_cmd
    assert "--security-opt=no-new-privileges:true" in docker_cmd
    assert "--memory" in docker_cmd and "512m" in docker_cmd
    assert "--cpus" in docker_cmd and "1.0" in docker_cmd
    assert f"{git_worktree.to_docker_mount_path(wt)}:/workspace:rw" in docker_cmd
    assert "sk-or-must-not-leak" not in joined

    # When Docker daemon is unavailable & allow_host_fallback=False -> fail loudly (NEVER silent host run)
    monkeypatch.setattr(opencode_adapter, "is_docker_available", lambda: False)
    with pytest.raises(opencode_adapter.OpenCodeError, match="Docker isolation"):
        opencode_adapter.run_opencode_task("echo test", workdir=wt, policy=policy)

    # Restricted subprocess strips secret env vars and denies shell/curl
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret")
    sub_policy = git_worktree.ExecutionPolicy(allowed_workdirs=[wt], sandbox_backend="restricted_subprocess")
    with pytest.raises(git_worktree.GitWorktreeError, match="denied"):
        git_worktree.run_worktree_command(wt, ["powershell", "-Command", "whoami"], policy=sub_policy)

    out_res = git_worktree.run_worktree_command(
        wt,
        [sys.executable, "-c", "import os; print('LEAK' if 'ANTHROPIC_API_KEY' in os.environ else 'CLEAN')"],
        policy=sub_policy,
    )
    assert out_res["passed"] is True
    assert "CLEAN" in out_res["stdout"]
    assert sub_policy.capabilities(docker_ready=False)["os_network_isolation"] is False


@pytest.mark.skipif(not git_worktree.is_docker_available(), reason="Live Docker daemon not running on host")
def test_p10_11_2_live_docker_sandbox_isolation(tmp_path):
    wt = tmp_path / "live_wt"
    wt.mkdir()
    policy = git_worktree.ExecutionPolicy(
        allowed_workdirs=[wt],
        sandbox_backend="docker",
        network_mode="none",
        max_memory_mb=256,
        cpus=0.5,
        allow_host_fallback=False,
    )
    res = git_worktree.run_worktree_command(wt, ["python", "-c", "print('docker-ok')"], policy=policy)
    assert res["passed"] and "docker-ok" in res["stdout"]


# ==============================================================================
# P10.11.3 — PostgreSQL Restore Verification Security
# ==============================================================================


def test_p10_11_3_pg_restore_security_pgpass_exit_on_error_and_prod_guard(tmp_path, settings, monkeypatch):
    prod_url = "postgresql+psycopg://postgres:SuperSecretPgPass123!@localhost:5432/jarvis_prod"
    scratch_url = "postgresql+psycopg://postgres:SuperSecretPgPass123!@localhost:5432/jarvis_scratch"

    db = DatabaseManager("", data_dir=tmp_path / "pg_mock")
    db.init_schema_sync()
    dr = DisasterRecoveryManager(db, settings, data_dir=tmp_path / "pg_mock", secrets_path=tmp_path / "pg_secrets.json")
    dr.register_database_target("scratch_pg", scratch_url, label="isolated scratch")
    # Simulate PG engine identity without opening real PG socket
    monkeypatch.setattr(dr.db, "is_postgres", True)
    monkeypatch.setattr(dr.db, "sync_url", prod_url)

    # 1. Never allow verifying or restoring over production URL without explicit flag
    dump_file = tmp_path / "fake.dump"
    dump_file.write_bytes(b"PGDMP")
    with pytest.raises(ValueError, match="production|scratch_pg"):
        dr.verify_backup(dump_file, passphrase="Passphrase123!", scratch_pg_url=prod_url)
    with pytest.raises(ValueError, match="production|approval"):
        dr.restore_postgresql(dump_file, prod_url)

    # 2. Password NEVER appears on pg_dump / pg_restore CLI argv; PGPASSFILE is used & cleaned; non-zero fails!
    observed: list[dict] = []

    def fake_pg_tool(name):
        return f"mock_{name}"

    def fake_run(argv, *, capture_output=True, text=True, timeout=300, shell=False, env=None):
        pgpass = (env or {}).get("PGPASSFILE")
        pgpass_content = Path(pgpass).read_text(encoding="utf-8") if pgpass and Path(pgpass).exists() else ""
        observed.append({"argv": list(argv), "pgpass": pgpass, "pgpass_content": pgpass_content})

        class R:
            returncode = 1
            stdout = ""
            stderr = "FATAL: password authentication failed for user postgres using SuperSecretPgPass123!"

        return R()

    monkeypatch.setattr("jarvis.recovery.backup._find_pg_tool", fake_pg_tool)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        dr.restore_postgresql(dump_file, scratch_url)

    assert len(observed) == 1
    cmd = observed[0]["argv"]
    assert "--exit-on-error" in cmd
    assert not any("SuperSecretPgPass123!" in arg for arg in cmd)
    assert "SuperSecretPgPass123!" not in str(exc_info.value)  # password scrubbed!
    assert observed[0]["pgpass"] is not None
    assert "SuperSecretPgPass123!" in observed[0]["pgpass_content"]
    assert not Path(observed[0]["pgpass"]).exists()  # PGPASSFILE temp file deleted!

    # 3. Verify bundle with db/postgres.dump but NO scratch_pg_url NEVER marks verified_restore=True!
    snap_dir = tmp_path / "pg_bundle_stage"
    (snap_dir / "db").mkdir(parents=True)
    (snap_dir / "db" / "postgres.dump").write_bytes(b"PGDMP_SYNTH")
    (snap_dir / "db" / "postgres_meta.json").write_text(json.dumps({"tables": {"agent_runs": 1}, "pgvector_installed": True}), encoding="utf-8")
    from jarvis.security.secrets import export_encrypted_vault

    (snap_dir / "secrets.vault.enc").write_bytes(export_encrypted_vault({"k": "v"}, "RecoverPass99!"))
    f_hashes = {p.relative_to(snap_dir).as_posix(): _sha256_file(p) for p in snap_dir.rglob("*") if p.is_file()}
    manifest_core = {"format": "jarvis-dr-bundle-v2", "tier": "tier2_warm", "created_at": time.time(), "dialect": "postgresql", "files": f_hashes}
    salt = os.urandom(16)
    from jarvis.recovery.backup import _manifest_hmac

    canon = json.dumps(manifest_core, indent=2, sort_keys=True).encode("utf-8")
    manifest = {**manifest_core, "hmac_salt_b64": base64.b64encode(salt).decode("ascii"), "hmac_sha256": _manifest_hmac(canon, salt, "RecoverPass99!")}
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    bpath = tmp_path / "pg_bundle.zip"
    with zipfile.ZipFile(bpath, "w") as zf:
        for p in sorted(snap_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(snap_dir).as_posix())

    rep_unverified = dr.verify_backup(bpath, passphrase="RecoverPass99!")
    assert rep_unverified["ok"] is False
    assert rep_unverified["verified_restore"] is False
    assert rep_unverified["db_verified"] is False

    # 4. Simulated pg_restore rc=0 against a scratch DB that is missing pgvector & has row mismatch -> verify fails!
    def fake_run_ok(argv, *, capture_output=True, text=True, timeout=300, shell=False, env=None):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    class _FakeScalarRes:
        def __init__(self, v):
            self._v = v
        def scalars(self):
            return self
        def all(self):
            return self._v if isinstance(self._v, list) else []
        def scalar(self):
            return self._v

    class _FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def execute(self, stmt):
            sql = str(stmt)
            if "FROM pg_tables" in sql:
                return _FakeScalarRes(["agent_runs"])
            if "SELECT COUNT(*)" in sql:
                return _FakeScalarRes(0)  # mismatch vs expected 1!
            if "FROM pg_extension" in sql:
                return _FakeScalarRes(0)  # missing pgvector!
            return _FakeScalarRes(0)

    class _FakeTargetDB:
        is_postgres = False
        pgvector_available = False
        sync_url = scratch_url

        def __init__(self, url):
            self.sync_engine = type("E", (), {"connect": lambda s: _FakeConn(), "dispose": lambda s: None})()

        def sync_session(self):
            return db.sync_session()

    monkeypatch.setattr("jarvis.recovery.backup.DatabaseManager", _FakeTargetDB)
    with pytest.raises(ValueError, match="PostgreSQL isolated restore verification failed"):
        dr.verify_backup(bpath, passphrase="RecoverPass99!", scratch_pg_url=scratch_url)


@pytest.mark.pg
def test_p10_11_3_live_postgres_backup_and_scratch_restore(tmp_path, settings):
    pg_url = os.environ.get("JARVIS_PG_TEST_URL", "").strip()
    scratch_url = os.environ.get("JARVIS_PG_SCRATCH_URL", "").strip()
    if not pg_url or not scratch_url:
        pytest.skip("JARVIS_PG_TEST_URL and JARVIS_PG_SCRATCH_URL not configured")
    db = DatabaseManager(pg_url, data_dir=tmp_path)
    db.init_schema_sync()
    dr = DisasterRecoveryManager(db, settings, data_dir=tmp_path, secrets_path=tmp_path / "pg_secrets.json")
    bundle = dr.create_backup(tier="tier2_warm", passphrase="PgLivePassphrase!99")
    rep = dr.verify_backup(Path(bundle["bundle_path"]), passphrase="PgLivePassphrase!99", scratch_pg_url=scratch_url)
    assert rep["ok"] is True and rep["verified_restore"] is True and rep["db_verified"] is True


# ==============================================================================
# P10.11.4 — Training Hardware Limits & Runtime Safeguards
# ==============================================================================


def test_p10_11_4_training_blockers_override_concurrency_and_qlora_worker(tmp_path, settings, monkeypatch):
    prep = tmp_path / "prepared" / "b1"
    prep.mkdir(parents=True)
    (prep / "train.jsonl").write_text(json.dumps({"instruction": "hi", "output": "hello"}) + "\n", encoding="utf-8")

    tm = TrainingManager(
        settings.learning,
        jobs_dir=tmp_path / "jobs",
        prepared_dir=tmp_path / "prepared",
        audit_log=TrainingAuditLog(tmp_path / "audit.jsonl"),
    )

    # Simulate PEFT/torch installed, NO bitsandbytes/CUDA, tight RAM
    fake_caps_no_bnb = {
        "installed": {"torch": True, "transformers": True, "peft": True, "bitsandbytes": False},
        "missing_required": [],
        "ready_for_peft_lora": True,
        "ready_for_qlora_4bit": False,
        "cuda": {"available": False, "device_name": None, "vram_gb": None},
    }
    monkeypatch.setattr("jarvis.learning.training.training_manager.inspect_training_capabilities", lambda: fake_caps_no_bnb)
    monkeypatch.setattr("jarvis.learning.training.training_worker.inspect_training_capabilities", lambda: fake_caps_no_bnb)
    monkeypatch.setattr("jarvis.learning.training.training_manager.sysinfo.ram", lambda: {"total_gb": 16.0, "free_gb": 0.5})
    monkeypatch.setattr("jarvis.learning.training.training_manager.sysinfo.gpu", lambda: {"name": None, "total_gb": 0.0, "used_gb": 0.0})

    cfg_qlora = TrainingJobConfig(dataset_build_id="b1", quantization="4bit", hf_model_id="Qwen/Qwen2.5-0.5B-Instruct")
    j_qlora = tm.create_job(cfg_qlora)
    tm.approve_job(j_qlora["job_id"])

    # Default force_spawn_even_if_blocked=False rejects BOTH package & hardware blockers
    with pytest.raises(RuntimeError):
        tm.start_job(j_qlora["job_id"])
    # Even owner override CANNOT bypass missing quantization/CUDA package requirement!
    with pytest.raises(RuntimeError):
        tm.start_job(j_qlora["job_id"], override=True, override_reason="try bypass", actor="owner")

    # Worker directly refuses silent FP32 fallback when 4-bit QLoRA requested!
    rc = run_job_directory(tmp_path / "jobs" / j_qlora["job_id"])
    assert rc == 2
    prog = json.loads((tmp_path / "jobs" / j_qlora["job_id"] / "progress.json").read_text(encoding="utf-8"))
    assert prog["state"] == "blocked" and "silent fp32" in prog["error"].lower()

    # Now simulate FP16 job with ONLY hardware blocker (packages OK)
    cfg_fp16 = TrainingJobConfig(dataset_build_id="b1", quantization="fp16", hf_model_id="Qwen/Qwen2.5-7B-Instruct")
    j_fp16 = tm.create_job(cfg_fp16)
    tm.approve_job(j_fp16["job_id"])
    with pytest.raises(RuntimeError, match="RAM"):
        tm.start_job(j_fp16["job_id"])
    with pytest.raises(PermissionError, match="override_reason"):
        tm.start_job(j_fp16["job_id"], override=True, override_reason="", actor="owner")

    # Mock Popen to spawn a controllable sleep process & test owner override + duplicate/concurrency guard + cancel termination
    monkeypatch.setattr("jarvis.learning.training.training_manager.sysinfo.ram", lambda: {"total_gb": 64.0, "free_gb": 48.0})
    cfg_fp16_2 = TrainingJobConfig(dataset_build_id="b1", quantization="fp16", hf_model_id="Qwen/Qwen2.5-0.5B-Instruct")
    j_fp16_2 = tm.create_job(cfg_fp16_2)
    tm.approve_job(j_fp16_2["job_id"])

    monkeypatch.setattr("jarvis.learning.training.training_manager.sysinfo.ram", lambda: {"total_gb": 16.0, "free_gb": 0.5})
    sleep_proc_holder: list[subprocess.Popen] = []
    orig_popen = subprocess.Popen

    def fake_popen(cmd, **kw):
        p = orig_popen([sys.executable, "-c", "import time; time.sleep(20)"])
        sleep_proc_holder.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    started = tm.start_job(j_fp16["job_id"], override=True, override_reason="Owner approved test swap", actor="owner")
    assert started["state"] in ("starting", "running")

    # Duplicate start on same job & concurrent second job both refused!
    with pytest.raises(RuntimeError, match="already running"):
        tm.start_job(j_fp16["job_id"], override=True, override_reason="dup", actor="owner")
    with pytest.raises(RuntimeError, match="max_concurrent_jobs"):
        tm.start_job(j_fp16_2["job_id"], override=True, override_reason="concurrent", actor="owner")

    # Cancel terminates real child process and clears worker.pid
    tm.cancel_job(j_fp16["job_id"])
    assert sleep_proc_holder[0].poll() is not None
    assert not (tmp_path / "jobs" / j_fp16["job_id"] / "worker.pid").exists()
    audit_txt = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "training_hardware_override" in audit_txt


# ==============================================================================
# P10.11.5 — Real Asynchronous Workforce Execution & Cancellation
# ==============================================================================


def test_p10_11_5_mid_execution_cancel_kills_process_keeps_event_loop_responsive_and_blocks_merge(
    tmp_path, settings, fake_brain, monkeypatch
):
    repo = _init_git_repo(tmp_path, "cancel_repo")
    entered = threading.Event()

    def slow_subprocess_coder(prompt, *, workdir, model=None, bin_name="opencode", policy=None, run_id=None, cancel_check=None):
        entered.set()
        git_worktree.run_worktree_command(
            Path(workdir),
            [sys.executable, "-c", "import time; time.sleep(25)"],
            policy=policy,
            run_id=run_id,
            cancel_check=cancel_check,
        )
        return {"ok": True}

    monkeypatch.setattr("jarvis.assistant.Assistant._warm_speech", lambda self: None)
    from jarvis.assistant import Assistant

    a = Assistant(settings, data_dir=tmp_path / "adata", brain=fake_brain)
    # Wire slow coder runner into orchestrator cleanly
    a.orchestrator._workforce_graph = __import__("jarvis.agents.workforce_graph", fromlist=["build_workforce_graph"]).build_workforce_graph(
        projects=a.orchestrator.projects,
        workforce=a.orchestrator.workforce,
        memory=a.orchestrator.memory,
        approvals=a.orchestrator.approvals,
        workforce_spec=settings.workforce,
        coder_runner=slow_subprocess_coder,
        is_cancelled_fn=a.orchestrator.is_run_cancelled,
        checkpointer=a.orchestrator.checkpointer,
    )
    proj = a.orchestrator.projects.register(name="cancel_repo", root_path=repo)
    app = create_app(settings, assistant=a)
    client = TestClient(app)

    run_result: dict = {}

    def _run_bg():
        r = client.post(
            "/api/v2/workforce/runs",
            json={"project_id": proj["id"], "task_prompt": "Slow refactor"},
            headers=H,
        )
        run_result["resp"] = r.json()

    t = threading.Thread(target=_run_bg, daemon=True)
    t.start()
    assert entered.wait(timeout=8.0)
    time.sleep(0.25)  # allow subprocess to start inside coder_opencode

    # Event loop stays responsive while workforce run is executing in asyncio.to_thread!
    t0 = time.perf_counter()
    ov = client.get("/api/v2/system/overview")
    assert ov.status_code == 200
    assert (time.perf_counter() - t0) < 2.0

    runs = client.get("/api/v2/workforce/runs").json()["runs"]
    active_run_id = runs[0]["id"]

    # Cancel live run -> terminates child process instead of sleeping 25s!
    t_cancel = time.perf_counter()
    c_resp = client.post(f"/api/v2/workforce/runs/{active_run_id}/cancel", json={"reason": "Owner stop"}, headers=H)
    assert c_resp.status_code == 200
    assert c_resp.json()["ok"] is True

    t.join(timeout=8.0)
    assert not t.is_alive()
    assert (time.perf_counter() - t_cancel) < 8.0
    assert run_result["resp"]["state"] == "cancelled"

    # Worktree preserved cleanly by default
    wt_rows = client.get(f"/api/v2/workforce/worktrees?project_id={proj['id']}").json()["worktrees"]
    assert wt_rows and wt_rows[0]["status"] == "cancelled_preserved"
    assert Path(wt_rows[0]["worktree_path"]).exists()

    # Even if an approval request exists for this cancelled run, deciding approved=True MUST be refused!
    stale_req = a.orchestrator.approvals.create_request(
        action_type="merge_worktree",
        target_resource="cancel_repo",
        run_id=active_run_id,
    )
    dec_resp = client.post(
        f"/api/v2/approvals/{stale_req['token']}/decide",
        json={"approved": True, "decided_by": "owner"},
        headers=H,
    )
    assert dec_resp.status_code == 409


# ==============================================================================
# P10.11.6 — Model Gateway Capability & Context Window Filtering
# ==============================================================================


@pytest.mark.anyio
async def test_p10_11_6_gateway_streaming_context_window_and_explicit_rejection(tmp_path, settings):
    db = DatabaseManager("", data_dir=tmp_path / "gw")
    db.init_schema_sync()
    gw = ModelGateway(FakeBrain(settings), db=db)

    # Inject model capabilities directly into cache to test filtering determinism
    gw._cap_cache["custom_nostream"] = ProviderCapabilities(
        provider="ollama", model_id="nostream", streaming=False, tool_calling=True, vision=True, json_mode=True, local=True, max_ctx=8192
    )
    gw._cap_cache["custom_smallctx"] = ProviderCapabilities(
        provider="ollama", model_id="smallctx", streaming=True, tool_calling=True, vision=False, json_mode=True, local=True, max_ctx=1000
    )
    gw._cap_cache["custom_fit"] = ProviderCapabilities(
        provider="ollama", model_id="fit", streaming=True, tool_calling=True, vision=True, json_mode=True, local=True, max_ctx=8192
    )

    # 1. require_streaming filters non-streaming models
    req_stream = GatewayRequest(
        model_key="custom_nostream",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=100,
        require_streaming=True,
    )
    eligible = gw.select_eligible_candidates(req_stream, ["custom_nostream", "custom_fit"])
    assert eligible == ["custom_fit"]

    # 2. est_prompt_tokens + max_tokens > max_ctx rejects even though max_tokens < max_ctx alone!
    long_msg = "token " * 700  # ~4200 chars -> ~1050 tokens; + 200 max_tokens = ~1250 > 1000!
    req_ctx = GatewayRequest(
        model_key="custom_smallctx",
        messages=[{"role": "user", "content": long_msg}],
        max_tokens=200,
    )
    assert gw.select_eligible_candidates(req_ctx, ["custom_smallctx"]) == []

    # 3. Never silently execute req.model_key when NO candidate satisfies capabilities!
    with pytest.raises(CloudGatewayError) as exc_info:
        async for _ in gw.stream_with_fallback(req_ctx, fallback_models=["custom_nostream"] if False else []):
            pass
    assert exc_info.value.category == "no_eligible_model"
    assert len(gw.brain.seen) == 0  # Brain was NEVER invoked!


# ==============================================================================
# P10.11.7 — Disaster Recovery Completeness & Tracked+Untracked Repo Restore
# ==============================================================================


def test_p10_11_7_dr_scope_tracking_and_full_git_tracked_untracked_restore(tmp_path, settings):
    settings.recovery.encrypt_bundles = False
    repo = _init_git_repo(tmp_path, "dr_proj")
    c1_sha = git_worktree.git_head_sha(repo)

    # Dirty tracked modification + brand new untracked file
    (repo / "app.py").write_text("def add(a, b):\n    return a + b + 1  # dirty\n", encoding="utf-8")
    (repo / "untracked_notes.txt").write_text("critical local draft note\n", encoding="utf-8")

    db = DatabaseManager("", data_dir=tmp_path / "dr_data")
    db.init_schema_sync()
    pr = ProjectRegistryService(db)
    pr.register(name="dr_proj", root_path=repo)
    FiveWayMemoryService(db, settings=settings).add_personal_memory(content="DR memory check", category="explicit")
    dr = DisasterRecoveryManager(db, settings, data_dir=tmp_path / "dr_data", secrets_path=tmp_path / "dr_secrets.json")

    # Part 1: DB-only backup clearly reports repositories_included=False and incomplete scope
    db_only = dr.create_backup(tier="tier1_hot", passphrase="DrPassphrase!99", include_repositories=False)
    assert db_only["repositories_included"] is False
    rep1 = dr.verify_backup(Path(db_only["bundle_path"]), passphrase="DrPassphrase!99")
    assert rep1["ok"] is True
    assert rep1["database_only_verified"] is True
    assert rep1["full_recovery_complete"] is False
    assert rep1["recovery_scope"] == "database_and_app_state_only"

    rest1 = dr.restore_to_directory(Path(db_only["bundle_path"]), passphrase="DrPassphrase!99", target_data_dir=tmp_path / "rest_db_only")
    assert rest1["full_recovery_complete"] is False
    assert rest1["recovery_scope"] == "database_and_app_state_only"

    # Part 2: Full backup with repositories + tracked dirty patch + untracked files
    full_b = dr.create_backup(
        tier="tier2_warm",
        passphrase="DrPassphrase!99",
        include_repositories=True,
        include_uncommitted=True,
    )
    assert full_b["repositories_included"] is True and full_b["repositories"] == ["dr_proj"]
    rep2 = dr.verify_backup(Path(full_b["bundle_path"]), passphrase="DrPassphrase!99")
    assert rep2["full_recovery_complete"] is True
    assert rep2["repositories_verified"] is True
    assert rep2["recovery_scope"] == "full_recovery_complete"

    rest_repos = tmp_path / "restored_repos"
    rest2 = dr.restore_to_directory(
        Path(full_b["bundle_path"]),
        passphrase="DrPassphrase!99",
        target_data_dir=tmp_path / "rest_full_data",
        target_repos_dir=rest_repos,
    )
    assert rest2["full_recovery_complete"] is True
    assert rest2["recovery_scope"] == "full_recovery_complete"

    restored_repo = rest_repos / "dr_proj"
    assert git_worktree.git_head_sha(restored_repo) == c1_sha
    assert "# dirty" in (restored_repo / "app.py").read_text(encoding="utf-8")
    assert (restored_repo / "untracked_notes.txt").read_text(encoding="utf-8") == "critical local draft note\n"

    # Part 3: Refuse overwriting non-empty existing target repo without overwrite_existing=True
    with pytest.raises(FileExistsError, match="overwrite_existing=True"):
        dr.restore_to_directory(
            Path(full_b["bundle_path"]),
            passphrase="DrPassphrase!99",
            target_data_dir=tmp_path / "rest_full_data",
            target_repos_dir=rest_repos,
            overwrite_existing=False,
        )

    rest3 = dr.restore_to_directory(
        Path(full_b["bundle_path"]),
        passphrase="DrPassphrase!99",
        target_data_dir=tmp_path / "rest_full_data",
        target_repos_dir=rest_repos,
        overwrite_existing=True,
    )
    assert rest3["full_recovery_complete"] is True

