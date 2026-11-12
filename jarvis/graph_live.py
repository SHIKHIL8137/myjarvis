"""Unified live knowledge graph (P10.15 / P10.18). Real entities and real relationships only.
Every node has a stable id "<type>:<key>", so clients merge snapshots and bus deltas instead of rebuilding."""
from datetime import datetime, timezone
from pathlib import Path
import re
import time

from sqlalchemy import select

from .db.models import AgentStepRow, AssistantTaskRow, LLMUsageLogRow, SkillMetricRow

CONVERSATION = "conversation:current"

# Never serialize secrets, API keys, tokens, or raw private paths into live graph nodes/events.
_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"AIza[0-9A-Za-z\-_]{16,}|"
    r"ghp_[0-9A-Za-z]{20,}|"
    r"hf_[0-9A-Za-z]{16,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{12,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|passwd|token|private[_-]?key|database[_-]?url)\s*([:=])\s*([^\s,;\"']{4,})"
)

ASSISTANT_OS_STATES = {
    "Idle",
    "Listening",
    "Speech detected",
    "Transcribing",
    "Understanding",
    "Thinking",
    "Planning",
    "Delegating",
    "Executing",
    "Waiting for approval",
    "Speaking",
    "Completed",
    "Error",
}


def sanitize_graph_text(text: str | None) -> str:
    """Redact potential credentials/tokens before placing text on graph metadata or live events."""
    if not text:
        return ""
    cleaned = _SECRET_RE.sub("[REDACTED]", str(text))
    cleaned = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", cleaned)
    return cleaned


def _iso(epoch: float | int | None) -> str | None:
    if epoch in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node(type_: str, key, label, **fields) -> dict:
    clean_fields: dict = {}
    for k, v in fields.items():
        if v in (None, ""):
            continue
        if isinstance(v, str):
            clean_fields[k] = sanitize_graph_text(v)
        else:
            clean_fields[k] = v
    if "created_at" in clean_fields and "created_iso" not in clean_fields:
        iso = _iso(clean_fields["created_at"])
        if iso:
            clean_fields["created_iso"] = iso
    if "updated_at" in clean_fields and "updated_iso" not in clean_fields:
        iso = _iso(clean_fields["updated_at"])
        if iso:
            clean_fields["updated_iso"] = iso
    return {"id": f"{type_}:{key}", "type": type_, "label": sanitize_graph_text(str(label or key))[:120], **clean_fields}


def _link(source: str, target: str, relation: str, **meta) -> dict:
    out = {"source": source, "target": target, "relation": relation}
    for k, v in meta.items():
        if v not in (None, ""):
            out[k] = v
    return out


def memory_node(r: dict, store: str = "five_way") -> dict:
    """Works for both the 5-way personal memory dict (content) and the legacy MemoryRecord dict (text)."""
    text = sanitize_graph_text(r.get("content") or r.get("text") or "")
    return _node("memory", r["id"], r.get("title") or text[:60], description=text[:400], source=r.get("source"),
                 category=r.get("category"), project_id=r.get("project_id"), store=store,
                 confidence=r.get("confidence"), version=1,
                 created_at=r.get("created_at"), updated_at=r.get("updated_at"))


def memory_links(r: dict) -> list[dict]:
    out = []
    if r.get("project_id"):
        out.append(_link(f"memory:{r['id']}", f"project:{r['project_id']}", "belongs_to"))
    if r.get("supersedes_id"):
        out.append(_link(f"memory:{r['id']}", f"memory:{r['supersedes_id']}", "supersedes"))
    return out


def project_node(p: dict) -> dict:
    return _node("project", p["id"], p["name"], source=p.get("root_path"), graphify_status=p.get("graphify_status"),
                 created_at=p.get("created_at"), updated_at=p.get("graphify_last_built_at"))


def workflow_node(name: str, *, description: str | None = None) -> dict:
    return _node("workflow", name, name, description=description or f"LangGraph / Jarvis workflow ({name})", source="orchestrator")


