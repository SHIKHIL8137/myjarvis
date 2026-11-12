"""AgentOrchestrator: wires DB, DecisionEngine, ModelGateway, 5-Way Memory, Assistant/Dev LangGraphs & DR."""
from pathlib import Path
import shutil
import threading
import time
import uuid

from langgraph.types import Command

from .. import graph_live
from ..config import DATA, SECRETS_PATH, Settings
from ..core.event_bus import EventBus
from ..db.engine import DatabaseManager
from ..db.models import AgentRunRow, AgentStepRow, LLMUsageLogRow, WorktreeRow
from ..decision.calibration import DecisionCalibrationService
from ..decision.router import DecisionEngine
from ..departments.assistant.documents import DocumentService
from ..departments.assistant.productivity import EmailDraftService, LocalCalendarService
from ..departments.assistant.reminders import ReminderService
from ..departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from ..filesystem_access import FilesystemAccessManager
from ..gateway.gateway import ModelGateway
from ..gateway.sandbox_proxy import ScopedGatewayTokenManager
from ..integrations import git_worktree, graphify_adapter
from ..memory.five_way import FiveWayMemoryService
from ..recovery.backup import DisasterRecoveryManager
from ..skills.registry import SkillRegistryService
from ..skills.router import AgentCapabilityRouter
from .approvals import ApprovalService
from .assistant_graph import build_assistant_graph
from .checkpointer import DBBackedCheckpointSaver
from .research import DeepResearchEngine, run_deep_research_background_task
from .task_runner import BackgroundTaskRunner
from .tasks import AssistantTaskService
from .workforce_graph import build_workforce_graph


