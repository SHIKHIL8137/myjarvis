"""Focused regression suite for JARVIS P10.22.1 hardening fixes (FIX 1 through FIX 5)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import time

from jarvis.actions import ActionExecutor
from jarvis.agents.research import (
    DeepResearchEngine,
    ResearchClaim,
    ResearchSnapshot,
    run_deep_research_background_task,
    verify_checkpoint_snapshots,
    verify_claims_against_snapshots,
)
from jarvis.agents.task_runner import (
    BackgroundTaskRunner,
    TaskExecutionResult,
)
from jarvis.agents.tasks import AssistantTaskService
from jarvis.config import ActionsSpec, FilesystemSpec, InternetSpec
from jarvis.db.engine import DatabaseManager
from jarvis.departments.assistant.documents import DocumentService
from jarvis.filesystem import SafeFilesystem
from jarvis.security.outcomes import (
    ToolOutcome,
    ToolStatus,
    VerificationCheck,
    sha256_file,
    verify_file_modified,
)


class _FakeSearch:
    def __init__(self, pages: dict[str, dict]):
        self.spec = InternetSpec(enabled=True, allow_page_fetch=True)
        self._pages = pages

    async def search(self, query: str, max_results: int = 5) -> dict:
        return {
            "query": query,
            "provider": "fake",
            "results": [
                {"title": p.get("title", url), "url": url, "snippet": p["content"][:160]}
                for url, p in list(self._pages.items())[:max_results]
            ],
        }

    async def fetch_page(self, url: str, *, max_chars: int | None = None) -> dict:
        p = self._pages[url]
        txt = p["content"] if max_chars is None else p["content"][:max_chars]
        return {"url": url, "title": p.get("title", url), "content": txt}


# ---------------------------------------------------------------------------
# FIX 1: Correct Graceful Shutdown & Recovery Safety
# ---------------------------------------------------------------------------


def test_fix1_owner_cancel_vs_graceful_and_forced_shutdown_and_restart(tmp_path: Path):
    async def scenario():
        db_url = f"sqlite+aiosqlite:///{(tmp_path / 'fix1.db').as_posix()}"
        db = DatabaseManager(db_url, data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)

        # 1) Graceful shutdown: in-flight step finishes within grace window; queued 2nd task is NOT claimed.
        runner = BackgroundTaskRunner(tasks, worker_id="w-grace", poll_interval_s=0.05, shutdown_grace_s=0.5)
        step_entered = asyncio.Event()
        executed: list[str] = []

        async def fast_step_handler(ctx, task):
            executed.append(task["task_id"])
            step_entered.set()
            await asyncio.sleep(0.05)
            await ctx.complete_step("s1", checkpoint_patch={"ok": True})
            return TaskExecutionResult(summary="Step finished cleanly", result="done")

        runner.register_handler("deep_research", fast_step_handler)
        t1 = await runner.enqueue("Research task 1", worker_kind="deep_research", intent="web_research")
        t2 = await runner.enqueue("Research task 2", worker_kind="deep_research", intent="web_research")

        await runner.start()
        await asyncio.wait_for(step_entered.wait(), timeout=2.0)
        await runner.stop(grace_s=0.6)
        # Repeated shutdown must be completely idempotent and safe
        await runner.stop(grace_s=0.1)

        r1 = await tasks.get_task(t1["task_id"])
        r2 = await tasks.get_task(t2["task_id"])
        assert r1["status"] == "completed"
        assert r2["status"] == "queued"
        assert r2["claimed_by"] is None
        assert executed == [t1["task_id"]]

        # 2) Forced shutdown: grace period expires on blocked task -> preserves checkpoint, releases claim, NOT cancelled!
        stuck_entered = asyncio.Event()

        async def slow_handler(ctx, task):
            if not ctx.is_step_done("step_plan"):
                await ctx.complete_step("step_plan", checkpoint_patch={"plan_saved": True}, current_step="Planned")
            stuck_entered.set()
            await asyncio.sleep(5.0)
            await ctx.complete_step("step_finish")
            return TaskExecutionResult(summary="Unreachable")

        runner2 = BackgroundTaskRunner(tasks, worker_id="w-forced", poll_interval_s=0.05, shutdown_grace_s=0.04)
        runner2.register_handler("deep_research", slow_handler)
        await runner2.start()
        await asyncio.wait_for(stuck_entered.wait(), timeout=2.0)
        await runner2.stop(grace_s=0.04)

        forced_row = await tasks.get_task(t2["task_id"])
        assert forced_row["status"] == "running"  # Preserved recoverable status, NOT cancelled!
        assert forced_row["claimed_by"] is None
        assert "step_plan" in forced_row["completed_step_keys"]
        assert forced_row["checkpoint_json"]["plan_saved"] is True
        assert forced_row["meta"].get("shutdown_interrupted") is True

        # 3) Owner cancellation stays cancelled even if handler runs check_guardrails
        t_owner = await runner2.enqueue("Owner cancels me", worker_kind="deep_research", intent="web_research")
        await tasks.cancel_task(t_owner["task_id"], reason="Owner aborted")
        owner_row = await tasks.get_task(t_owner["task_id"])
        assert owner_row["status"] == "cancelled"

        # 4) Seed an approval-gated operation before simulated restart
        t_gate = await tasks.create_task(
            "Delete folder on host",
            intent="filesystem_ops",
            status="awaiting_approval",
            worker_kind="assistant",
        )
        t_unsafe = await tasks.create_task(
            "Interrupted unsafe command",
            intent="shell_exec",
            status="running",
            worker_kind="assistant",
        )
        await db.close()

        # Simulated reboot on fresh DB connection & runner: recovers t2, blocks t_gate and t_unsafe
        db2 = DatabaseManager(db_url, data_dir=tmp_path)
        tasks2 = AssistantTaskService(db2)
        runner3 = BackgroundTaskRunner(tasks2, worker_id="w-reboot")

        ran_on_reboot: list[str] = []

        async def resume_research_handler(ctx, task):
            ran_on_reboot.append(task["task_id"])
            assert ctx.is_step_done("step_plan")
            assert ctx.checkpoint.get("plan_saved") is True
            await ctx.complete_step("step_finish", checkpoint_patch={"finished": True})
            return TaskExecutionResult(summary="Resumed after shutdown", result="ok")

        runner3.register_handler("deep_research", resume_research_handler)
        rec = await runner3.start()
        assert t2["task_id"] in rec["requeued"]
        assert t_gate["task_id"] in rec["halted_approval"]
        assert t_unsafe["task_id"] in rec["paused_unsafe"]
        assert t_owner["task_id"] not in rec["requeued"]

        await asyncio.sleep(0.25)
        await runner3.stop(grace_s=0.5)
        resumed_t2 = await tasks2.get_task(t2["task_id"])
        gate_after = await tasks2.get_task(t_gate["task_id"])
        unsafe_after = await tasks2.get_task(t_unsafe["task_id"])
        assert resumed_t2["status"] == "completed"
        assert t2["task_id"] in ran_on_reboot
        assert gate_after["status"] == "awaiting_approval"
        assert unsafe_after["status"] == "paused"
        await db2.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# FIX 2: Enforce Tool Outcome Contract in Task Runner
# ---------------------------------------------------------------------------


def test_fix2_outcome_enforcement_in_task_runner(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'fix2.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)
        runner = BackgroundTaskRunner(tasks, backoff_base_s=0.01, backoff_max_s=0.02)

        async def verified_h(ctx, task):
            out = ToolOutcome(
                tool_name="verify_job",
                status="done",
                summary="Verified disk write",
                checks=[VerificationCheck("file_exists", True, "ok").to_dict()],
            )
            return TaskExecutionResult(summary="Verified", result="good", outcome=out)

        async def spoofed_unverified_h(ctx, task):
            # Attacker / LLM tries to claim verified=True with zero checks -> MUST downgrade to unverified
            return TaskExecutionResult(
                summary="Spoofed done",
                result="fake",
                outcome={"tool_name": "spoof", "status": "done", "verified": True, "summary": "Trust me", "checks": []},
            )

        async def failed_h(ctx, task):
            return TaskExecutionResult(
                summary="Command failed",
                result="",
                outcome=ToolOutcome(tool_name="cmd", status="failed", summary="Exit 1", error="non-zero exit").to_dict(),
            )

        async def started_h(ctx, task):
            return TaskExecutionResult(
                summary="Launched app",
                result="",
                outcome=ToolOutcome(tool_name="open_app", status="started", summary="PID launched only").to_dict(),
            )

        async def missing_side_effect_outcome_h(ctx, task):
            return TaskExecutionResult(summary="Forgot outcome", result="claimed", side_effect_expected=True)

        async def exploding_h(ctx, task):
            raise RuntimeError("boom")

        async def cancel_midway_h(ctx, task):
            await tasks.cancel_task(task["task_id"], reason="User clicked stop")
            return TaskExecutionResult(summary="Late finish", result="should stay cancelled")

        runner.register_handler("verified_job", verified_h)
        runner.register_handler("unverified_job", spoofed_unverified_h)
        runner.register_handler("failed_job", failed_h)
        runner.register_handler("started_job", started_h)
        runner.register_handler("side_effect_job", missing_side_effect_outcome_h)
        runner.register_handler("boom_job", exploding_h)
        runner.register_handler("cancel_job", cancel_midway_h)

        t_ok = await runner.enqueue("v", worker_kind="verified_job")
        t_unv = await runner.enqueue("u", worker_kind="unverified_job")
        t_fail = await runner.enqueue("f", worker_kind="failed_job")
        t_start = await runner.enqueue("s", worker_kind="started_job")
        t_se = await runner.enqueue("se", worker_kind="side_effect_job", intent="filesystem_ops")
        t_boom = await runner.enqueue("b", worker_kind="boom_job", max_attempts=1)
        t_can = await runner.enqueue("c", worker_kind="cancel_job")

        res = []
        for _ in range(7):
            r = await runner.run_once()
            if r:
                res.append(r)

        by_id = {x["task_id"]: x for x in res}
        assert by_id[t_ok["task_id"]]["status"] == "completed"
        assert by_id[t_ok["task_id"]]["meta"]["outcome"]["verified"] is True

        assert by_id[t_unv["task_id"]]["status"] == "unverified"
        assert by_id[t_unv["task_id"]]["meta"]["outcome"]["status"] == "unverified"
        assert by_id[t_unv["task_id"]]["meta"]["outcome"]["verified"] is False

        assert by_id[t_fail["task_id"]]["status"] == "failed"
        assert "non-zero exit" in by_id[t_fail["task_id"]]["error_info"]

        assert by_id[t_start["task_id"]]["status"] == "started"
        assert by_id[t_start["task_id"]]["meta"]["outcome"]["status"] == "started"

        assert by_id[t_se["task_id"]]["status"] == "unverified"
        assert by_id[t_se["task_id"]]["meta"]["outcome"]["status"] == "unverified"

        assert by_id[t_boom["task_id"]]["status"] == "failed"
        assert by_id[t_boom["task_id"]]["meta"]["outcome"]["status"] == "failed"

        assert by_id[t_can["task_id"]]["status"] == "cancelled"
        await db.close()

    asyncio.run(scenario())


def test_fix2_research_completed_but_export_failed(tmp_path: Path):
    async def scenario():
        db = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'fix2_exp.db').as_posix()}", data_dir=tmp_path)
        db.init_schema_sync()
        tasks = AssistantTaskService(db)
        runner = BackgroundTaskRunner(tasks)

        class _BrokenDocService:
            def generate(self, fmt: str, *, filename: str, title: str, payload: dict) -> dict:
                raise RuntimeError(f"Simulated {fmt} exporter crash")

        search = _FakeSearch(
            {
                "https://docs.example.org/wal": {
                    "title": "SQLite WAL",
                    "content": "SQLite WAL mode supports concurrent readers alongside a single writer.",
                }
            }
        )
        engine = DeepResearchEngine(
            data_dir=tmp_path,
            search_service=search,
            document_service=_BrokenDocService(),  # type: ignore[arg-type]
            online_allowed_fn=lambda: True,
        )
        runner.register_handler(
            "deep_research",
            lambda ctx, t: run_deep_research_background_task(ctx, t, engine=engine),
        )
        task = await runner.enqueue(
            "Deep research SQLite WAL concurrency",
            worker_kind="deep_research",
            intent="web_research",
            payload={"export_format": "pdf", "export_artifact": True},
        )
        out = await runner.run_once()
        assert out["task_id"] == task["task_id"]
        assert out["status"] == "completed"
        assert "# Deep Research Report" in out["result"]
        assert "SQLite WAL mode supports concurrent readers" in out["result"]
        assert out["generated_files"] == []
        assert out["meta"]["outcome"]["status"] == "done"
        assert out["meta"]["outcome"]["verified"] is True
        assert out["meta"]["artifact_outcome"]["status"] == "failed"
        assert out["meta"]["artifact_status"] == "failed"
        assert out["meta"]["partial_failure"] is True
        assert "exporter crash" in out["error_info"]
        await db.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# FIX 3: Strengthen Deep Research Claim Verification
# ---------------------------------------------------------------------------


def test_fix3_claim_support_contradictions_duplicates_inference_and_assessor(tmp_path: Path):
    snaps = {
        "S1": ResearchSnapshot(
            source_id="S1",
            title="Async Spec",
            url="https://example.org/spec",
            canonical_url="https://example.org/spec",
            source_type="web_page",
            sha256="aaa",
            fetched_at=1.0,
            snapshot_path=str(tmp_path / "s1.txt"),
            content="Python 3.14 free-threading build is officially supported on Linux with 18% overhead.",
        ),
        "S2": ResearchSnapshot(
            source_id="S2",
            title="Mirror Spec",
            url="https://example.org/spec/#mirror",
            canonical_url="https://example.org/spec",  # Duplicate URL of S1
            source_type="web_page",
            sha256="aaa",
            fetched_at=1.0,
            snapshot_path=str(tmp_path / "s2.txt"),
            content="Python 3.14 free-threading build is officially supported on Linux with 18% overhead.",
        ),
        "S3": ResearchSnapshot(
            source_id="S3",
            title="Vendor Advisory",
            url="https://vendor.example.net/advisory",
            canonical_url="https://vendor.example.net/advisory",
            source_type="web_page",
            sha256="bbb",
            fetched_at=1.0,
            snapshot_path=str(tmp_path / "s3.txt"),
            content="Python 3.14 free-threading build is not supported in production containers; Redis eviction uses LRU.",
        ),
        "S4": ResearchSnapshot(
            source_id="S4",
            title="Postgres Guide",
            url="https://pg.example.org/backup",
            canonical_url="https://pg.example.org/backup",
            source_type="web_page",
            sha256="ccc",
            fetched_at=1.0,
            snapshot_path=str(tmp_path / "s4.txt"),
            content="PostgreSQL logical replication streams WAL changes across standbys.",
        ),
    }

    claims = [
        # 0: Valid citation without quote -> MUST NOT remain verified_fact!
        ResearchClaim(
            statement="Redis eviction uses LRU algorithm.",
            kind="verified_fact",
            citations=["S3"],
            quote="",
        ),
        # 1: Valid quote + topical support -> grounded reported/verified (test non-contradicted PG fact)
        ResearchClaim(
            statement="PostgreSQL logical replication streams WAL changes across standbys.",
            kind="verified_fact",
            citations=["S4"],
            quote="PostgreSQL logical replication streams WAL changes",
        ),
        # 2: Contradicted claim (S1 supported vs S3 not supported)
        ResearchClaim(
            statement="Python 3.14 free-threading build is officially supported with 18% overhead.",
            kind="verified_fact",
            citations=["S1", "S2"],  # Also tests duplicate URL S1/S2 -> independent_source_count == 1!
            quote="free-threading build is officially supported on Linux with 18% overhead.",
        ),
        # 3: Valid but irrelevant source (S4 is Postgres, claim is Kubernetes GPU scheduling!)
        ResearchClaim(
            statement="Kubernetes schedules GPU pods using device plugins.",
            kind="verified_fact",
            citations=["S4"],
            quote="PostgreSQL logical replication streams WAL",
        ),
        # 4: Clearly labeled inference
        ResearchClaim(
            statement="Teams adopting free-threading may see deployment variation across runtimes.",
            kind="inference",
            citations=[],
        ),
        # 5: Unavailable source ID
        ResearchClaim(
            statement="Imaginary metric hits 5000 RPS.",
            kind="verified_fact",
            citations=["S99"],
            quote="5000 RPS",
        ),
        # 6: Unsupported numeric mismatch
        ResearchClaim(
            statement="Python 3.14 overhead is 99% on Linux.",
            kind="reported_claim",
            citations=["S1"],
            quote="officially supported on Linux",
        ),
        # 7: Quote not found verbatim in source
        ResearchClaim(
            statement="PostgreSQL logical replication streams WAL changes.",
            kind="verified_fact",
            citations=["S4"],
            quote="does not exist anywhere in postgres guide",
        ),
    ]

    rep = verify_claims_against_snapshots(claims, snaps, [])
    assert rep.passed is False
    assert rep.overall_status == "disputed"
    assert len(rep.duplicate_urls) == 1
    assert claims[2].independent_source_count == 1  # Duplicate URL did NOT count as 2 independent sources!
    assert claims[0].citation_valid is True and claims[0].quote_verified is False
    assert claims[1].citation_valid and claims[1].quote_verified and claims[1].evidence_supports_claim
    assert claims[2].evidence_contradicts_claim is True
    assert 3 in rep.unsupported_claims
    assert claims[4].verification_status == "inference"
    assert "S99" in rep.invalid_citations
    assert 6 in rep.unsupported_claims
    assert any(m["claim_index"] == 7 for m in rep.quote_mismatches)
    assert claims[7].quote_verified is False

    # Full engine run with bounded claim_assessor_fn asserting model cannot override failed deterministic checks
    engine = DeepResearchEngine(
        data_dir=tmp_path,
        search_service=_FakeSearch(
            {
                "https://pg.example.org/backup": {"title": "PG", "content": snaps["S4"].content},
                "https://example.org/spec": {"title": "Spec", "content": snaps["S1"].content},
                "https://vendor.example.net/advisory": {"title": "Adv", "content": snaps["S3"].content},
            }
        ),
        document_service=DocumentService(tmp_path / "docs"),
        online_allowed_fn=lambda: True,
        synthesizer_fn=lambda q, ws, st: {
            "summary": "Mixed findings",
            "claims": [
                {"statement": claims[0].statement, "kind": "verified_fact", "citations": ["S3"], "quote": ""},
                {"statement": claims[1].statement, "kind": "verified_fact", "citations": ["S1"], "quote": claims[1].quote},
                {"statement": claims[2].statement, "kind": "verified_fact", "citations": ["S2"], "quote": claims[2].quote},
                {"statement": claims[3].statement, "kind": "verified_fact", "citations": ["S1"], "quote": "PostgreSQL logical replication"},
                {"statement": claims[4].statement, "kind": "inference", "citations": []},
            ],
        },
        # Even if an optimistic LLM assessor claims supports=True on bad claims, deterministic checks must hold!
        claim_assessor_fn=lambda cl, ev: {"supports": True, "contradicts": False},
    )
    res = asyncio.run(engine.run(task_id="task-fix3", question="Test deep research verification", export_format="md"))
    md = res["markdown"]
    assert "Citation valid:" in md
    assert "Quotation verified:" in md
    assert "Evidence supports claim:" in md
    assert "Evidence contradicts claim:" in md
    assert "Claim Unverified" in md
    assert "(Inference)" in md
    assert res["verification"]["overall_status"] == "disputed"
    # Claim 0 (no quote on supported source) downgraded to reported_claim
    c0 = next(c for c in res["claims"] if "Redis eviction" in c["statement"])
    assert c0["kind"] == "reported_claim"
    # Claim 2 (contradicted) marked disputed, never verified_fact
    c2 = next(c for c in res["claims"] if "free-threading" in c["statement"] and c["kind"] != "inference")
    assert c2["kind"] == "disputed"


# ---------------------------------------------------------------------------
# FIX 4: Require Real File Modification Evidence
# ---------------------------------------------------------------------------


def test_fix4_verify_file_modified_real_noop_substring_missing_and_race(tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("alpha existing_token beta", encoding="utf-8")
    h0 = sha256_file(f)

    # 1) Existing expected substring WITHOUT prior hash -> UNVERIFIED (never done!)
    no_prior = verify_file_modified(f, expected_substring="existing_token")
    assert no_prior.status == ToolStatus.UNVERIFIED.value
    assert no_prior.ok is False

    # 2) Existing expected substring WITH unchanged hash -> FAILED / no-op (never done!)
    same_with_sub = verify_file_modified(f, previous_sha256=h0, expected_substring="existing_token")
    assert same_with_sub.status == ToolStatus.FAILED.value
    assert same_with_sub.evidence.get("noop") is True
    assert same_with_sub.ok is False

    # 3) No-op contract returning unverified when explicitly requested
    noop_unv = verify_file_modified(f, previous_sha256=h0, noop_status=ToolStatus.UNVERIFIED.value)
    assert noop_unv.status == ToolStatus.UNVERIFIED.value
    assert noop_unv.evidence.get("noop") is True

    # 4) Missing expected content even after real change -> FAILED
    f.write_text("alpha changed gamma", encoding="utf-8")
    missing_sub = verify_file_modified(f, previous_sha256=h0, expected_substring="never_written")
    assert missing_sub.status == ToolStatus.FAILED.value
    assert missing_sub.error == "expected content missing"

    # 5) Real modification with ActionExecutor + Root boundary check -> DONE
    fs = SafeFilesystem(FilesystemSpec(allowed_roots=[str(tmp_path)], allow_write=True))
    act = ActionExecutor(ActionsSpec(enabled=True, confirm_levels=[]), fs)
    mod_res = act.request_or_execute("modify_file", {"path": str(f), "content": "real new payload"})
    assert mod_res["ok"] is True
    assert mod_res["outcome"]["status"] == "done"
    assert mod_res["outcome"]["evidence"]["hash_changed"] is True

    # ActionExecutor no-op modify_file (re-writing identical content) -> must NOT claim done!
    noop_res = act.request_or_execute("modify_file", {"path": str(f), "content": "real new payload"})
    assert noop_res["ok"] is False
    assert noop_res["outcome"]["status"] == "failed"
    assert noop_res["outcome"]["evidence"]["noop"] is True

    # 6) File replaced by another process (expected_after_sha256 differs from disk) -> FAILED
    h_before = sha256_file(f)
    intended = hashlib.sha256(b"intended content").hexdigest()
    f.write_text("concurrent external overwrite", encoding="utf-8")
    raced = verify_file_modified(f, previous_sha256=h_before, expected_after_sha256=intended, root=tmp_path)
    assert raced.status == ToolStatus.FAILED.value
    assert raced.evidence.get("concurrent_modification") is True

    # 7) File removed during modification -> FAILED
    f.unlink()
    gone = verify_file_modified(f, previous_sha256=h_before)
    assert gone.status == ToolStatus.FAILED.value
    assert gone.error == "missing target file"


# ---------------------------------------------------------------------------
# FIX 5: Verify Snapshot Integrity on Reload & Recovery
# ---------------------------------------------------------------------------


def test_fix5_snapshot_integrity_on_reload_and_task_recovery(tmp_path: Path):
    async def scenario():
        db_url = f"sqlite+aiosqlite:///{(tmp_path / 'fix5.db').as_posix()}"
        db1 = DatabaseManager(db_url, data_dir=tmp_path)
        db1.init_schema_sync()
        tasks1 = AssistantTaskService(db1)

        engine1 = DeepResearchEngine(data_dir=tmp_path)
        task = await tasks1.create_task(
            "Deep research snapshot integrity",
            intent="web_research",
            status="running",
            worker_kind="deep_research",
            meta={"safe_resumable": True},
        )
        tid = task["task_id"]

        snap_valid = engine1._store_snapshot(
            tid,
            source_id="S1",
            title="Valid Source",
            url="https://valid.example.org/doc",
            source_type="web_page",
            content="Valid verified documentation confirms atomic commits work.",
        )
        snap_mod = engine1._store_snapshot(
            tid,
            source_id="S2",
            title="Modified Source",
            url="https://mod.example.org/doc",
            source_type="web_page",
            content="Original clean content about distributed locks.",
        )
        snap_trunc = engine1._store_snapshot(
            tid,
            source_id="S3",
            title="Truncated Source",
            url="https://trunc.example.org/doc",
            source_type="web_page",
            content="Complete specification with full checksum details.",
        )
        snap_miss = engine1._store_snapshot(
            tid,
            source_id="S4",
            title="Missing Source",
            url="https://miss.example.org/doc",
            source_type="web_page",
            content="Transient text should never fallback to unverified snippet.",
        )

        # Outside allowed task pages root!
        outside_secret = tmp_path / "secret_host.txt"
        outside_secret.write_text("TOP SECRET", encoding="utf-8")
        outside_meta = ResearchSnapshot(
            source_id="S5",
            title="Escaped Snapshot",
            url="https://escape.example.org",
            canonical_url="https://escape.example.org",
            source_type="web_page",
            sha256=hashlib.sha256(b"TOP SECRET").hexdigest(),
            fetched_at=time.time(),
            snapshot_path=str(outside_secret),
            content="TOP SECRET",
        ).to_metadata_dict()

        await tasks1.record_step_completion(
            tid,
            "step_plan",
            checkpoint_patch={
                "plan": {
                    "question": "Deep research snapshot integrity",
                    "subqueries": ["atomic commits"],
                    "expected_source_types": ["web_page"],
                    "key_claims_to_verify": ["atomic commits"],
                },
                "online_allowed": False,
            },
        )
        await tasks1.record_step_completion(
            tid,
            "step_investigate_0",
            checkpoint_patch={
                "snapshots": [
                    snap_valid.to_metadata_dict(),
                    snap_mod.to_metadata_dict(),
                    snap_trunc.to_metadata_dict(),
                    snap_miss.to_metadata_dict(),
                    outside_meta,
                ]
            },
        )
        await db1.close()

        # Corrupt S2, truncate S3, delete S4 on disk before reboot!
        Path(snap_mod.snapshot_path).write_text("INJECTED TAMPERED TEXT", encoding="utf-8")
        Path(snap_trunc.snapshot_path).write_bytes(b"Comp")
        Path(snap_miss.snapshot_path).unlink()

        # Verify standalone checkpoint loader rejects all 4 compromised snapshots while keeping S1 intact
        valid_only, rejected_list, int_warns = verify_checkpoint_snapshots(
            tid,
            {
                "snapshots": [
                    snap_valid.to_metadata_dict(),
                    snap_mod.to_metadata_dict(),
                    snap_trunc.to_metadata_dict(),
                    snap_miss.to_metadata_dict(),
                    outside_meta,
                ]
            },
            data_dir=tmp_path,
        )
        assert [v.source_id for v in valid_only] == ["S1"]
        assert len(rejected_list) == 4
        assert len(int_warns) == 4
        # Ensure corrupted file was NOT silently replaced with original text
        assert Path(snap_mod.snapshot_path).read_text(encoding="utf-8") == "INJECTED TAMPERED TEXT"

        # Reboot recovery MUST reverify snapshot integrity and log warnings while re-queuing safe task
        db2 = DatabaseManager(db_url, data_dir=tmp_path)
        tasks2 = AssistantTaskService(db2)
        engine2 = DeepResearchEngine(
            data_dir=tmp_path,
            document_service=DocumentService(tmp_path / "docs"),
            online_allowed_fn=lambda: False,
            synthesizer_fn=lambda q, ws, st: {
                "summary": "Mixed integrity recovery",
                "claims": [
                    {
                        "statement": "Valid verified documentation confirms atomic commits work.",
                        "kind": "verified_fact",
                        "citations": ["S1"],
                        "quote": "atomic commits work",
                    },
                    {
                        "statement": "Claim relying on corrupted snapshot S2.",
                        "kind": "verified_fact",
                        "citations": ["S2"],
                        "quote": "INJECTED TAMPERED TEXT",
                    },
                ],
            },
        )
        runner2 = BackgroundTaskRunner(tasks2)
        runner2.register_handler(
            "deep_research",
            lambda ctx, t: run_deep_research_background_task(ctx, t, engine=engine2),
        )
        rec = await runner2.start()
        assert tid in rec["requeued"]
        assert tid in rec["snapshot_integrity_warnings"]

        resumed = await runner2.run_once()
        await runner2.stop(grace_s=0.5)
        assert resumed["status"] == "completed"
        ver = resumed["meta"]["research_verification"]
        # Valid snapshot S1 remained verified; S2-dependent claim was rejected as unverified; integrity warnings preserved!
        assert ver["verified_claim_count"] == 1
        assert ver["unverified_claim_count"] == 1
        assert len(ver["integrity_warnings"]) >= 4
        assert "Snapshot Integrity" in resumed["result"]
        assert "INJECTED TAMPERED TEXT" not in resumed["result"]
        await db2.close()

    asyncio.run(scenario())