def tool_node(name: str, *, source: str = "runtime") -> dict:
    return _node("tool", name, name, source=source, description=f"Executed capability tool: {name}")


def run_delta(r: dict) -> tuple[list[dict], list[dict]]:
    """Task node for an agent run + the agent that executes it + the project/workflow it runs on."""
    tid = f"task:{r['id']}"
    wf = r.get("workflow_name") or "assistant"
    nodes = [
        _node("task", r["id"], r.get("input_summary") or wf, description=r.get("output_summary"),
              source=wf, state=r.get("state"), step=r.get("current_step"), project_id=r.get("project_id"),
              created_at=r.get("started_at"), updated_at=r.get("ended_at") or r.get("last_activity")),
        workflow_node(wf),
    ]
    links = [_link(tid, f"workflow:{wf}", "part_of")]
    if r.get("agent_role"):
        nodes.append(_node("agent", r["agent_role"], r["agent_role"]))
        links.append(_link(f"agent:{r['agent_role']}", tid, "executes"))
    if r.get("project_id"):
        links.append(_link(tid, f"project:{r['project_id']}", "runs_on"))
    return nodes, links


def conversation_node(a) -> dict:
    items = a.history.items
    return _node("conversation", "current", "Current conversation", source="history", messages=len(items),
                 created_at=items[0].get("t") if items else None, updated_at=items[-1].get("t") if items else None)


def _infer_department(event: str) -> str:
    if event.startswith("agent.") or event.startswith("project.") or event.startswith("workforce.") or event.startswith("workflow."):
        return "dev"
    if event.startswith("memory.") or event.startswith("note.") or event.startswith("conversation."):
        return "memory"
    if event.startswith("voice.") or event.startswith("assistant."):
        return "assistant"
    if event.startswith("skill."):
        return "skills"
    if event.startswith("tool."):
        return "tools"
    return "system"


def emit(bus, event: str, *, nodes=(), links=(), removed=(), highlight=(), resync: bool = False,
         source_department: str | None = None, entity_id: str | None = None, payload: dict | None = None, **extra) -> None:
    """Publish a canonical versioned `graph_event` envelope on EventBus while keeping backward-compatible fields."""
    node_list = list(nodes)
    link_list = list(links)
    removed_list = list(removed)
    hl_list = list(highlight)
    eid = entity_id or (node_list[0]["id"] if node_list else (removed_list[0] if removed_list else None))
    bus.publish({
        "type": "graph_event",
        "event": event,
        "event_type": event,
        "schema_version": 1,
        "occurred_at": _utc_now_iso(),
        "source_department": source_department or _infer_department(event),
        "entity_id": eid,
        "nodes": node_list,
        "links": link_list,
        "removed": removed_list,
        "highlight": hl_list,
        "resync": resync,
        "payload": payload or {},
        "ts": round(time.time(), 3),
        **extra,
    })


def publish_os_event(bus, event_type: str, *, source_department: str = "system", entity_id: str | None = None,
                     payload: dict | None = None, **extra) -> None:
    """Publish a canonical versioned system/assistant/voice telemetry event."""
    bus.publish({
        "type": extra.pop("legacy_type", event_type),
        "event": event_type,
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": _utc_now_iso(),
        "source_department": source_department,
        "entity_id": entity_id,
        "payload": payload or {},
        "ts": round(time.time(), 3),
        **extra,
    })


def emit_assistant_state(bus, state: str, *, source: str = "text", detail: str = "", entity_id: str | None = None, **extra) -> None:
    """Publish real-time holographic assistant state transitions (`Idle`, `Listening`, `Thinking`, etc.)."""
    publish_os_event(
        bus,
        "assistant.state.changed",
        legacy_type="assistant_state",
        source_department="assistant",
        entity_id=entity_id or CONVERSATION,
        state=state,
        source=source,
        detail=sanitize_graph_text(detail)[:200],
        payload={"state": state, "source": source, "detail": sanitize_graph_text(detail)[:200], **extra},
        **extra,
    )


