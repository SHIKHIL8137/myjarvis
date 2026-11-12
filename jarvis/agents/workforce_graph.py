"""12-State LangGraph AI Development Workforce DAG with full base..HEAD manifest scan,
Docker/subprocess isolation, diff-pinned approval, & durable cancellation checkpoints."""
from enum import Enum
import inspect
from pathlib import Path
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from ..integrations import git_worktree, graphify_adapter, opencode_adapter
from ..memory.five_way import FiveWayMemoryService
from .approvals import ApprovalService


class WorkforceState(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    QA_PENDING = "QA_PENDING"
    QA_FAILED = "QA_FAILED"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


WORKFORCE_TRANSITIONS: dict[WorkforceState, set[WorkforceState]] = {
    WorkforceState.PLANNING: {WorkforceState.RUNNING, WorkforceState.BLOCKED, WorkforceState.FAILED, WorkforceState.CANCELLED},
    WorkforceState.RUNNING: {WorkforceState.QA_PENDING, WorkforceState.BLOCKED, WorkforceState.FAILED, WorkforceState.CANCELLED},
    WorkforceState.QA_PENDING: {WorkforceState.SECURITY_REVIEW, WorkforceState.QA_FAILED, WorkforceState.FAILED, WorkforceState.CANCELLED},
    WorkforceState.QA_FAILED: {WorkforceState.RUNNING, WorkforceState.FAILED, WorkforceState.BLOCKED, WorkforceState.CANCELLED},
    WorkforceState.SECURITY_REVIEW: {WorkforceState.APPROVAL_PENDING, WorkforceState.BLOCKED, WorkforceState.FAILED, WorkforceState.CANCELLED},
    WorkforceState.APPROVAL_PENDING: {WorkforceState.APPROVED, WorkforceState.CANCELLED, WorkforceState.BLOCKED, WorkforceState.FAILED},
    WorkforceState.APPROVED: {WorkforceState.MERGING, WorkforceState.BLOCKED, WorkforceState.FAILED, WorkforceState.CANCELLED},
    WorkforceState.MERGING: {WorkforceState.COMPLETED, WorkforceState.FAILED, WorkforceState.BLOCKED},
    WorkforceState.COMPLETED: set(),
    WorkforceState.BLOCKED: {WorkforceState.CANCELLED},
    WorkforceState.FAILED: {WorkforceState.CANCELLED},
    WorkforceState.CANCELLED: set(),
}


def validate_transition(current: str | WorkforceState | None, nxt: WorkforceState) -> str:
    """Validate explicit workflow state transition and raise ValueError on illegal jumps."""
    if current is None:
        return nxt.value
    cur_enum = current if isinstance(current, WorkforceState) else WorkforceState(str(current))
    if nxt == cur_enum:
        return nxt.value
    allowed = WORKFORCE_TRANSITIONS.get(cur_enum, set())
    if nxt not in allowed:
        raise ValueError(f"Invalid workforce state transition: {cur_enum.value} -> {nxt.value}")
    return nxt.value


class WorkforceGraphState(TypedDict, total=False):
    run_id: str
    project_id: str
    project_name: str
    task_prompt: str
    target_symbol: str
    qa_command: list[str]
    model: str
    max_retries: int
    retry_count: int
    workflow_state: str
    cancelled: bool
    cancel_reason: str
    graph_stats: dict
    impact_context: dict
    plan: str
    selected_skills: list[dict]
    skill_prompt_block: str
    worktree_path: str
    branch_name: str
    worktree_base_sha: str
    execution_policy_caps: dict
    coder_result: dict
    qa_result: dict
    diff_info: dict
    security_scan: dict
    approved_base_sha: str
    approved_head_sha: str
    approved_manifest_sha: str
    approved_diff_hash: str
    approved_fingerprint: str
    approval_token: str
    approved: bool
    merge_result: dict
    lesson_recorded: dict
    status: str
    summary: str


def build_workforce_graph(
    *,
    projects: ProjectRegistryService,
    workforce: DevWorkforceService,
    memory: FiveWayMemoryService,
    approvals: ApprovalService,
    workforce_spec=None,
    coder_runner: Callable[..., dict] | None = None,
    is_cancelled_fn: Callable[[str], tuple[bool, str]] | None = None,
    capability_router=None,
    token_manager=None,
    gateway_port: int = 4700,
    graphify_bin: str = "graphify",
    opencode_bin: str = "opencode",
    checkpointer=None,
):
    coder_fn = coder_runner or opencode_adapter.run_opencode_task
    coder_params = inspect.signature(coder_fn).parameters

    def _cancelled(state: WorkforceGraphState) -> tuple[bool, str]:
        if state.get("cancelled"):
            return True, state.get("cancel_reason") or "Cancelled"
        rid = state.get("run_id") or ""
        if rid and is_cancelled_fn:
            return is_cancelled_fn(rid)
        return False, ""

    def _cancel_patch(state: WorkforceGraphState, reason: str) -> dict:
        cur = state.get("workflow_state")
        wf_cancel = validate_transition(cur, WorkforceState.CANCELLED) if cur else WorkforceState.CANCELLED.value
        if state.get("worktree_path"):
            workforce.update_worktree_status(state["worktree_path"], "cancelled_preserved")
        return {
            "cancelled": True,
            "cancel_reason": reason,
            "approved": False,
            "workflow_state": wf_cancel,
            "status": "cancelled",
            "summary": f"Workflow cancelled cleanly: {reason} (worktree preserved).",
        }

    def sync_graphify(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        wf_state = validate_transition(None, WorkforceState.PLANNING)
        proj = projects.get(state["project_id"])
        root = Path(proj["root_path"])
        try:
            g_res = projects.sync_graphify(state["project_id"], bin_name=graphify_bin, no_cluster=True)
        except Exception as e:
            g_res = {"ok": False, "error": str(e), "node_count": 0, "edge_count": 0}
        return {
            "project_name": proj["name"],
            "graph_stats": g_res,
            "workflow_state": wf_state,
            "status": "running",
            "retry_count": 0,
            "max_retries": int(state.get("max_retries", 2)),
            "_root": str(root),
        }

    def impact_and_context(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        proj = projects.get(state["project_id"])
        root = Path(proj["root_path"])
        sym = state.get("target_symbol") or ""
        q_out = ""
        aff_out = ""
        if graphify_adapter.graph_artifact_path(root).exists():
            q_out = graphify_adapter.query_graph(root, state["task_prompt"], bin_name=graphify_bin).get("output", "")
            if sym:
                aff_out = graphify_adapter.affected_nodes(root, sym, bin_name=graphify_bin).get("output", "")
        proj_mem = memory.search_project_knowledge(project_id=state["project_id"], query=state["task_prompt"], k=3)
        exp_mem = memory.search_experience(state["task_prompt"], scope="dev", project_id=state["project_id"], k=3)
        sel_skills: list[dict] = []
        skill_block = ""
        if capability_router is not None:
            rt = "opencode" if coder_fn is opencode_adapter.run_opencode_task else "jarvis_native"
            sel = capability_router.select_skills_for_task(
                task_prompt=state["task_prompt"],
                intent="workforce_coding",
                department="dev",
                complexity="heavy_coding",
                agent_role="coder_agent",
                project_id=state["project_id"],
                runtime=rt,
            )
            sel_skills = list(sel.get("selected") or [])
            skill_block = str(sel.get("combined_prompt_block") or "")
        return {
            "selected_skills": sel_skills,
            "skill_prompt_block": skill_block,
            "impact_context": {
                "graph_query": q_out[:2000],
                "affected": aff_out[:2000],
                "project_hits": proj_mem,
                "experience_hits": exp_mem,
                "selected_skills": sel_skills,
            },
        }

    def architect_plan(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        ctx = state.get("impact_context") or {}
        plan = (
            f"Objective: {state['task_prompt']}\n"
            f"Project: {state.get('project_name') or state['project_id']}\n"
            f"Graphify Nodes: {(state.get('graph_stats') or {}).get('node_count', 0)}\n"
            f"Prior Lessons: {len(ctx.get('experience_hits') or [])}\n"
            "Execution: Isolated Git worktree -> Sandboxed Coder -> QA Gate -> Full Base..HEAD Security Scan -> Diff-Pinned Owner Approval -> Merge."
        )
        return {"plan": plan}

    def open_worktree(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.RUNNING)
        run_id = state.get("run_id") or "task"
        wt = workforce.open_worktree(state["project_id"], run_id, ensure_unique=True)
        policy = workforce.build_execution_policy(Path(wt["worktree_path"]), workforce_spec=workforce_spec)
        return {
            "worktree_path": wt["worktree_path"],
            "branch_name": wt["branch"],
            "worktree_base_sha": wt.get("base_sha") or wt.get("head_sha", ""),
            "execution_policy_caps": policy.capabilities(),
            "workflow_state": wf_state,
        }

    def coder_opencode(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        wf_state = state.get("workflow_state") or WorkforceState.RUNNING.value
        if wf_state == WorkforceState.QA_FAILED.value:
            wf_state = validate_transition(wf_state, WorkforceState.RUNNING)
        proj = projects.get(state["project_id"])
        wt_path = Path(state["worktree_path"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), wt_path, state.get("run_id", ""))
        prev_qa = state.get("qa_result") or {}
        retry_note = ""
        if prev_qa and not prev_qa.get("passed", True):
            retry_note = f"\n\nPrevious QA check failed (rc={prev_qa.get('returncode')}):\n{prev_qa.get('stdout', '')[:1200]}\n{prev_qa.get('stderr', '')[:800]}"
        skill_note = ""
        if state.get("skill_prompt_block"):
            skill_note = f"\n\n### Validated Active Skills (System Policy Always Precedes)\n{state['skill_prompt_block']}"
        if capability_router is not None and coder_fn is opencode_adapter.run_opencode_task and (state.get("selected_skills") or []):
            capability_router.materialize_for_worktree({"selected": state["selected_skills"]}, worktree_path=wt_path, runtime="opencode", allow_scripts=False)
        prompt = f"{state['task_prompt']}{skill_note}{retry_note}"
        policy = workforce.build_execution_policy(wt_path, workforce_spec=workforce_spec)

        kwargs: dict = {"workdir": state["worktree_path"], "model": state.get("model"), "bin_name": opencode_bin}
        if "policy" in coder_params:
            kwargs["policy"] = policy
        if "run_id" in coder_params:
            kwargs["run_id"] = state.get("run_id")
        if "cancel_check" in coder_params:
            kwargs["cancel_check"] = lambda: _cancelled(state)[0]
        if "token_manager" in coder_params and token_manager is not None:
            kwargs["token_manager"] = token_manager
        if "gateway_port" in coder_params:
            kwargs["gateway_port"] = gateway_port

        try:
            res = coder_fn(prompt, **kwargs)
        except Exception as e:
            c2, rsn2 = _cancelled(state)
            if c2 or "cancelled" in str(e).lower():
                return _cancel_patch(state, rsn2 or str(e))
            raise
        c2, rsn2 = _cancelled(state)
        if c2:
            return _cancel_patch(state, rsn2)
        return {"coder_result": res, "workflow_state": wf_state}

    def qa_verify(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.QA_PENDING)
        proj = projects.get(state["project_id"])
        wt_path = Path(state["worktree_path"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), wt_path, state.get("run_id", ""))
        cmd = state.get("qa_command") or proj["test_cmd"].split()
        policy = workforce.build_execution_policy(wt_path, workforce_spec=workforce_spec)
        res = git_worktree.run_worktree_command(
            wt_path,
            list(cmd),
            policy=policy,
            run_id=state.get("run_id"),
            cancel_check=lambda: _cancelled(state)[0],
        )
        c2, rsn2 = _cancelled(state)
        if c2:
            return _cancel_patch(state, rsn2)
        retries = int(state.get("retry_count", 0))
        if not res["passed"]:
            retries += 1
            wf_state = validate_transition(wf_state, WorkforceState.QA_FAILED)
        return {"qa_result": res, "retry_count": retries, "workflow_state": wf_state}

    def after_qa(state: WorkforceGraphState) -> str:
        if state.get("workflow_state") == WorkforceState.CANCELLED.value or _cancelled(state)[0]:
            return "cancelled_halt"
        qa = state.get("qa_result") or {}
        if qa.get("passed") is True:
            return "security_diff_review"
        if int(state.get("retry_count", 0)) <= int(state.get("max_retries", 2)):
            return "coder_opencode"
        return "qa_exhausted_halt"

    def cancelled_halt(state: WorkforceGraphState) -> dict:
        if state.get("workflow_state") == WorkforceState.CANCELLED.value:
            return {}
        _, rsn = _cancelled(state)
        return _cancel_patch(state, rsn or "Cancelled by owner")

    def qa_exhausted_halt(state: WorkforceGraphState) -> dict:
        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.FAILED)
        workforce.update_worktree_status(state["worktree_path"], "qa_failed")
        qa = state.get("qa_result") or {}
        return {
            "approved": False,
            "workflow_state": wf_state,
            "status": "failed",
            "summary": f"QA verification failed after {state.get('retry_count', 0)} attempt(s) (rc={qa.get('returncode')}); merge blocked.",
        }

    def security_diff_review(state: WorkforceGraphState) -> dict:
        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)
        qa = state.get("qa_result") or {}
        if not qa.get("passed"):
            wf_state = validate_transition(state.get("workflow_state"), WorkforceState.FAILED)
            workforce.update_worktree_status(state["worktree_path"], "qa_failed")
            return {"approved": False, "workflow_state": wf_state, "status": "failed", "summary": "Blocked: QA did not pass."}

        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.SECURITY_REVIEW)
        proj = projects.get(state["project_id"])
        wt_path = Path(state["worktree_path"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), wt_path, state.get("run_id", ""))

        df = git_worktree.build_change_manifest(wt_path, base_sha=state.get("worktree_base_sha"))
        scan = workforce.security_scan_worktree(df, wt_path)
        compact_df = {k: v for k, v in df.items() if k != "committed_diff"}
        if not scan["clean"]:
            wf_blocked = validate_transition(wf_state, WorkforceState.BLOCKED)
            workforce.update_worktree_status(wt_path, "blocked_security")
            return {
                "diff_info": compact_df,
                "security_scan": scan,
                "approved": False,
                "workflow_state": wf_blocked,
                "status": "blocked_security",
                "summary": f"Merge blocked by security scan ({len(scan['findings'])} finding(s)).",
            }

        c2, rsn2 = _cancelled(state)
        if c2:
            return _cancel_patch(state, rsn2)

        wf_pending = validate_transition(wf_state, WorkforceState.APPROVAL_PENDING)
        manifest_sha = df["manifest_sha256"]
        base_sha = df["base_sha"]
        head_sha = df["head_sha"]
        diff_hash = df["diff_hash"]
        fp_sha = df["approval_fingerprint"]
        test_cmd_str = " ".join(qa.get("argv") or [])
        changed_summary = [
            {"path": e["path"], "old_path": e.get("old_path"), "change_type": e["change_type"], "committed": e.get("committed"), "security_result": e["security_result"]}
            for e in scan["manifest"]
        ]
        req = approvals.create_request(
            action_type="merge_worktree",
            target_resource=f"{proj['name']}: {state.get('branch_name')} -> {proj.get('default_branch', 'main')}",
            preview_diff=(df.get("committed_diff", "") + "\n" + df.get("diff", "")).strip() or df.get("status_short", "(no diff)"),
            args={
                "project_id": state["project_id"],
                "project_name": proj["name"],
                "task_description": state["task_prompt"],
                "branch": state.get("branch_name"),
                "worktree_path": state.get("worktree_path"),
                "base_sha": base_sha,
                "head_sha": head_sha,
                "diff_hash": diff_hash,
                "manifest_sha256": manifest_sha,
                "approval_fingerprint": fp_sha,
                "changed_files": changed_summary,
                "diff_summary": df.get("status_short", ""),
                "qa_passed": True,
                "test_commands": test_cmd_str,
                "test_results": {"returncode": qa.get("returncode"), "stdout_tail": (qa.get("stdout") or "")[-800:]},
                "security_clean": True,
                "warnings": scan.get("warnings") or [],
            },
            run_id=state.get("run_id"),
        )
        return {
            "diff_info": compact_df,
            "security_scan": scan,
            "approved_base_sha": base_sha,
            "approved_head_sha": head_sha,
            "approved_manifest_sha": manifest_sha,
            "approved_diff_hash": diff_hash,
            "approved_fingerprint": fp_sha,
            "approval_token": req["token"],
            "workflow_state": wf_pending,
            "status": "awaiting_approval",
        }

    def human_merge_gate(state: WorkforceGraphState) -> dict:
        proj = projects.get(state["project_id"])
        req_payload = {
            "approval_token": state.get("approval_token"),
            "project_name": proj["name"],
            "task_description": state.get("task_prompt"),
            "branch": state.get("branch_name"),
            "base_sha": state.get("approved_base_sha"),
            "head_sha": state.get("approved_head_sha"),
            "approval_fingerprint": state.get("approved_fingerprint"),
            "changed_files": [e.get("path") for e in ((state.get("diff_info") or {}).get("manifest") or [])],
            "qa_passed": (state.get("qa_result") or {}).get("passed"),
            "security_clean": (state.get("security_scan") or {}).get("clean"),
            "warnings": (state.get("security_scan") or {}).get("warnings") or [],
        }
        decision = interrupt(req_payload)

        c, rsn = _cancelled(state)
        if c:
            return _cancel_patch(state, rsn)

        approved = bool(decision.get("approved") if isinstance(decision, dict) else decision)
        root = Path(proj["root_path"])
        wt = Path(state["worktree_path"])

        if not approved:
            wf_cancel = validate_transition(state.get("workflow_state"), WorkforceState.CANCELLED)
            git_worktree.cleanup_worktree(root, wt, state.get("branch_name"), authorized_uncommitted=True, expected_task_id=state.get("run_id"))
            workforce.update_worktree_status(wt, "rejected_cleaned")
            return {"approved": False, "workflow_state": wf_cancel, "status": "rejected", "summary": "Merge rejected by owner; isolated worktree cleaned."}

        if not (state.get("qa_result") or {}).get("passed"):
            wf_fail = validate_transition(state.get("workflow_state"), WorkforceState.FAILED)
            return {"approved": False, "workflow_state": wf_fail, "status": "failed", "summary": "Blocked merge: mandatory QA checks did not pass."}

        git_worktree.verify_worktree_ownership(root, wt, state.get("run_id", ""))
        fresh_df = git_worktree.build_change_manifest(wt, base_sha=state.get("worktree_base_sha"))
        fresh_scan = workforce.security_scan_worktree(fresh_df, wt)

        fp_mismatch = (
            fresh_df["base_sha"] != state.get("approved_base_sha")
            or fresh_df["head_sha"] != state.get("approved_head_sha")
            or fresh_df["manifest_sha256"] != state.get("approved_manifest_sha")
            or fresh_df["diff_hash"] != state.get("approved_diff_hash")
            or fresh_df["approval_fingerprint"] != state.get("approved_fingerprint")
        )
        if fp_mismatch:
            wf_blk = validate_transition(state.get("workflow_state"), WorkforceState.BLOCKED)
            workforce.update_worktree_status(wt, "approval_invalidated")
            return {
                "approved": False,
                "workflow_state": wf_blk,
                "status": "blocked",
                "summary": "Blocked merge: branch HEAD, diff, or files changed after security review; approval invalidated.",
            }
        if not fresh_scan["clean"]:
            wf_blk = validate_transition(state.get("workflow_state"), WorkforceState.BLOCKED)
            workforce.update_worktree_status(wt, "blocked_security")
            return {
                "approved": False,
                "workflow_state": wf_blk,
                "status": "blocked_security",
                "summary": "Blocked merge: critical security issue detected during final verification.",
            }

        wf_app = validate_transition(state.get("workflow_state"), WorkforceState.APPROVED)
        wf_merging = validate_transition(wf_app, WorkforceState.MERGING)
        from jarvis.security.outcomes import verify_git_state

        mres = git_worktree.merge_worktree(root, state["branch_name"], wt, commit_msg=state["task_prompt"][:100])
        merge_outcome = verify_git_state(
            root,
            tool_name="worktree_merge",
            expect_head_changed_from=state.get("worktree_base_sha"),
        )
        git_worktree.cleanup_worktree(root, wt, state.get("branch_name"), authorized_uncommitted=False, expected_task_id=state.get("run_id"))
        workforce.update_worktree_status(wt, "merged")
        wf_done = validate_transition(wf_merging, WorkforceState.COMPLETED)
        try:
            projects.sync_graphify(state["project_id"], bin_name=graphify_bin, no_cluster=True)
        except Exception:
            pass
        return {
            "approved": True,
            "merge_result": mres,
            "merge_outcome": merge_outcome.to_dict(),
            "workflow_state": wf_done,
            "status": "completed" if merge_outcome.ok else merge_outcome.status,
            "summary": (
                f"Verified, merged {state['branch_name']}, and cleaned worktree."
                if merge_outcome.ok
                else f"Merged {state['branch_name']}, post-condition status: {merge_outcome.format_for_assistant()}"
            ),
        }

    def extract_lesson(state: WorkforceGraphState) -> dict:
        if not state.get("approved"):
            return {}
        lesson = memory.add_experience_lesson(
            scope="dev",
            category="workflow_lesson",
            project_id=state["project_id"],
            topic="coding_worktree",
            trigger_context=state["task_prompt"],
            lesson_after=f"Verified via QA command in worktree {state.get('branch_name')} and merged cleanly.",
            approved=True,
            source="workforce_verified",
        )
        return {"lesson_recorded": lesson}

    def _route_unless_cancelled(next_node: str):
        return lambda s: "cancelled_halt" if (s.get("workflow_state") == WorkforceState.CANCELLED.value or _cancelled(s)[0]) else next_node

    g = StateGraph(WorkforceGraphState)
    g.add_node("sync_graphify", sync_graphify)
    g.add_node("impact_and_context", impact_and_context)
    g.add_node("architect_plan", architect_plan)
    g.add_node("open_worktree", open_worktree)
    g.add_node("coder_opencode", coder_opencode)
    g.add_node("qa_verify", qa_verify)
    g.add_node("qa_exhausted_halt", qa_exhausted_halt)
    g.add_node("cancelled_halt", cancelled_halt)
    g.add_node("security_diff_review", security_diff_review)
    g.add_node("human_merge_gate", human_merge_gate)
    g.add_node("extract_lesson", extract_lesson)

    g.add_edge(START, "sync_graphify")
    g.add_conditional_edges("sync_graphify", _route_unless_cancelled("impact_and_context"), {"impact_and_context": "impact_and_context", "cancelled_halt": "cancelled_halt"})
    g.add_conditional_edges("impact_and_context", _route_unless_cancelled("architect_plan"), {"architect_plan": "architect_plan", "cancelled_halt": "cancelled_halt"})
    g.add_conditional_edges("architect_plan", _route_unless_cancelled("open_worktree"), {"open_worktree": "open_worktree", "cancelled_halt": "cancelled_halt"})
    g.add_conditional_edges("open_worktree", _route_unless_cancelled("coder_opencode"), {"coder_opencode": "coder_opencode", "cancelled_halt": "cancelled_halt"})
    g.add_conditional_edges("coder_opencode", _route_unless_cancelled("qa_verify"), {"qa_verify": "qa_verify", "cancelled_halt": "cancelled_halt"})
    g.add_conditional_edges(
        "qa_verify",
        after_qa,
        {
            "coder_opencode": "coder_opencode",
            "security_diff_review": "security_diff_review",
            "qa_exhausted_halt": "qa_exhausted_halt",
            "cancelled_halt": "cancelled_halt",
        },
    )
    g.add_edge("qa_exhausted_halt", "extract_lesson")
    g.add_edge("cancelled_halt", END)
    g.add_conditional_edges(
        "security_diff_review",
        lambda s: (
            "cancelled_halt"
            if (s.get("workflow_state") == WorkforceState.CANCELLED.value or _cancelled(s)[0])
            else ("human_merge_gate" if s.get("workflow_state") == WorkforceState.APPROVAL_PENDING.value else "extract_lesson")
        ),
        {"human_merge_gate": "human_merge_gate", "extract_lesson": "extract_lesson", "cancelled_halt": "cancelled_halt"},
    )
    g.add_edge("human_merge_gate", "extract_lesson")
    g.add_edge("extract_lesson", END)
    return g.compile(checkpointer=checkpointer)
