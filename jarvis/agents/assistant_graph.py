"""LangGraph Assistant Department Workflow (Research, Document Export, Reminders, Email/Calendar with Approval)."""
from typing import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..departments.assistant.documents import DocumentService
from ..departments.assistant.productivity import EmailDraftService
from ..departments.assistant.reminders import ReminderService
from ..memory.five_way import FiveWayMemoryService
from .approvals import ApprovalService


class AssistantGraphState(TypedDict, total=False):
    run_id: str
    goal: str
    task_kind: str  # "document_export" | "reminder" | "email_draft_and_send" | "research"
    payload: dict
    context_hits: list[dict]
    selected_skills: list[dict]
    skill_prompt_block: str
    artifact: dict
    approval_token: str
    approved: bool
    status: str
    summary: str


def build_assistant_graph(
    *,
    memory: FiveWayMemoryService,
    docs: DocumentService,
    reminders: ReminderService,
    email_service: EmailDraftService,
    approvals: ApprovalService,
    online_allowed_fn,
    capability_router=None,
    deep_research=None,
    checkpointer=None,
):
    def gather_context(state: AssistantGraphState) -> dict:
        goal = state.get("goal", "")
        kind = state.get("task_kind", "research")
        payload = state.get("payload") or {}
        # Strict privacy isolation: never leak personal memory into web/deep research prompts.
        isolate_web = kind == "deep_research" or (kind == "research" and (bool(payload.get("deep_research")) or bool(online_allowed_fn())))
        personal = [] if isolate_web else memory.search_personal_memory(goal, k=3)
        exp = [] if isolate_web else memory.search_experience(goal, scope="assistant", k=2)
        sel_skills: list[dict] = []
        skill_block = ""
        if capability_router is not None:
            role = "document_producer" if kind == "document_export" else ("research_agent" if kind == "research" else "assistant_coordinator")
            sel = capability_router.select_skills_for_task(
                task_prompt=goal,
                intent=kind,
                department="assistant",
                complexity="standard",
                agent_role=role,
                runtime="jarvis_native",
            )
            sel_skills = list(sel.get("selected") or [])
            skill_block = str(sel.get("combined_prompt_block") or "")
        return {"context_hits": personal + exp, "selected_skills": sel_skills, "skill_prompt_block": skill_block, "status": "running"}

    def route_task(state: AssistantGraphState) -> str:
        kind = state.get("task_kind", "research")
        if kind == "document_export":
            return "export_doc"
        if kind == "email_draft_and_send":
            return "draft_email"
        if kind == "reminder":
            return "create_rem"
        return "summarize"

    def export_doc(state: AssistantGraphState) -> dict:
        p = state.get("payload") or {}
        skills_used = [s.get("skill_id") for s in (state.get("selected_skills") or []) if s.get("skill_id")]
        res = docs.generate(
            p.get("format", "pdf"),
            filename=p.get("filename", "jarvis-report"),
            title=p.get("title", state.get("goal", "Report")),
            payload=p,
        )
        note = f" [skills={','.join(skills_used)}]" if (skills_used and state.get("skill_prompt_block")) else ""
        return {"artifact": res, "status": "completed", "summary": f"Generated {res['name']} ({res['bytes']} bytes).{note}"}

    def create_rem(state: AssistantGraphState) -> dict:
        p = state.get("payload") or {}
        import time
        due = float(p.get("due_at") or (time.time() + 3600))
        rem = reminders.create(title=p.get("title") or state.get("goal", "Reminder"), due_at=due, notes=p.get("notes", ""))
        return {"artifact": rem, "status": "completed", "summary": f"Reminder scheduled: {rem['title']}"}

    def draft_email(state: AssistantGraphState) -> dict:
        p = state.get("payload") or {}
        draft = email_service.create_draft(
            to=p.get("to", "unknown@example.com"),
            subject=p.get("subject", "Jarvis Update"),
            body=p.get("body", state.get("goal", "")),
        )
        req = approvals.create_request(
            action_type="email_send",
            target_resource=draft["to"],
            preview_diff=f"Subject: {draft['subject']}\nTo: {draft['to']}\n\n{draft['body']}",
            args={"draft_id": draft["id"]},
            run_id=state.get("run_id"),
        )
        return {"artifact": draft, "approval_token": req["token"], "status": "awaiting_approval"}

    def approval_gate(state: AssistantGraphState) -> dict:
        decision = interrupt(
            {
                "approval_token": state.get("approval_token"),
                "action": "email_send",
                "target": (state.get("artifact") or {}).get("to"),
            }
        )
        approved = bool(decision.get("approved") if isinstance(decision, dict) else decision)
        if not approved:
            return {"approved": False, "status": "rejected", "summary": "Email dispatch rejected by user; kept as draft."}
        sent = email_service.dispatch_approved_draft(
            state["artifact"]["id"],
            online_allowed=bool(online_allowed_fn()),
        )
        return {"approved": True, "artifact": sent, "status": "completed", "summary": f"Email status: {sent['status']}."}

    def summarize(state: AssistantGraphState) -> dict:
        kind = state.get("task_kind", "research")
        payload = state.get("payload") or {}
        if deep_research is not None and (kind == "deep_research" or bool(payload.get("deep_research"))):
            import asyncio

            run_id = state.get("run_id") or "research-graph"
            fmt = payload.get("export_format", "md")
            coro = deep_research.run(
                task_id=str(run_id),
                question=str(state.get("goal") or ""),
                export_format=str(fmt) if payload.get("export_artifact", True) else None,
            )
            try:
                asyncio.get_running_loop()
                # If invoked inside an existing loop thread (rare), run on helper thread loop
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    res = pool.submit(lambda: asyncio.run(coro)).result()
            except RuntimeError:
                res = asyncio.run(coro)
            return {
                "status": "completed",
                "artifact": res,
                "summary": res.get("summary") or f"Deep research completed ({len(res.get('sources') or [])} sources).",
            }

        hits = state.get("context_hits") or []
        skills_used = [s.get("skill_id") for s in (state.get("selected_skills") or []) if s.get("skill_id")]
        note = f" Skills active: {', '.join(skills_used)}." if (skills_used and state.get("skill_prompt_block")) else ""
        return {"status": "completed", "summary": f"Gathered {len(hits)} relevant personal/experience items.{note}"}

    g = StateGraph(AssistantGraphState)
    g.add_node("gather_context", gather_context)
    g.add_node("export_doc", export_doc)
    g.add_node("create_rem", create_rem)
    g.add_node("draft_email", draft_email)
    g.add_node("approval_gate", approval_gate)
    g.add_node("summarize", summarize)

    g.add_edge(START, "gather_context")
    g.add_conditional_edges(
        "gather_context",
        route_task,
        {"export_doc": "export_doc", "create_rem": "create_rem", "draft_email": "draft_email", "summarize": "summarize"},
    )
    g.add_edge("export_doc", END)
    g.add_edge("create_rem", END)
    g.add_edge("draft_email", "approval_gate")
    g.add_edge("approval_gate", END)
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)