def conversation_used(a, hits, ctx: dict) -> None:
    """A chat/voice turn retrieved these notes, memories and knowledge sources: link + highlight them live."""
    ids = ([f"note:{n.uid}" for n, _ in hits] + [f"memory:{i}" for i in ctx.get("memory_ids", [])]
           + [f"knowledge:{k['source_id']}" for k in ctx.get("knowledge_sources", [])])
    if not ids:
        return
    a.conversation_refs.update(ids)  # bounded by the number of notes/memories/sources
    emit(a.bus, "conversation.updated", source_department="assistant", entity_id=CONVERSATION,
         nodes=[conversation_node(a)],
         links=[_link(CONVERSATION, i, "references") for i in ids], highlight=[CONVERSATION, *ids])


def _build_tool_delta(a, tool_name: str, *, target_ids: list[str] | None = None) -> tuple[str, dict, list[dict], list[dict]]:
    clean_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tool_name).strip("_") or "tool"
    tnode = tool_node(clean_name, source="assistant")
    nodes = [tnode]
    links = []
    if a.history.items or a.conversation_refs:
        nodes.append(conversation_node(a))
        links.append(_link(CONVERSATION, tnode["id"], "uses_tool"))
    for tid in target_ids or []:
        links.append(_link(tnode["id"], tid, "touches"))
    return clean_name, tnode, nodes, links


def tool_invoked(a, tool_name: str, *, query: str = "", target_ids: list[str] | None = None) -> None:
    """Broadcast start of a generic tool invocation (`tool.invoked`), distinct from installed Skills."""
    clean_name, tnode, nodes, links = _build_tool_delta(a, tool_name, target_ids=target_ids)
    emit(
        a.bus,
        "tool.invoked",
        source_department="tools",
        entity_id=tnode["id"],
        nodes=nodes,
        links=links,
        highlight=[tnode["id"], CONVERSATION, *(target_ids or [])],
        tool=clean_name,
        status="running",
        query=sanitize_graph_text(query)[:140],
    )


def tool_used(
    a,
    tool_name: str,
    *,
    query: str = "",
    target_ids: list[str] | None = None,
    ok: bool = True,
    duration_ms: float | None = None,
) -> None:
    """Record a completed generic tool invocation (`tool.executed`) and broadcast its graph delta."""
    clean_name, tnode, nodes, links = _build_tool_delta(a, tool_name, target_ids=target_ids)
    used = getattr(a, "used_tools", None)
    if isinstance(used, set):
        used.add(clean_name)
    extra: dict = {
        "tool": clean_name,
        "ok": bool(ok),
        "query": sanitize_graph_text(query)[:140],
    }
    if duration_ms is not None:
        extra["duration_ms"] = round(float(duration_ms), 2)
    emit(
        a.bus,
        "tool.executed",
        source_department="tools",
        entity_id=tnode["id"],
        nodes=nodes,
        links=links,
        highlight=[tnode["id"], CONVERSATION, *(target_ids or [])],
        **extra,
    )


def task_delta(task: dict) -> tuple[list[dict], list[dict]]:
    """Build real knowledge graph nodes/links for an AssistantTaskRow."""
    tid = str(task.get("task_id") or "")
    if not tid:
        return [], []
    label = sanitize_graph_text(str(task.get("user_request") or task.get("intent") or tid))[:60]
    tnode = _node(
        "task",
        tid,
        label,
        status=task.get("status"),
        intent=task.get("intent"),
        source=task.get("source"),
        step=task.get("current_step"),
        created_at=task.get("created_at"),
        updated_at=task.get("updated_at"),
    )
    nodes = [tnode]
    links: list[dict] = [_link(CONVERSATION, tnode["id"], "triggered")]
    for ag in task.get("selected_agents") or []:
        if ag:
            anode = _node("agent", str(ag), str(ag))
            nodes.append(anode)
            links.append(_link(tnode["id"], anode["id"], "assigned_to"))
    for tool_name in task.get("tools_used") or []:
        if tool_name:
            t_node = tool_node(str(tool_name), source="assistant")
            nodes.append(t_node)
            links.append(_link(tnode["id"], t_node["id"], "uses_tool"))
    for fitem in task.get("generated_files") or []:
        fpath = fitem.get("path") if isinstance(fitem, dict) else str(fitem)
        fname = fitem.get("name") if isinstance(fitem, dict) else (Path(fpath).name if fpath else "")
        if fpath and fname:
            dnode = _node("document", f"fs_{abs(hash(fpath)) % 10**9}", fname, source=fpath)
            nodes.append(dnode)
            links.append(_link(tnode["id"], dnode["id"], "produced"))
    return nodes, links


