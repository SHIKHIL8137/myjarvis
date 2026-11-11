"""AgentOrchestrator: wires DB, DecisionEngine, ModelGateway, 5-Way Memory, Assistant/Dev LangGraphs & DR."""
from pathlib import Path
import time
import uuid

from langgraph.types import Command

from ..config import DATA, SECRETS_PATH, Settings
from ..core.event_bus import EventBus
from ..db.engine import DatabaseManager
from ..db.models import AgentRunRow, AgentStepRow
from ..decision.router import DecisionEngine
from ..departments.assistant.documents import DocumentService
from ..departments.assistant.productivity import EmailDraftService, LocalCalendarService
from ..departments.assistant.reminders import ReminderService
from ..departments.dev.workforce import DevWorkforceService, ProjectRegistryService
from ..gateway.gateway import ModelGateway
from ..memory.five_way import FiveWayMemoryService
from ..recovery.backup import DisasterRecoveryManager
from .approvals import ApprovalService
from .assistant_graph import build_assistant_graph
from .checkpointer import DBBackedCheckpointSaver
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

        self.memory = FiveWayMemoryService(self.db)
        self.approvals = ApprovalService(self.db)
        self.projects = ProjectRegistryService(self.db)
        self.workforce = DevWorkforceService(self.db, self.projects)
        self.documents = DocumentService(self.data_dir / "outputs" / "documents")
        self.reminders = ReminderService(self.db, bus=self.bus)
        self.email = EmailDraftService(self.data_dir / "outbox")
        self.calendar = LocalCalendarService(self.data_dir / "calendar")
        self.recovery = DisasterRecoveryManager(self.db, settings, data_dir=self.data_dir, secrets_path=SECRETS_PATH)
        self.decision = DecisionEngine(settings)
        self.gateway = ModelGateway(brain, db=self.db) if brain is not None else None

        self.checkpointer = DBBackedCheckpointSaver(self.db)
        self._coder_runner = coder_runner
        self._assistant_graph = build_assistant_graph(
            memory=self.memory,
            docs=self.documents,
            reminders=self.reminders,
            email_service=self.email,
            approvals=self.approvals,
            online_allowed_fn=lambda: bool(self.settings.internet.enabled and not self.settings.offline_enforced),
            checkpointer=self.checkpointer,
        )
        self._workforce_graph = build_workforce_graph(
            projects=self.projects,
            workforce=self.workforce,
            memory=self.memory,
            approvals=self.approvals,
            coder_runner=coder_runner,
            graphify_bin=settings.workforce.graphify_bin,
            opencode_bin=settings.workforce.opencode_bin,
            checkpointer=self.checkpointer,
        )

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
        return run_id, thread_id

    def _update_run(self, run_id: str, state: str, output_summary: str) -> None:
        with self.db.sync_session() as s:
            row = s.get(AgentRunRow, run_id)
            if row:
                row.state = state
                row.output_summary = output_summary[:1000]
                if state in ("completed", "failed", "rejected", "blocked_security"):
                    row.ended_at = round(time.time(), 3)
                s.commit()

    def _log_step(self, run_id: str, idx: int, node: str, status: str, payload: dict | None = None) -> None:
        with self.db.sync_session() as s:
            s.add(AgentStepRow(run_id=run_id, step_index=idx, node_name=node, status=status, output_payload=payload or {}))
            s.commit()

    def start_assistant_workflow(self, *, goal: str, task_kind: str = "research", payload: dict | None = None) -> dict:
        run_id, thread_id = self._create_run("assistant", task_kind, goal)
        cfg = {"configurable": {"thread_id": thread_id}}
        res = self._assistant_graph.invoke(
            {"run_id": run_id, "goal": goal, "task_kind": task_kind, "payload": payload or {}},
            cfg,
        )
        self.checkpointer.persist_thread(thread_id)
        interrupted = "__interrupt__" in res
        state = "awaiting_approval" if interrupted else res.get("status", "completed")
        self._update_run(run_id, state, res.get("summary") or state)
        self._log_step(run_id, 1, task_kind, state, {"artifact": res.get("artifact"), "approval_token": res.get("approval_token")})
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": "assistant", "state": state})
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
        res = self._workforce_graph.invoke(init_state, cfg)
        self.checkpointer.persist_thread(thread_id)
        interrupted = "__interrupt__" in res
        state = "awaiting_approval" if interrupted else res.get("status", "completed")
        self._update_run(run_id, state, res.get("summary") or state)
        self._log_step(
            run_id,
            1,
            "workforce_pipeline",
            state,
            {"branch": res.get("branch_name"), "approval_token": res.get("approval_token"), "qa_result": res.get("qa_result")},
        )
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": "workforce", "state": state})
        return {"run_id": run_id, "thread_id": thread_id, "state": state, "interrupted": interrupted, "result": self._safe_res(res)}

    def resume_workflow_after_approval(self, *, token: str, approved: bool, decided_by: str = "owner") -> dict:
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
        cfg = {"configurable": {"thread_id": tid}}
        graph = self._workforce_graph if wf == "workforce" else self._assistant_graph
        res = graph.invoke(Command(resume={"approved": approved}), cfg)
        self.checkpointer.persist_thread(tid)
        final_state = res.get("status", "completed")
        self._update_run(run_id, final_state, res.get("summary") or final_state)
        self._log_step(run_id, 2, "human_resume", final_state, {"approved": approved})
        self.bus.publish({"type": "agent_run_update", "run_id": run_id, "workflow": wf, "state": final_state})
        return {"approval": dec, "resumed": True, "run_id": run_id, "state": final_state, "result": self._safe_res(res)}

    def list_runs(self, limit: int = 25) -> list[dict]:
        with self.db.sync_session() as s:
            rows = s.query(AgentRunRow).order_by(AgentRunRow.started_at.desc()).limit(limit).all()
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
                }
                for r in rows
            ]

    @staticmethod
    def _safe_res(res: dict) -> dict:
        return {k: v for k, v in (res or {}).items() if k != "__interrupt__"}
