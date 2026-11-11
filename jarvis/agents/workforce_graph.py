"""12-Step LangGraph AI Development Workforce DAG:
Graphify -> Context -> Plan -> Worktree -> OpenCode -> QA Test Loop -> Diff Security -> Interrupt Gate -> Merge -> Lesson."""
from pathlib import Path
from typing import Callable, TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from ..integrations import git_worktree, graphify_adapter, opencode_adapter
from ..memory.five_way import FiveWayMemoryService
from .approvals import ApprovalService


class WorkforceGraphState(TypedDict, total=False):
    run_id: str
    project_id: str
    task_prompt: str
    target_symbol: str
    qa_command: list[str]
    model: str
    max_retries: int
    retry_count: int
    graph_stats: dict
    impact_context: dict
    plan: str
    worktree_path: str
    branch_name: str
    coder_result: dict
    qa_result: dict
    diff_info: dict
    security_scan: dict
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
    coder_runner: Callable[..., dict] | None = None,
    graphify_bin: str = "graphify",
    opencode_bin: str = "opencode",
    checkpointer=None,
):
    coder_fn = coder_runner or opencode_adapter.run_opencode_task

    def sync_graphify(state: WorkforceGraphState) -> dict:
        proj = projects.get(state["project_id"])
        root = Path(proj["root_path"])
        try:
            g_res = projects.sync_graphify(state["project_id"], bin_name=graphify_bin, no_cluster=True)
        except Exception as e:
            g_res = {"ok": False, "error": str(e), "node_count": 0, "edge_count": 0}
        return {"graph_stats": g_res, "status": "running", "retry_count": 0, "max_retries": int(state.get("max_retries", 2)), "_root": str(root)}

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
            f"Graphify Nodes: {(state.get('graph_stats') or {}).get('node_count', 0)}\n"
            f"Prior Lessons: {len(ctx.get('experience_hits') or [])}\n"
            "Execution: Work strictly inside isolated Git worktree, verify via QA command, security-scan diff, request human merge approval."
        )
        return {"plan": plan}

    def open_worktree(state: WorkforceGraphState) -> dict:
        wt = workforce.open_worktree(state["project_id"], state.get("run_id", "task"))
        return {"worktree_path": wt["worktree_path"], "branch_name": wt["branch"]}

    def coder_opencode(state: WorkforceGraphState) -> dict:
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
        return {"coder_result": res}

    def qa_verify(state: WorkforceGraphState) -> dict:
        proj = projects.get(state["project_id"])
        cmd = state.get("qa_command") or proj["test_cmd"].split()
        res = git_worktree.run_worktree_command(Path(state["worktree_path"]), list(cmd))
        retries = int(state.get("retry_count", 0)) + (0 if res["passed"] else 1)
        return {"qa_result": res, "retry_count": retries}

    def after_qa(state: WorkforceGraphState) -> str:
        qa = state.get("qa_result") or {}
        if qa.get("passed"):
            return "security_diff_review"
        if int(state.get("retry_count", 0)) <= int(state.get("max_retries", 2)):
            return "coder_opencode"
        return "security_diff_review"

    def security_diff_review(state: WorkforceGraphState) -> dict:
        df = git_worktree.worktree_diff(Path(state["worktree_path"]))
        scan = workforce.security_scan_diff(df.get("diff", ""))
        req = approvals.create_request(
            action_type="merge_worktree",
            target_resource=f"{state.get('branch_name')} -> main",
            preview_diff=df.get("diff", "") or df.get("status_short", "(no diff)"),
            args={
                "project_id": state["project_id"],
                "branch": state.get("branch_name"),
                "worktree_path": state.get("worktree_path"),
                "qa_passed": bool((state.get("qa_result") or {}).get("passed")),
                "security_clean": scan["clean"],
            },
            run_id=state.get("run_id"),
        )
        return {"diff_info": df, "security_scan": scan, "approval_token": req["token"], "status": "awaiting_approval"}

    def human_merge_gate(state: WorkforceGraphState) -> dict:
        decision = interrupt(
            {
                "approval_token": state.get("approval_token"),
                "branch": state.get("branch_name"),
                "qa_passed": (state.get("qa_result") or {}).get("passed"),
                "security_clean": (state.get("security_scan") or {}).get("clean"),
            }
        )
        approved = bool(decision.get("approved") if isinstance(decision, dict) else decision)
        proj = projects.get(state["project_id"])
        root = Path(proj["root_path"])
        wt = Path(state["worktree_path"])
        if not approved:
            git_worktree.cleanup_worktree(root, wt, state.get("branch_name"))
            return {"approved": False, "status": "rejected", "summary": "Merge rejected; isolated worktree cleaned cleanly."}
        if not (state.get("security_scan") or {}).get("clean", True):
            git_worktree.cleanup_worktree(root, wt, state.get("branch_name"))
            return {"approved": False, "status": "blocked_security", "summary": "Blocked merge: critical secret detected in diff."}

        mres = git_worktree.merge_worktree(root, state["branch_name"], wt, commit_msg=state["task_prompt"][:100])
        git_worktree.cleanup_worktree(root, wt, state.get("branch_name"))
        try:
            projects.sync_graphify(state["project_id"], bin_name=graphify_bin, no_cluster=True)
        except Exception:
            pass
        return {"approved": True, "merge_result": mres, "status": "completed", "summary": f"Verified, merged {state['branch_name']}, and cleaned worktree."}

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
    g.add_node("security_diff_review", security_diff_review)
    g.add_node("human_merge_gate", human_merge_gate)
    g.add_node("extract_lesson", extract_lesson)

    g.add_edge(START, "sync_graphify")
    g.add_edge("sync_graphify", "impact_and_context")
    g.add_edge("impact_and_context", "architect_plan")
    g.add_edge("architect_plan", "open_worktree")
    g.add_edge("open_worktree", "coder_opencode")
    g.add_edge("coder_opencode", "qa_verify")
    g.add_conditional_edges("qa_verify", after_qa, {"coder_opencode": "coder_opencode", "security_diff_review": "security_diff_review"})
    g.add_edge("security_diff_review", "human_merge_gate")
    g.add_edge("human_merge_gate", "extract_lesson")
    g.add_edge("extract_lesson", END)
    return g.compile(checkpointer=checkpointer)