def filesystem_activity(
    a,
    *,
    task_id: str | None = None,
    folder_path: str = "",
    files: list[dict] | None = None,
    operation: str = "search_files",
) -> None:
    """Emit real filesystem discovery nodes/highlights ONLY after verified execution."""
    nodes: list[dict] = []
    links: list[dict] = []
    hl: list[str] = [CONVERSATION]
    if task_id:
        tnode = _node("task", task_id, task_id[:16], status="running")
        nodes.append(tnode)
        hl.append(tnode["id"])
    f_tool = tool_node("filesystem_access", source="filesystem")
    nodes.append(f_tool)
    hl.append(f_tool["id"])
    for item in (files or [])[:12]:
        fpath = str(item.get("path") or "")
        fname = str(item.get("name") or "")
        if not fpath or not fname:
            continue
        fid = f"fs_{abs(hash(fpath)) % 10**9}"
        dnode = _node("document", fid, fname, source=fpath, bytes=item.get("size_bytes"), updated_at=item.get("modified_at"))
        nodes.append(dnode)
        hl.append(dnode["id"])
        if task_id:
            links.append(_link(f"task:{task_id}", dnode["id"], f"fs_{operation}"))
    emit(
        a.bus,
        "filesystem.executed",
        source_department="assistant",
        entity_id=f_tool["id"],
        nodes=nodes,
        links=links,
        highlight=hl,
        operation=operation,
        folder=sanitize_graph_text(folder_path)[:160],
        count=len(files or []),
    )


def skill_executed(
    bus,
    *,
    skill_id: str,
    skill_name: str = "",
    agent_role: str = "assistant_coordinator",
    run_id: str | None = None,
    project_id: str | None = None,
    ok: bool = True,
    task_kind: str = "",
) -> None:
    """Emit genuine `skill.executed` event ONLY when an installed SkillRegistry skill runs."""
    sid = f"skill:{skill_id}"
    aid = f"agent:{agent_role}"
    snode = _node(
        "skill",
        skill_id,
        skill_name or skill_id,
        category=task_kind,
        role=agent_role,
        status="completed" if ok else "failed",
    )
    anode = _node("agent", agent_role, agent_role)
    links = [_link(aid, sid, "uses_skill")]
    if run_id:
        links.append(_link(f"task:{run_id}", sid, "used_skill"))
    if project_id:
        links.append(_link(sid, f"project:{project_id}", "applies_to"))
    emit(
        bus,
        "skill.executed",
        source_department="skills",
        entity_id=sid,
        nodes=[snode, anode],
        links=links,
        highlight=[sid, aid],
        skill_id=skill_id,
        agent_role=agent_role,
        run_id=run_id,
        ok=bool(ok),
        task_kind=task_kind,
    )


def workflow_event(
    bus,
    event_name: str,
    *,
    run_id: str,
    workflow_name: str,
    agent_role: str = "assistant_coordinator",
    department: str = "assistant",
    project_id: str | None = None,
    state: str = "running",
    step: str | None = None,
    detail: str = "",
) -> None:
    """Emit structured `workflow.started` / `workflow.completed` / `workflow.failed` lifecycle event."""
    wf_node = workflow_node(workflow_name)
    anode = _node("agent", agent_role, agent_role)
    links = [_link(f"agent:{agent_role}", wf_node["id"], "executes_workflow")]
    if project_id:
        links.append(_link(wf_node["id"], f"project:{project_id}", "targets"))
    active = state in ("running", "awaiting_approval", "cancelling")
    emit(
        bus,
        event_name,
        source_department=department,
        entity_id=wf_node["id"],
        nodes=[wf_node, anode],
        links=links,
        highlight=[wf_node["id"], f"task:{run_id}"] if active else [],
        run_id=run_id,
        workflow=workflow_name,
        state=state,
        step=step,
        detail=sanitize_graph_text(detail)[:240],
    )


