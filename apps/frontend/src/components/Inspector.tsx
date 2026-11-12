import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { collapseProject, expandProject, graph, highlightNodes, neighbors, projectExpanded, useGraphVersion } from "../graphStore";
import { onEvent, recentEvents } from "../live";
import { fmtTime, type Inspectable, type ShellProps } from "../shell";
import { styleOf } from "./GalaxyCanvas";

const show = (v: unknown): string => (v === null || v === undefined || v === "" ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

const Field: React.FC<{ k: string; v: unknown; mono?: boolean }> = ({ k, v, mono }) => (
  <div className="kv">
    <dt>{k}</dt>
    <dd className={mono ? "mono" : ""}>{show(v)}</dd>
  </div>
);

/** Actions only call existing, already-authorised endpoints; destructive ones go through the shared confirm dialog. */
export const NodeActions: React.FC<{
  id: string;
  onFocusNode?: (id: string) => void;
  onUnpinNode?: (id: string) => void;
} & ShellProps> = ({ id, onNotify, onNavigate, onFocusNode, onUnpinNode }) => {
  const n = graph.nodes.get(id);
  if (!n) return null;
  const key = id.slice(n.type.length + 1);
  const run = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
      onNotify(label);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };
  const open = (view: Parameters<NonNullable<ShellProps["onNavigate"]>>[0], label: string) => (
    <button className="ghost" onClick={() => onNavigate?.(view)}>{label}</button>
  );
  const relIds = neighbors(id).map((x) => x.node.id);
  return (
    <div className="row wrap">
      <button className="ghost" onClick={() => (onFocusNode ? onFocusNode(id) : onNavigate?.("graph", id))}>Focus camera</button>
      {relIds.length > 0 && (
        <button className="ghost" onClick={() => highlightNodes([id, ...relIds], 6000)}>Highlight neighbors</button>
      )}
      {onUnpinNode && (
        <button className="ghost" onClick={() => onUnpinNode(id)} title="Release fixed drag position">Unpin</button>
      )}
      {n.type === "memory" && n.store === "five_way" && (
        <button className="ghost" onClick={run("Memory archived", () => apiFetch(`/v2/memory/personal/${key}/archive`, { method: "POST" }))}>Archive</button>
      )}
      {n.type === "memory" && (
        <button className="danger" onClick={run("Memory deleted", () => apiFetch(n.store === "five_way" ? `/v2/memory/personal/${key}` : `/memory/items/${key}`, { method: "DELETE" }))}>Delete</button>
      )}
      {n.type === "memory" && open("memory", "Open Memory")}
      {n.type === "project" && (projectExpanded(key)
        ? <button className="ghost" onClick={() => collapseProject(key)}>Hide code graph</button>
        : <button onClick={run("Code graph loaded", () => expandProject(key))}>Load code graph</button>)}
      {n.type === "project" && <button className="ghost" onClick={run("Graphify synced", () => apiFetch(`/v2/projects/${key}/graphify/sync`, { method: "POST" }))}>Sync Graphify</button>}
      {n.type === "project" && open("projects", "Open Projects")}
      {n.type === "task" && ["running", "awaiting_approval"].includes(String(n.state)) && (
        <button className="danger" onClick={run("Cancel requested", () => apiFetch(`/v2/workforce/runs/${key}/cancel`, { method: "POST" }))}>Cancel run</button>
      )}
      {(n.type === "task" || n.type === "agent") && open("workforce", "Open Workforce")}
      {n.type === "skill" && (
        <button className="ghost" onClick={run(n.enabled ? "Skill disabled" : "Skill enabled", () => apiFetch(`/v2/skills/${key}/${n.enabled ? "disable" : "enable"}`, { method: "POST" }))}>
          {n.enabled ? "Disable" : "Enable"}
        </button>
      )}
      {n.type === "skill" && open("skills", "Open Skills")}
      {n.type === "document" && <a className="btn ghost" href={`/api/v2/documents/download/${encodeURIComponent(key)}`} target="_blank" rel="noreferrer">Download</a>}
      {n.type === "model" && open("gateway", "Open Gateway")}
      {n.type === "conversation" && open("chat", "Open Chat")}
      {n.type === "knowledge" && open("learning", "Open Learning")}
    </div>
  );
};