class AgentOrchestrator:
    """Unified coordinator for v2 AI Operating System workflows without polluting voice turn loops."""

    def __init__(
        self,
        settings: Settings,
        *,
        db: DatabaseManager | None = None,
        bus: EventBus | None = None,
        brain=None,
        data_dir: Path = DATA,
        coder_runner=None,
    ):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.bus = bus or EventBus()
        self.db = db or DatabaseManager(settings.database.url, data_dir=self.data_dir, echo=settings.database.echo)
        self.db.init_schema_sync()

        self.memory = FiveWayMemoryService(self.db, settings=settings)
        self.approvals = ApprovalService(self.db)
        self.fs_access = FilesystemAccessManager(
            self.db,
            settings=settings,
            workspace_baseline_roots=[settings.notes_dir],
        )
        self.tasks = AssistantTaskService(self.db, event_publisher=self.bus.publish)
        self.task_runner = BackgroundTaskRunner(self.tasks)
        self.projects = ProjectRegistryService(self.db)
        self.workforce = DevWorkforceService(self.db, self.projects)
        self.documents = DocumentService(self.data_dir / "outputs" / "documents")
        self.reminders = ReminderService(self.db, bus=self.bus)
        self.email = EmailDraftService(self.data_dir / "outbox")
        self.calendar = LocalCalendarService(self.data_dir / "calendar")
        self.recovery = DisasterRecoveryManager(self.db, settings, data_dir=self.data_dir, secrets_path=SECRETS_PATH, approvals=self.approvals)
        skills_dir = Path(settings.skills.skills_dir)
        if not skills_dir.is_absolute():
            skills_dir = self.data_dir / "skills"
        self.skills = SkillRegistryService(
            self.db,
            skills_root=skills_dir,
            approvals=self.approvals,
            allowed_licenses={x.lower() for x in settings.skills.allowed_licenses},
            require_install_approval=settings.skills.require_install_approval,
            require_update_approval=settings.skills.require_update_approval,
            allow_untrusted_scripts=settings.skills.allow_untrusted_scripts,
        )
        self.capability_router = AgentCapabilityRouter(
            self.skills,
            default_max_context_chars=settings.skills.max_context_chars,
            default_max_skills=settings.skills.max_skills_per_run,
        )
        self.decision = DecisionEngine(settings, data_dir=self.data_dir)
        cal_dir = Path(settings.laya.calibration_dir)
        if not cal_dir.is_absolute():
            cal_dir = self.data_dir / "decision" / "calibration"
        self.decision_calibration = DecisionCalibrationService(calibration_dir=cal_dir, approvals=self.approvals)
        self.gateway = ModelGateway(brain, db=self.db, settings=settings) if brain is not None else None
        self.sandbox_tokens = ScopedGatewayTokenManager()

        self.checkpointer = DBBackedCheckpointSaver(self.db)
        self._coder_runner = coder_runner
        self._orch_lock = threading.Lock()
        self._active_runs: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancelled_reasons: dict[str, str] = {}
        self._run_progress: dict[str, tuple[str, float]] = {}  # run_id -> (last finished LangGraph node, time); this process only
        self._assistant_graph = build_assistant_graph(
            memory=self.memory,
            docs=self.documents,
            reminders=self.reminders,
            email_service=self.email,
            approvals=self.approvals,
            online_allowed_fn=lambda: bool(self.settings.internet.enabled and not self.settings.offline_enforced),
            capability_router=self.capability_router,
            checkpointer=self.checkpointer,
        )
        self.research_engine = DeepResearchEngine(
            data_dir=self.data_dir,
            search_service=None,
            knowledge_pipeline=None,
            document_service=self.documents,
            online_allowed_fn=lambda: bool(self.settings.internet.enabled and not self.settings.offline_enforced),
        )
        # Re-compile assistant_graph with isolated deep_research engine and register durable worker handler.
        self._assistant_graph = build_assistant_graph(
            memory=self.memory,
            docs=self.documents,
            reminders=self.reminders,
            email_service=self.email,
            approvals=self.approvals,
            online_allowed_fn=lambda: bool(self.settings.internet.enabled and not self.settings.offline_enforced),
            capability_router=self.capability_router,
            deep_research=self.research_engine,
            checkpointer=self.checkpointer,
        )
        self.task_runner.register_handler(
            "deep_research",
            lambda ctx, t: run_deep_research_background_task(ctx, t, engine=self.research_engine),
        )
        self._workforce_graph = build_workforce_graph(
            projects=self.projects,
            workforce=self.workforce,
            memory=self.memory,
            approvals=self.approvals,
            workforce_spec=settings.workforce,
            coder_runner=coder_runner,
            is_cancelled_fn=self.is_run_cancelled,
            capability_router=self.capability_router,
            token_manager=self.sandbox_tokens,
            gateway_port=settings.port,
            graphify_bin=settings.workforce.graphify_bin,
            opencode_bin=settings.workforce.opencode_bin,
            checkpointer=self.checkpointer,
        )

    def attach_research_deps(
        self,
        *,
        search_service=None,
        knowledge_pipeline=None,
        online_allowed_fn=None,
        synthesizer_fn=None,
    ) -> None:
        """Bind web search & knowledge index dependencies to DeepResearchEngine (never personal memory)."""
        if search_service is not None:
            self.research_engine.search_service = search_service
        if knowledge_pipeline is not None:
            self.research_engine.knowledge_pipeline = knowledge_pipeline
        if online_allowed_fn is not None:
            self.research_engine.online_allowed_fn = online_allowed_fn
        if synthesizer_fn is not None:
            self.research_engine.synthesizer_fn = synthesizer_fn

    def is_run_cancelled(self, run_id: str) -> tuple[bool, str]:
        with self._orch_lock:
            ev = self._cancel_events.get(run_id)
            if ev is not None and ev.is_set():
                return True, self._cancelled_reasons.get(run_id) or "Cancelled by owner"
        with self.db.sync_session() as s:
            row = s.get(AgentRunRow, run_id)
            if row is not None:
                payload = row.state_payload or {}
                if row.state in ("cancelled", "cancelling") or payload.get("cancelled"):
                    return True, str(payload.get("cancel_reason") or "Cancelled by owner")
        return False, ""

    def _create_run(self, workflow_name: str, agent_role: str, summary: str, project_id: str | None = None) -> tuple[str, str]:
        run_id = uuid.uuid4().hex[:12]
        thread_id = f"thr-{run_id}"
        with self.db.sync_session() as s:
            s.add(
                AgentRunRow(
                    id=run_id,
                    project_id=project_id,
                    workflow_name=workflow_name,
                    agent_role=agent_role,
                    thread_id=thread_id,
                    state="running",
                    input_summary=summary[:500],
                    state_payload={},
                )
            )
            s.commit()
        self._emit_run(run_id, "agent.started")
        graph_live.workflow_event(
            self.bus,
            "workflow.started",
            run_id=run_id,
            workflow_name=workflow_name,
            agent_role=agent_role,
            department="dev" if workflow_name == "workforce" else "assistant",
            project_id=project_id,
            state="running",
            detail=summary[:200],
        )
        return run_id, thread_id

    def _update_run(self, run_id: str, state: str, output_summary: str) -> None:
        with self.db.sync_session() as s:
            row = s.get(AgentRunRow, run_id)
            if row:
                row.state = state
                row.output_summary = output_summary[:1000]
                if state in ("completed", "failed", "rejected", "blocked_security", "blocked", "cancelled"):
                    row.ended_at = round(time.time(), 3)
                s.commit()

    def _emit_run(self, run_id: str, event: str, step: str | None = None) -> None:
        """Publish the run's real state to the live graph (task node, executing agent, project) and highlight them."""
        if step:
            self._run_progress[run_id] = (step, round(time.time(), 3))
        run = next(iter(self.list_runs(run_id=run_id)), None)
        if run is None:
            return
        nodes, links = graph_live.run_delta(run)
        active = run["state"] in ("running", "awaiting_approval", "cancelling")
        ids = [n["id"] for n in nodes] + ([f"project:{run['project_id']}"] if run.get("project_id") else [])
        graph_live.emit(self.bus, event, nodes=nodes, links=links, highlight=ids if active else [], run_id=run_id, state=run["state"], step=step)

    def _invoke(self, graph, inp, cfg: dict, run_id: str) -> dict:
        """graph.invoke() that also publishes one `agent.progress` event per finished LangGraph node (real progress only)."""
        res: dict = {}
        interrupts: list = []
        for mode, chunk in graph.stream(inp, cfg, stream_mode=["updates", "values"]):
            if mode == "values":
                res = chunk
                continue
            for node, payload in (chunk or {}).items():
                if node == "__interrupt__":
                    interrupts.extend(payload)
                else:
                    self._emit_run(run_id, "agent.progress", step=node)
        return {**res, "__interrupt__": interrupts} if interrupts else res

    def _log_step(self, run_id: str, idx: int, node: str, status: str, payload: dict | None = None) -> None:
        with self.db.sync_session() as s:
            s.add(AgentStepRow(run_id=run_id, step_index=idx, node_name=node, status=status, output_payload=payload or {}))
            s.commit()

    def start_assistant_workflow(self, *, goal: str, task_kind: str = "research", payload: dict | None = None) -> dict:
        run_id, thread_id = self._create_run("assistant", task_kind, goal)
        cfg = {"configurable": {"thread_id": thread_id}}
        res = self._invoke(
            self._assistant_graph,
            {"run_id": run_id, "goal": goal, "task_kind": task_kind, "payload": payload or {}},
            cfg,
            run_id,
        )
        self.checkpointer.persist_thread(thread_id)
        interrupted = "__interrupt__" in res
        state = "awaiting_approval" if interrupted else res.get("status", "completed")
        self._update_run(run_id, state, res.get("summary") or state)
        self._log_step(
            run_id,
            1,
            task_kind,
            state,
            {"artifact": res.get("artifact"), "approval_token": res.get("approval_token"), "selected_skills": res.get("selected_skills")},
        )
        for sk in res.get("selected_skills") or []:
            role = sk.get("agent_role") or ("document_producer" if task_kind == "document_export" else "assistant_coordinator")
            ok_flag = state not in ("failed", "rejected", "blocked")
            self.skills.record_invocation_metric(
                sk["skill_id"],
                agent_role=role,
                run_id=run_id,
                task_kind=f"assistant:{task_kind}",
                success=ok_flag,
                token_overhead=max(1, len(res.get("skill_prompt_block") or "") // 4),
            )
            graph_live.skill_executed(
                self.bus,
                skill_id=sk["skill_id"],
                skill_name=sk.get("name", ""),
                agent_role=role,
                run_id=run_id,
                ok=ok_flag,
                task_kind=f"assistant:{task_kind}",
            )
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": "assistant", "state": state})
        if state in ("completed", "failed", "blocked", "blocked_security", "rejected", "cancelled"):
            graph_live.workflow_event(
                self.bus,
                "workflow.completed" if state == "completed" else "workflow.failed",
                run_id=run_id,
                workflow_name="assistant",
                agent_role="assistant_coordinator",
                department="assistant",
                state=state,
                detail=res.get("summary") or state,
            )
        self._emit_run(run_id, "agent.completed" if state == "completed" else "agent.failed" if state in ("failed", "blocked", "blocked_security", "rejected", "cancelled") else "agent.progress")
        return {"run_id": run_id, "thread_id": thread_id, "state": state, "interrupted": interrupted, "result": self._safe_res(res)}

    def start_workforce_workflow(
        self,
        *,
        project_id: str,
        task_prompt: str,
        target_symbol: str = "",
        qa_command: list[str] | None = None,
        model: str | None = None,
    ) -> dict:
        run_id, thread_id = self._create_run("workforce", "coder_agent", task_prompt, project_id=project_id)
        with self._orch_lock:
            self._cancel_events.setdefault(run_id, threading.Event())
            self._active_runs.add(run_id)
        cfg = {"configurable": {"thread_id": thread_id}}
        init_state = {
            "run_id": run_id,
            "project_id": project_id,
            "task_prompt": task_prompt,
            "target_symbol": target_symbol,
            "max_retries": self.settings.workforce.max_repair_retries,
        }
        if qa_command:
            init_state["qa_command"] = qa_command
        if model:
            init_state["model"] = model
        try:
            res = self._invoke(self._workforce_graph, init_state, cfg, run_id)
        finally:
            with self._orch_lock:
                self._active_runs.discard(run_id)
        self.checkpointer.persist_thread(thread_id)
        cancelled, cancel_reason = self.is_run_cancelled(run_id)
        interrupted = "__interrupt__" in res and not cancelled
        if cancelled:
            state = "cancelled"
            self.approvals.cancel_for_run(run_id, decided_by="system_cancelled_run")
            summary = res.get("summary") or f"Cancelled cleanly: {cancel_reason}"
        else:
            state = "awaiting_approval" if interrupted else res.get("status", "completed")
            summary = res.get("summary") or state
        self._update_run(run_id, state, summary)
        self._log_step(
            run_id,
            1,
            "workforce_pipeline",
            state,
            {"branch": res.get("branch_name"), "approval_token": res.get("approval_token"), "qa_result": res.get("qa_result"), "cancelled": cancelled, "selected_skills": res.get("selected_skills")},
        )
        qa_pass = (res.get("qa_result") or {}).get("passed")
        for sk in res.get("selected_skills") or []:
            ok_flag = state not in ("failed", "blocked", "blocked_security", "cancelled")
            self.skills.record_invocation_metric(
                sk["skill_id"],
                agent_role="coder_agent",
                run_id=run_id,
                task_kind="workforce",
                success=ok_flag,
                qa_passed=qa_pass,
                token_overhead=max(1, len(res.get("skill_prompt_block") or "") // 4),
            )
            graph_live.skill_executed(
                self.bus,
                skill_id=sk["skill_id"],
                skill_name=sk.get("name", ""),
                agent_role="coder_agent",
                run_id=run_id,
                project_id=project_id,
                ok=ok_flag,
                task_kind="workforce",
            )
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": "workforce", "state": state})
        if state in ("completed", "failed", "blocked", "blocked_security", "rejected", "cancelled"):
            graph_live.workflow_event(
                self.bus,
                "workflow.completed" if state == "completed" else "workflow.failed",
                run_id=run_id,
                workflow_name="workforce",
                agent_role="coder_agent",
                department="dev",
                project_id=project_id,
                state=state,
                detail=summary,
            )
        self._emit_run(run_id, "agent.completed" if state == "completed" else "agent.failed" if state in ("failed", "blocked", "blocked_security", "rejected", "cancelled") else "agent.progress")
        return {"run_id": run_id, "thread_id": thread_id, "state": state, "interrupted": interrupted, "cancelled": cancelled, "result": self._safe_res(res)}

    def cancel_workforce_run(
        self,
        run_id: str,
        *,
        reason: str = "Cancelled by owner",
        actor: str = "owner",
        authorize_cleanup: bool = False,
    ) -> dict:
        with self.db.sync_session() as s:
            row = s.get(AgentRunRow, run_id)
            if not row:
                raise KeyError(run_id)
            if row.state in ("completed", "failed", "rejected", "blocked_security"):
                return {
                    "ok": False,
                    "run_id": run_id,
                    "state": row.state,
                    "detail": f"Run already finished with state={row.state}",
                }
            payload = dict(row.state_payload or {})
            payload.update({
                "cancelled": True,
                "cancel_reason": reason,
                "cancelled_by": actor,
                "cancelled_at": round(time.time(), 3),
                "authorize_cleanup": authorize_cleanup,
            })
            row.state_payload = payload
            row.state = "cancelling"
            row.output_summary = f"Cancelling: {reason}"[:1000]
            s.commit()

        with self._orch_lock:
            ev = self._cancel_events.setdefault(run_id, threading.Event())
            self._cancelled_reasons[run_id] = reason
            ev.set()

        killed = git_worktree.cancel_run_processes(run_id)
        cancelled_approvals = self.approvals.cancel_for_run(run_id, decided_by=actor)

        for _ in range(15):
            with self._orch_lock:
                if run_id not in self._active_runs:
                    break
            time.sleep(0.02)

        with self._orch_lock:
            still_active = run_id in self._active_runs

        worktrees_handled: list[dict] = []
        with self.db.sync_session() as s:
            wt_rows = s.query(WorktreeRow).filter(WorktreeRow.run_id == run_id).all()
            for wt_r in wt_rows:
                wt_path = Path(wt_r.worktree_path)
                if authorize_cleanup and not still_active:
                    try:
                        proj = self.projects.get(wt_r.project_id)
                        git_worktree.cleanup_worktree(
                            Path(proj["root_path"]),
                            wt_path,
                            wt_r.branch_name,
                            authorized_uncommitted=True,
                            expected_task_id=run_id,
                        )
                        wt_r.status = "cancelled_cleaned"
                    except Exception:
                        wt_r.status = "cancelled_preserved"
                else:
                    if wt_r.status not in ("merged", "cancelled_cleaned"):
                        wt_r.status = "cancelled_preserved"
                worktrees_handled.append({"worktree_path": wt_r.worktree_path, "status": wt_r.status, "preserved": wt_r.status == "cancelled_preserved"})
            if wt_rows:
                s.commit()

        final_state = "cancelling" if still_active else "cancelled"
        if not still_active:
            self._update_run(run_id, "cancelled", f"Cancelled: {reason}")
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": "workforce", "state": final_state})
        if final_state == "cancelled":
            graph_live.workflow_event(
                self.bus,
                "workflow.failed",
                run_id=run_id,
                workflow_name="workforce",
                agent_role="coder_agent",
                department="dev",
                state="cancelled",
                detail=reason,
            )
        self._emit_run(run_id, "agent.completed" if final_state == "completed" else "agent.failed" if final_state in ("failed", "blocked", "blocked_security", "rejected", "cancelled") else "agent.progress")
        return {
            "ok": True,
            "run_id": run_id,
            "state": final_state,
            "processes_terminated": killed,
            "approvals_cancelled": cancelled_approvals,
            "worktrees": worktrees_handled,
            "worktree_preserved": all(w["preserved"] for w in worktrees_handled) if worktrees_handled else True,
        }

    def resume_workflow_after_approval(self, *, token: str, approved: bool, decided_by: str = "owner") -> dict:
        req = self.approvals.get(token)
        req_run_id = req.get("run_id")
        if req_run_id:
            cancelled, cancel_reason = self.is_run_cancelled(req_run_id)
            if cancelled:
                self.approvals.cancel_for_run(req_run_id, decided_by="system_cancelled_run")
                raise ValueError(f"Run {req_run_id} is cancelled ({cancel_reason}) and cannot be approved or merged.")

        dec = self.approvals.decide(token, approved=approved, decided_by=decided_by)
        run_id = dec.get("run_id")
        if not run_id:
            return {"approval": dec, "resumed": False}
        with self.db.sync_session() as s:
            row = s.get(AgentRunRow, run_id)
            if not row:
                return {"approval": dec, "resumed": False}
            wf = row.workflow_name
            tid = row.thread_id
        with self._orch_lock:
            self._active_runs.add(run_id)
        cfg = {"configurable": {"thread_id": tid}}
        graph = self._workforce_graph if wf == "workforce" else self._assistant_graph
        try:
            res = self._invoke(graph, Command(resume={"approved": approved}), cfg, run_id)
        finally:
            with self._orch_lock:
                self._active_runs.discard(run_id)
        self.checkpointer.persist_thread(tid)
        final_state = res.get("status", "completed")
        self._update_run(run_id, final_state, res.get("summary") or final_state)
        self._log_step(run_id, 2, "human_resume", final_state, {"approved": approved})
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": wf, "state": final_state})
        if final_state in ("completed", "failed", "blocked", "blocked_security", "rejected", "cancelled"):
            graph_live.workflow_event(
                self.bus,
                "workflow.completed" if final_state == "completed" else "workflow.failed",
                run_id=run_id,
                workflow_name=wf,
                agent_role="coder_agent" if wf == "workforce" else "assistant_coordinator",
                department="dev" if wf == "workforce" else "assistant",
                state=final_state,
                detail=res.get("summary") or final_state,
            )
        self._emit_run(run_id, "agent.completed" if final_state == "completed" else "agent.failed" if final_state in ("failed", "blocked", "blocked_security", "rejected", "cancelled") else "agent.progress")
        return {"approval": dec, "resumed": True, "run_id": run_id, "state": final_state, "result": self._safe_res(res)}

    def list_runs(self, limit: int = 25, run_id: str | None = None) -> list[dict]:
        with self.db.sync_session() as s:
            q = s.query(AgentRunRow)
            if run_id:
                q = q.filter(AgentRunRow.id == run_id)
            rows = q.order_by(AgentRunRow.started_at.desc()).limit(limit).all()
            models = {u.run_id: u.model for u in s.query(LLMUsageLogRow).filter(LLMUsageLogRow.run_id.in_([r.id for r in rows])).all()}
            return [
                {
                    "id": r.id,
                    "workflow_name": r.workflow_name,
                    "agent_role": r.agent_role,
                    "project_id": r.project_id,
                    "thread_id": r.thread_id,
                    "state": r.state,
                    "input_summary": r.input_summary,
                    "output_summary": r.output_summary,
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                    "model": models.get(r.id),
                    "current_step": self._run_progress.get(r.id, (None, None))[0] if r.ended_at is None else None,
                    "last_activity": self._run_progress.get(r.id, (None, None))[1] or r.ended_at or r.started_at,
                }
                for r in rows
            ]

    @staticmethod
    def _safe_res(res: dict) -> dict:
        return {k: v for k, v in (res or {}).items() if k != "__interrupt__"}

    def cancel_active_runs(self, *, reason: str = "Cancelled by user", actor: str = "owner") -> list[dict]:
        """Cancel all currently running AgentOrchestrator workflows cleanly."""
        with self._orch_lock:
            active_ids = list(self._active_runs)
        out: list[dict] = []
        for rid in active_ids:
            try:
                out.append(self.cancel_workforce_run(rid, reason=reason, actor=actor))
            except KeyError:
                pass
        return out

    def _match_project(self, goal: str, project_hint: str | None = None) -> tuple[dict | None, list[dict]]:
        projs = self.projects.list_projects()
        if not projs:
            return None, []
        hint = (project_hint or "").strip().lower()
        g_low = (goal or "").lower()
        for p in projs:
            pname = str(p.get("name") or "").lower()
            proot = Path(str(p.get("root_path") or "")).name.lower()
            if hint and (hint == pname or hint in pname or hint == p["id"].lower()):
                return p, projs
            if pname and pname in g_low:
                return p, projs
            if proot and len(proot) >= 3 and proot in g_low:
                return p, projs
        if len(projs) == 1:
            return projs[0], projs
        return None, projs

    def _can_execute_coder(self, project_root: Path) -> tuple[bool, str]:
        """Check whether real code-editing execution (custom runner or OpenCode + sandbox) is available."""
        if self._coder_runner is not None:
            return True, "configured_runner"
        oc_bin = self.settings.workforce.opencode_bin
        has_oc = bool(shutil.which(f"{oc_bin}.cmd") or shutil.which(f"{oc_bin}.exe") or shutil.which(oc_bin))
        pol = self.workforce.build_execution_policy(project_root, workforce_spec=self.settings.workforce)
        if not has_oc:
            return False, f"OpenCode CLI `{oc_bin}` is not installed on PATH."
        if pol.sandbox_backend == "docker" and not pol.allow_host_fallback and not git_worktree.is_docker_available():
            return False, "Docker daemon is unavailable and host fallback is disabled (`sandbox_backend='docker'`)."
        return True, pol.sandbox_backend

    def run_autonomous_dev_pipeline(
        self,
        *,
        goal: str,
        project_hint: str | None = None,
        intent: str = "dev_coding",
        risk_tier: str = "WRITE",
        filesystem=None,
        qa_command: list[str] | None = None,
        cancel_check=None,
        timeout_s: float = 120.0,
    ) -> dict:
        """Execute the multi-agent dev lifecycle honestly: identify project -> inspect -> plan -> select skills -> execute -> verify -> report."""
        t_start = time.monotonic()
        proj, all_projs = self._match_project(goal, project_hint=project_hint)

        def _is_aborted() -> tuple[bool, str]:
            if cancel_check and cancel_check():
                return True, "Cancelled by user"
            if (time.monotonic() - t_start) > timeout_s:
                return True, f"Timed out after {int(timeout_s)}s"
            return False, ""

        if proj is None and len(all_projs) > 1:
            names = ", ".join(p["name"] for p in all_projs[:6])
            return {
                "ok": False,
                "status": "clarification_needed",
                "summary": f"Multiple projects are registered ({names}). Specify which project should be inspected or modified.",
                "projects": [p["name"] for p in all_projs],
            }

        # Fallback read-only workspace inspection when no formal project is registered yet
        if proj is None:
            run_id, _ = self._create_run("dev_autonomous", "architect_agent", goal)
            if risk_tier == "READ" and filesystem is not None:
                self._emit_run(run_id, "agent.progress", step="inspect_workspace")
                fs_info = filesystem.inspect_project()
                exp_hits = self.memory.search_experience(goal, scope="dev", k=3)
                sel = self.capability_router.select_skills_for_task(
                    task_prompt=goal,
                    intent=intent,
                    department="dev",
                    complexity="complex_reasoning",
                    agent_role="architect_agent",
                )
                for sk in sel.get("selected") or []:
                    self.skills.record_invocation_metric(
                        sk["skill_id"], agent_role="architect_agent", run_id=run_id, task_kind="dev:analysis", success=True,
                    )
                    graph_live.skill_executed(
                        self.bus, skill_id=sk["skill_id"], skill_name=sk.get("name", ""), agent_role="architect_agent",
                        run_id=run_id, ok=True, task_kind="dev:analysis",
                    )
                langs = ", ".join(f"{k}: {v}" for k, v in list((fs_info.get("languages") or {}).items())[:6]) or "none"
                manifests = ", ".join(fs_info.get("manifests") or []) or "none"
                plan = (
                    f"1. Identified workspace root `{fs_info.get('root')}` ({fs_info.get('total_files', 0)} files).\n"
                    f"2. Inspected manifests ({manifests}) and languages ({langs}).\n"
                    f"3. Agents selected: architect_agent ({len(sel.get('selected') or [])} skills, {len(exp_hits)} lessons)."
                )
                summary = (
                    f"Workspace inspection complete for `{fs_info.get('root')}`: {fs_info.get('total_files', 0)} files "
                    f"({langs}); manifests: {manifests}."
                )
                self._log_step(run_id, 1, "inspect_workspace", "completed", {"inspection": fs_info, "plan": plan})
                self._update_run(run_id, "completed", summary)
                graph_live.workflow_event(
                    self.bus, "workflow.completed", run_id=run_id, workflow_name="dev_autonomous",
                    agent_role="architect_agent", department="dev", state="completed", detail=summary,
                )
                self._emit_run(run_id, "agent.completed")
                return {
                    "ok": True, "run_id": run_id, "status": "completed", "project": None,
                    "inspection": fs_info, "plan": plan, "selected_skills": sel.get("selected") or [],
                    "agents": ["architect_agent"], "summary": summary,
                }
            msg = "No project is registered in Project Registry yet. Register the repository in Workforce / Projects before autonomous code edits can run."
            self._log_step(run_id, 1, "identify_project", "blocked", {"reason": msg})
            self._update_run(run_id, "blocked", msg)
            graph_live.workflow_event(
                self.bus, "workflow.failed", run_id=run_id, workflow_name="dev_autonomous",
                agent_role="architect_agent", department="dev", state="blocked", detail=msg,
            )
            self._emit_run(run_id, "agent.failed")
            return {"ok": False, "run_id": run_id, "status": "blocked", "summary": msg}

        proj_root = Path(proj["root_path"])
        can_edit, coder_reason = self._can_execute_coder(proj_root)

        # Full worktree coding path when write is intended AND sandboxed coder runtime is ready
        if risk_tier == "WRITE" and can_edit:
            wf_out: dict = {}
            wf_err: list[Exception] = []

            def _run_wf():
                try:
                    wf_out["res"] = self.start_workforce_workflow(
                        project_id=proj["id"],
                        task_prompt=goal,
                        qa_command=qa_command,
                    )
                except Exception as exc:
                    wf_err.append(exc)

            th = threading.Thread(target=_run_wf, daemon=True)
            th.start()
            while th.is_alive():
                aborted, reason = _is_aborted()
                if aborted:
                    self.cancel_active_runs(reason=reason, actor="assistant_timeout" if "Timed out" in reason else "owner")
                    th.join(timeout=2.0)
                    return {"ok": False, "status": "cancelled" if "Cancelled" in reason else "failed", "summary": f"Workforce execution stopped cleanly: {reason}."}
                th.join(timeout=0.1)

            if wf_err:
                return {"ok": False, "status": "failed", "summary": f"Workforce pipeline error: {wf_err[0]}"}
            wf_res = wf_out.get("res") or {}
            st = wf_res.get("state", "completed")
            rbody = wf_res.get("result") or {}
            qa = rbody.get("qa_result") or {}
            qa_note = f"QA tests {'passed' if qa.get('passed') else 'failed'} (rc={qa.get('returncode')})." if qa else ""
            tok = rbody.get("approval_token")
            gate_note = f" Approval token `{tok}` created for owner merge review." if tok else ""
            summary = f"[{proj['name']}] Workforce run {wf_res.get('run_id')} state={st}. {rbody.get('summary') or ''} {qa_note}{gate_note}".strip()
            return {
                "ok": st in ("completed", "awaiting_approval"),
                "run_id": wf_res.get("run_id"),
                "status": st,
                "project": proj,
                "plan": rbody.get("plan"),
                "qa_result": qa,
                "approval_token": tok,
                "selected_skills": rbody.get("selected_skills") or [],
                "agents": ["architect_agent", "coder_agent", "qa_agent", "security_reviewer"],
                "summary": summary,
            }

        # Read-only dev analysis/review/audit OR blocked write with real inspection & plan (never fabricated edits)
        run_id, _ = self._create_run("dev_autonomous", "architect_agent", goal, project_id=proj["id"])
        aborted, reason = _is_aborted()
        if aborted:
            self._update_run(run_id, "cancelled", reason)
            graph_live.workflow_event(
                self.bus, "workflow.failed", run_id=run_id, workflow_name="dev_autonomous",
                agent_role="architect_agent", department="dev", project_id=proj["id"], state="cancelled", detail=reason,
            )
            self._emit_run(run_id, "agent.failed")
            return {"ok": False, "run_id": run_id, "status": "cancelled", "summary": reason}

        self._emit_run(run_id, "agent.progress", step="inspect_context")
        g_stats: dict = {"ok": False, "node_count": 0, "edge_count": 0}
        if graphify_adapter.graph_artifact_path(proj_root).exists():
            gq = graphify_adapter.query_graph(proj_root, goal, bin_name=self.settings.workforce.graphify_bin)
            g_stats = {"ok": bool(gq.get("ok")), "output": (gq.get("output") or "")[:1200], "node_count": proj.get("graphify_node_count") or 0}
        from ..config import FilesystemSpec
        from ..filesystem import SafeFilesystem
        try:
            fs_summary = filesystem.inspect_project(str(proj_root)) if filesystem is not None else SafeFilesystem(FilesystemSpec(allowed_roots=[str(proj_root)]), default_base=proj_root).inspect_project(str(proj_root))
        except Exception:
            fs_summary = SafeFilesystem(FilesystemSpec(allowed_roots=[str(proj_root)]), default_base=proj_root).inspect_project(str(proj_root))
        proj_mem = self.memory.search_project_knowledge(project_id=proj["id"], query=goal, k=3)
        exp_mem = self.memory.search_experience(goal, scope="dev", project_id=proj["id"], k=3)

        self._emit_run(run_id, "agent.progress", step="architect_plan")
        sel = self.capability_router.select_skills_for_task(
            task_prompt=goal,
            intent=intent,
            department="dev",
            complexity="heavy_coding" if risk_tier == "WRITE" else "complex_reasoning",
            agent_role="architect_agent",
            project_id=proj["id"],
        )
        for sk in sel.get("selected") or []:
            self.skills.record_invocation_metric(
                sk["skill_id"], agent_role="architect_agent", run_id=run_id, task_kind=f"dev:{intent}", success=True,
            )
            graph_live.skill_executed(
                self.bus, skill_id=sk["skill_id"], skill_name=sk.get("name", ""), agent_role="architect_agent",
                run_id=run_id, project_id=proj["id"], ok=True, task_kind=f"dev:{intent}",
            )

        agents = ["architect_agent", "context_researcher"]
        if intent == "security_audit":
            agents.append("security_reviewer")
        if risk_tier == "WRITE":
            agents.extend(["coder_agent", "qa_agent", "security_reviewer"])

        langs = ", ".join(f"{k}: {v}" for k, v in list((fs_summary.get("languages") or {}).items())[:6]) or "unknown"
        manifests = ", ".join(fs_summary.get("manifests") or []) or "none"
        plan = (
            f"1. Project: {proj['name']} ({proj['root_path']})\n"
            f"2. Repository context: {fs_summary.get('total_files', 0)} files ({langs}); manifests: {manifests}; "
            f"Graphify hits: {bool(g_stats.get('output'))}, lessons: {len(exp_mem)}.\n"
            f"3. Agents assigned: {', '.join(agents)}.\n"
            f"4. Verification command: `{proj.get('test_cmd') or 'pytest -q'}`."
        )
        self._log_step(
            run_id, 1, "dev_analysis", "blocked" if risk_tier == "WRITE" else "completed",
            {"inspection": fs_summary, "plan": plan, "selected_skills": sel.get("selected") or []},
        )

        if risk_tier == "WRITE":
            summary = (
                f"Identified project `{proj['name']}` ({fs_summary.get('total_files', 0)} files, manifests: {manifests}) "
                f"and built execution plan for agents {', '.join(agents)}. "
                f"Autonomous code modification is blocked safely because {coder_reason}"
            )
            self._update_run(run_id, "blocked", summary)
            graph_live.workflow_event(
                self.bus, "workflow.failed", run_id=run_id, workflow_name="dev_autonomous",
                agent_role="architect_agent", department="dev", project_id=proj["id"], state="blocked", detail=summary,
            )
            self._emit_run(run_id, "agent.failed")
            return {
                "ok": False, "run_id": run_id, "status": "blocked", "project": proj,
                "inspection": fs_summary, "plan": plan, "selected_skills": sel.get("selected") or [],
                "agents": agents, "summary": summary,
            }

        summary = (
            f"[{proj['name']}] Multi-agent analysis complete ({', '.join(agents)}): "
            f"{fs_summary.get('total_files', 0)} files ({langs}), manifests: {manifests}, "
            f"{len(proj_mem)} project knowledge hit(s), {len(exp_mem)} experience lesson(s)."
        )
        self._update_run(run_id, "completed", summary)
        graph_live.workflow_event(
            self.bus, "workflow.completed", run_id=run_id, workflow_name="dev_autonomous",
            agent_role="architect_agent", department="dev", project_id=proj["id"], state="completed", detail=summary,
        )
        self._emit_run(run_id, "agent.completed")
        return {
            "ok": True, "run_id": run_id, "status": "completed", "project": proj,
            "inspection": fs_summary, "plan": plan, "project_knowledge": proj_mem,
            "experience_lessons": exp_mem, "graphify": g_stats,
            "selected_skills": sel.get("selected") or [], "agents": agents, "summary": summary,
        }