def conversation_cleared(a) -> None:
    a.conversation_refs.clear()
    used = getattr(a, "used_tools", None)
    if isinstance(used, set):
        used.clear()
    emit(a.bus, "conversation.deleted", source_department="assistant", entity_id=CONVERSATION, removed=[CONVERSATION])


def build_graph(a, types: set[str] | None = None, limit: int = 300) -> dict:
    """Snapshot. `limit` caps each entity type to its most recent items; `truncated` reports what was left out."""
    o = a.orchestrator
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    truncated: dict[str, int] = {}

    def want(t: str) -> bool:
        return not types or t in types

    def take(t: str, items: list) -> list:
        if len(items) > limit:
            truncated[t] = len(items)
        return items[:limit]

    def add(n: dict) -> None:
        if want(n["type"]):
            nodes.setdefault(n["id"], n)

    if want("note"):
        notes = a.store.all()
        g = a.store.graph()
        keep = {n.id for n in take("note", sorted(notes, key=lambda n: -n.mtime_ns))}
        nid = {n.id: f"note:{n.uid}" for n in notes if n.id in keep}
        for n in notes:
            if n.id in keep:
                add(_node("note", n.uid, n.title, description=n.excerpt[:400], source=n.path, category=n.category,
                          updated_at=round(n.mtime_ns / 1e9, 3)))
        links += [_link(nid[e["source"]], nid[e["target"]], "linked") for e in g["links"] if e["source"] in nid and e["target"] in nid]

    if want("memory"):
        by_path = {n.path: n.uid for n in a.store.all()}
        for rec in take("memory", a.memory_store.list_all(include_pending=False)):
            d = rec.to_dict()
            add(memory_node(d, store="legacy"))
            if d.get("note_path") in by_path:
                links.append(_link(f"memory:{d['id']}", f"note:{by_path[d['note_path']]}", "saved_as"))
        for d in take("memory", o.memory.list_personal_memories()):
            add(memory_node(d))
            links += memory_links(d)

    if want("experience"):
        for e in take("experience", o.memory.list_experience_lessons()):
            add(_node("experience", e["id"], e["trigger_context"], description=e["lesson_after"][:400], source=e["scope"],
                      category=e["category"], project_id=e.get("project_id"), created_at=e.get("created_at")))
            if e.get("project_id"):
                links.append(_link(f"experience:{e['id']}", f"project:{e['project_id']}", "learned_in"))

    if want("project"):
        for p in o.projects.list_projects():
            add(project_node(p))

    if want("skill") or want("agent"):
        skills_list = o.skills.list_skills() if want("skill") else []
        for s in take("skill", skills_list):
            add(_node("skill", s["skill_id"], s["name"], description=s["description"][:400], source=s.get("source_repo"),
                      category=s["category"], enabled=s["enabled"], created_at=s.get("installed_at"), updated_at=s.get("updated_at")))
        for asn in o.skills.list_assignments():
            add(_node("agent", asn["agent_role"], asn["agent_role"]))
            links.append(_link(f"skill:{asn['skill_id']}", f"agent:{asn['agent_role']}", "supports"))

    if want("task") or want("workflow") or want("tool") or want("agent"):
        runs = take("task", o.list_runs(limit=limit + 1))
        for r in runs:
            ns, ls = run_delta(r)
            for n in ns:
                add(n)
            links += ls
        run_ids = [r["id"] for r in runs]
        if run_ids:
            with o.db.sync_session() as s:
                for m in s.scalars(select(SkillMetricRow).where(SkillMetricRow.run_id.in_(run_ids))).all():
                    links.append(_link(f"task:{m.run_id}", f"skill:{m.skill_id}", "used_skill"))
                for u in s.scalars(select(LLMUsageLogRow).where(LLMUsageLogRow.run_id.in_(run_ids))).all():
                    links.append(_link(f"task:{u.run_id}", f"model:{u.provider}:{u.model}", "used_model"))
                for st in s.scalars(select(AgentStepRow).where(AgentStepRow.run_id.in_(run_ids))).all():
                    art = (st.output_payload or {}).get("artifact") or {}
                    if isinstance(art, dict) and art.get("name"):
                        links.append(_link(f"task:{st.run_id}", f"document:{art['name']}", "produced"))
                    tname = (st.output_payload or {}).get("tool")
                    if isinstance(tname, str) and tname:
                        add(tool_node(tname, source="agent"))
                        links.append(_link(f"task:{st.run_id}", f"tool:{tname}", "uses_tool"))
        with o.db.sync_session() as s:
            atasks = s.scalars(select(AssistantTaskRow).order_by(AssistantTaskRow.created_at.desc()).limit(limit)).all()
            for t in atasks:
                td = {
                    "task_id": t.task_id,
                    "user_request": t.user_request,
                    "source": t.source,
                    "intent": t.intent,
                    "status": t.status,
                    "selected_agents": list(t.selected_agents or []),
                    "tools_used": list(t.tools_used or []),
                    "generated_files": list(t.generated_files or []),
                    "current_step": t.current_step,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                tns, tls = task_delta(td)
                for n in tns:
                    add(n)
                links += tls

    if want("tool"):
        for tname in sorted(getattr(a, "used_tools", set()) or set()):
            add(tool_node(tname, source="assistant"))
            if a.history.items or a.conversation_refs:
                links.append(_link(CONVERSATION, f"tool:{tname}", "uses_tool"))

    if want("document"):
        for d in take("document", o.documents.list_generated()):
            add(_node("document", d["name"], d["name"], source=d["path"], bytes=d["bytes"], updated_at=d.get("mtime")))

    if want("model") and o.gateway:
        for c in o.gateway.list_capabilities():
            add(_node("model", f"{c['provider']}:{c['model_id']}", c["model_id"], source=c["provider"], local=c["local"],
                      description=f"{c['provider']} · ctx {c['max_ctx']} · {c['pricing_tier']}"))

    if want("knowledge"):
        for k in take("knowledge", a.knowledge.list_sources()):
            add(_node("knowledge", k["source_id"], k.get("name") or k["source_id"], source=k.get("location"),
                      category=k.get("topic"), description=k.get("kind")))

    if want("conversation"):
        if a.history.items:
            add(conversation_node(a))
            links += [_link(CONVERSATION, i, "references") for i in a.conversation_refs]
            for tname in sorted(getattr(a, "used_tools", set()) or set()):
                links.append(_link(CONVERSATION, f"tool:{tname}", "uses_tool"))
        for sess in o.memory.list_sessions():
            add(_node("conversation", sess["id"], sess["title"], source=sess["department"], project_id=sess.get("project_id")))
            if sess.get("project_id"):
                links.append(_link(f"conversation:{sess['id']}", f"project:{sess['project_id']}", "belongs_to"))

    seen: set[tuple] = set()
    out_links = []
    for e in links:  # only relationships whose both ends exist; no duplicates
        key = (e["source"], e["relation"], e["target"])
        if e["source"] in nodes and e["target"] in nodes and key not in seen:
            seen.add(key)
            out_links.append(e)
    counts: dict[str, int] = {}
    for n in nodes.values():
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    return {
        "schema_version": 1,
        "seq": getattr(a.bus, "latest_seq", 0),
        "nodes": list(nodes.values()),
        "links": out_links,
        "counts": counts,
        "truncated": truncated,
        "ts": round(time.time(), 3),
    }