export const NodeDetails: React.FC<{
  id: string;
  onFocusNode?: (id: string) => void;
  onUnpinNode?: (id: string) => void;
} & ShellProps> = (props) => {
  useGraphVersion(); // stay live: metadata refreshes and deletions show up here too
  const n = graph.nodes.get(props.id);
  if (!n) return <p className="empty">This node no longer exists in the graph (it was deleted or filtered out by the server limit).</p>;
  const rel = neighbors(n.id);
  const project = n.project_id ? graph.nodes.get(`project:${n.project_id}`) : null;
  const extra = Object.entries(n).filter(([k]) => !["id", "type", "label", "description", "source", "created_at", "updated_at", "created_iso", "updated_iso", "project_id", "local"].includes(k));
  return (
    <>
      <div className="insp-title">
        <i className="dot" style={{ background: styleOf(n.type).color }} />
        <h2>{n.label}</h2>
      </div>
      <dl>
        <Field k="ID" v={n.id} mono />
        <Field k="Type" v={n.type} />
        <Field k="Description" v={n.description} />
        <Field k="Source" v={n.source} mono />
        <Field k="Created" v={n.created_iso || fmtTime(n.created_at as number)} />
        <Field k="Updated" v={n.updated_iso || fmtTime(n.updated_at as number)} />
        <Field k="Project" v={project ? project.label : n.project_id} />
        {extra.map(([k, v]) => <Field key={k} k={k.replace(/_/g, " ")} v={v} />)}
      </dl>
      <h3>Related ({rel.length})</h3>
      {!rel.length && <p className="empty">No relationships recorded for this node.</p>}
      <ul className="rel-list">
        {rel.slice(0, 60).map(({ node, relation, outgoing }) => (
          <li key={`${relation}${node.id}${outgoing}`}>
            <button className="link" onClick={() => props.onInspect?.({ kind: "node", id: node.id })} title={node.id}>
              <span className="mono dim">{outgoing ? `${relation} →` : `← ${relation}`}</span>
              <i className="dot" style={{ background: styleOf(node.type).color }} />
              {node.label}
            </button>
          </li>
        ))}
      </ul>
      {rel.length > 60 && <p className="dim">+ {rel.length - 60} more</p>}
      <h3>Actions</h3>
      <NodeActions {...props} />
    </>
  );
};

/** Real event stream from /api/events (newest first) with sequence & department tags. */
export const SystemEvents: React.FC = () => {
  const [, tick] = useState(0);
  useEffect(() => onEvent((ev) => ev.type !== "token" && ev.type !== "level" && tick((t) => t + 1)), []);
  const rows = recentEvents.slice(-45).reverse();
  return (
    <>
      <div className="row between" style={{ marginBottom: 8 }}>
        <h3>Live Telemetry Feed</h3>
        <span className="mono dim">{rows.length} events</span>
      </div>
      {!rows.length && <p className="empty">No events yet. Activity from chat, voice, memory and agents appears here live.</p>}
      <ul className="event-list">
        {rows.map(({ at, ev }, i) => (
          <li key={`${at}-${i}`}>
            <span className="mono dim">{ev.seq ? `#${ev.seq}` : new Date(at).toLocaleTimeString()}</span>
            <span className="mono">{ev.event_type || ev.event || ev.type}</span>
            <span className="dim">{[ev.source_department, ev.state, ev.step, ev.run_id, ev.detail, ev.message].filter((x) => typeof x === "string" && x).join(" · ")}</span>
          </li>
        ))}
      </ul>
    </>
  );
};

export const Inspector: React.FC<{ item: Inspectable | null; onClose: () => void } & ShellProps> = ({ item, onClose, ...shell }) => (
  <aside className="inspector" aria-label="Inspector">
    <div className="insp-bar">
      <span className="eyebrow">{item?.kind === "node" ? "Graph node" : item ? "Details" : "Live"}</span>
      <div className="row">
        {item && <button className="ghost" onClick={() => shell.onInspect?.(null)} title="Back to system events">Events</button>}
        <button className="ghost icon" onClick={onClose} aria-label="Close inspector" title="Close inspector">✕</button>
      </div>
    </div>
    <div className="insp-body">
      {!item && <SystemEvents />}
      {item?.kind === "node" && <NodeDetails id={item.id} {...shell} />}
      {item?.kind === "detail" && (
        <>
          <div className="insp-title"><h2>{item.title}</h2></div>
          {item.subtitle && <p className="dim">{item.subtitle}</p>}
          <dl>{Object.entries(item.fields).map(([k, v]) => <Field key={k} k={k.replace(/_/g, " ")} v={v} mono={/id$|path|sha|token/i.test(k)} />)}</dl>
          {item.text && <pre className="diff">{item.text}</pre>}
        </>
      )}
    </div>
  </aside>
);
