"""12-State LangGraph AI Development Workforce DAG with mandatory QA, manifest scanning, & diff-pinned approval."""
from enum import Enum
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
    """Validate explicit workflow state transition and raise ValueError on illegal jumps (e.g. QA_FAILED -> MERGING)."""
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
    graph_stats: dict
    impact_context: dict
    plan: str
    worktree_path: str
    branch_name: str
    worktree_base_sha: str
    execution_policy_caps: dict
    coder_result: dict
    qa_result: dict
    diff_info: dict
    security_scan: dict
    approved_manifest_sha: str
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
    graphify_bin: str = "graphify",
    opencode_bin: str = "opencode",
    checkpointer=None,
):
    coder_fn = coder_runner or opencode_adapter.run_opencode_task

    def sync_graphify(state: WorkforceGraphState) -> dict:
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
        return {
            "impact_context": {
                "graph_query": q_out[:2000],
                "affected": aff_out[:2000],
                "project_hits": proj_mem,
                "experience_hits": exp_mem,
            }
        }

    def architect_plan(state: WorkforceGraphState) -> dict:
        ctx = state.get("impact_context") or {}
        plan = (
            f"Objective: {state['task_prompt']}\n"
            f"Project: {state.get('project_name') or state['project_id']}\n"
            f"Graphify Nodes: {(state.get('graph_stats') or {}).get('node_count', 0)}\n"
            f"Prior Lessons: {len(ctx.get('experience_hits') or [])}\n"
            "Execution: Isolated Git worktree -> QA verification gate -> Full-manifest security scan -> Diff-pinned owner approval -> Merge."
        )
        return {"plan": plan}

    def open_worktree(state: WorkforceGraphState) -> dict:
        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.RUNNING)
        run_id = state.get("run_id") or "task"
        wt = workforce.open_worktree(state["project_id"], run_id, ensure_unique=True)
        policy = workforce.build_execution_policy(Path(wt["worktree_path"]), workforce_spec=workforce_spec)
        return {
            "worktree_path": wt["worktree_path"],
            "branch_name": wt["branch"],
            "worktree_base_sha": wt.get("head_sha", ""),
            "execution_policy_caps": policy.capabilities(),
            "workflow_state": wf_state,
        }

    def coder_opencode(state: WorkforceGraphState) -> dict:
        wf_state = state.get("workflow_state") or WorkforceState.RUNNING.value
        if wf_state == WorkforceState.QA_FAILED.value:
            wf_state = validate_transition(wf_state, WorkforceState.RUNNING)
        proj = projects.get(state["project_id"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), Path(state["worktree_path"]), state.get("run_id", ""))
        prev_qa = state.get("qa_result") or {}
        retry_note = ""
        if prev_qa and not prev_qa.get("passed", True):
            retry_note = f"\n\nPrevious QA check failed (rc={prev_qa.get('returncode')}):\n{prev_qa.get('stdout', '')[:1200]}\n{prev_qa.get('stderr', '')[:800]}"
        prompt = f"{state['task_prompt']}{retry_note}"
        res = coder_fn(
            prompt,
            workdir=state["worktree_path"],
            model=state.get("model"),
            bin_name=opencode_bin,
        )
        return {"coder_result": res, "workflow_state": wf_state}

    def qa_verify(state: WorkforceGraphState) -> dict:
        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.QA_PENDING)
        proj = projects.get(state["project_id"])
        wt_path = Path(state["worktree_path"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), wt_path, state.get("run_id", ""))
        cmd = state.get("qa_command") or proj["test_cmd"].split()
        policy = workforce.build_execution_policy(wt_path, workforce_spec=workforce_spec)
        res = git_worktree.run_worktree_command(wt_path, list(cmd), policy=policy)
        retries = int(state.get("retry_count", 0))
        if not res["passed"]:
            retries += 1
            wf_state = validate_transition(wf_state, WorkforceState.QA_FAILED)
        return {"qa_result": res, "retry_count": retries, "workflow_state": wf_state}

    def after_qa(state: WorkforceGraphState) -> str:
        qa = state.get("qa_result") or {}
        if qa.get("passed") is True:
            return "security_diff_review"
        if int(state.get("retry_count", 0)) <= int(state.get("max_retries", 2)):
            return "coder_opencode"
        return "qa_exhausted_halt"

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
        qa = state.get("qa_result") or {}
        if not qa.get("passed"):
            wf_state = validate_transition(state.get("workflow_state"), WorkforceState.FAILED)
            workforce.update_worktree_status(state["worktree_path"], "qa_failed")
            return {"approved": False, "workflow_state": wf_state, "status": "failed", "summary": "Blocked: QA did not pass."}

        wf_state = validate_transition(state.get("workflow_state"), WorkforceState.SECURITY_REVIEW)
        proj = projects.get(state["project_id"])
        wt_path = Path(state["worktree_path"])
        git_worktree.verify_worktree_ownership(Path(proj["root_path"]), wt_path, state.get("run_id", ""))

        df = git_worktree.build_change_manifest(wt_path)
        scan = workforce.security_scan_worktree(df, wt_path)
        if not scan["clean"]:
            wf_blocked = validate_transition(wf_state, WorkforceState.BLOCKED)
            workforce.update_worktree_status(wt_path, "blocked_security")
            return {
                "diff_info": {k: v for k, v in df.items() if k != "untracked_corpus"},
                "security_scan": scan,
                "approved": False,
                "workflow_state": wf_blocked,
                "status": "blocked_security",
                "summary": f"Merge blocked by security scan ({len(scan['findings'])} finding(s)).",
            }

        wf_pending = validate_transition(wf_state, WorkforceState.APPROVAL_PENDING)
        manifest_sha = df["manifest_sha256"]
        test_cmd_str = " ".join(qa.get("argv") or [])
        changed_summary = [
            {"path": e["path"], "change_type": e["change_type"], "security_result": e["security_result"], "qa_relevant": e["qa_relevant"]}
            for e in scan["manifest"]
        ]
        req = approvals.create_request(
            action_type="merge_worktree",
            target_resource=f"{proj['name']}: {state.get('branch_name')} -> {proj.get('default_branch', 'main')}",
            preview_diff=df.get("diff", "") or df.get("status_short", "(no diff)"),
            args={
                "project_id": state["project_id"],
                "project_name": proj["name"],
                "task_description": state["task_prompt"],
                "branch": state.get("branch_name"),
                "worktree_path": state.get("worktree_path"),
                "commit_sha": state.get("worktree_base_sha", ""),
                "changed_files": changed_summary,
                "diff_summary": df.get("status_short", ""),
                "manifest_sha256": manifest_sha,
                "qa_passed": True,
                "test_commands": test_cmd_str,
                "test_results": {"returncode": qa.get("returncode"), "stdout_tail": (qa.get("stdout") or "")[-800:]},
                "security_clean": True,
                "warnings": scan.get("warnings") or [],
            },
            run_id=state.get("run_id"),
        )
        return {
            "diff_info": {k: v for k, v in df.items() if k != "untracked_corpus"},
            "security_scan": scan,
            "approved_manifest_sha": manifest_sha,
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
            "changed_files": [e.get("path") for e in ((state.get("diff_info") or {}).get("manifest") or [])],
            "manifest_sha256": state.get("approved_manifest_sha"),
            "qa_passed": (state.get("qa_result") or {}).get("passed"),
            "security_clean": (state.get("security_scan") or {}).get("clean"),
            "warnings": (state.get("security_scan") or {}).get("warnings") or [],
            "commit_sha": state.get("worktree_base_sha", ""),
        }
        decision = interrupt(req_payload)
        approved = bool(decision.get("approved") if isinstance(decision, dict) else decision)
        root = Path(proj["root_path"])
        wt = Path(state["worktree_path"])

        if not approved:
            wf_cancel = validate_transition(state.get("workflow_state"), WorkforceState.CANCELLED)
            git_worktree.cleanup_worktree(root, wt, state.get("branch_name"), authorized_uncommitted=True, expected_task_id=state.get("run_id"))
            workforce.update_worktree_status(wt, "rejected_cleaned")
            return {"approved": False, "workflow_state": wf_cancel, "status": "rejected", "summary": "Merge rejected by owner; isolated worktree cleaned."}

        # Mandatory safety enforcement even when owner approved
        if not (state.get("qa_result") or {}).get("passed"):
            wf_fail = validate_transition(state.get("workflow_state"), WorkforceState.FAILED)
            return {"approved": False, "workflow_state": wf_fail, "status": "failed", "summary": "Blocked merge: mandatory QA checks did not pass."}

        git_worktree.verify_worktree_ownership(root, wt, state.get("run_id", ""))
        fresh_df = git_worktree.build_change_manifest(wt)
        fresh_scan = workforce.security_scan_worktree(fresh_df, wt)
        if fresh_df["manifest_sha256"] != state.get("approved_manifest_sha"):
            wf_blk = validate_transition(state.get("workflow_state"), WorkforceState.BLOCKED)
            workforce.update_worktree_status(wt, "approval_invalidated")
            return {
                "approved": False,
                "workflow_state": wf_blk,
                "status": "blocked",
                "summary": "Blocked merge: worktree changed after security review, invalidating prior approval. Fresh review required.",
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
        mres = git_worktree.merge_worktree(root, state["branch_name"], wt, commit_msg=state["task_prompt"][:100])
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
            "workflow_state": wf_done,
            "status": "completed",
            "summary": f"Verified, merged {state['branch_name']}, and cleaned worktree.",
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

    g = StateGraph(WorkforceGraphState)
    g.add_node("sync_graphify", sync_graphify)
    g.add_node("impact_and_context", impact_and_context)
    g.add_node("architect_plan", architect_plan)
    g.add_node("open_worktree", open_worktree)
    g.add_node("coder_opencode", coder_opencode)
    g.add_node("qa_verify", qa_verify)
    g.add_node("qa_exhausted_halt", qa_exhausted_halt)
    g.add_node("security_diff_review", security_diff_review)
    g.add_node("human_merge_gate", human_merge_gate)
    g.add_node("extract_lesson", extract_lesson)

    g.add_edge(START, "sync_graphify")
    g.add_edge("sync_graphify", "impact_and_context")
    g.add_edge("impact_and_context", "architect_plan")
    g.add_edge("architect_plan", "open_worktree")
    g.add_edge("open_worktree", "coder_opencode")
    g.add_edge("coder_opencode", "qa_verify")
    g.add_conditional_edges(
        "qa_verify",
        after_qa,
        {
            "coder_opencode": "coder_opencode",
            "security_diff_review": "security_diff_review",
            "qa_exhausted_halt": "qa_exhausted_halt",
        },
    )
    g.add_edge("qa_exhausted_halt", "extract_lesson")
    g.add_conditional_edges(
        "security_diff_review",
        lambda s: "human_merge_gate" if s.get("workflow_state") == WorkforceState.APPROVAL_PENDING.value else "extract_lesson",
        {"human_merge_gate": "human_merge_gate", "extract_lesson": "extract_lesson"},
    )
    g.add_edge("human_merge_gate", "extract_lesson")
    g.add_edge("extract_lesson", END)
    return g.compile(checkpointer=checkpointer)
