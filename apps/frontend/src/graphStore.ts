/** Live knowledge-graph store: snapshot from GET /api/v2/graph, then incremental `graph_event` deltas over SSE. */
import { useSyncExternalStore } from "react";
import { apiFetch } from "./api/client";
import { applyDelta, applySnapshot, createGraph, degreeMap, topHubs, type Delta, type GLink, type GNode } from "./graphCore";
import { onEvent } from "./live";

export const graph = createGraph();
/** node id -> expiry (ms epoch). Set by real backend activity (a turn retrieving a memory, an agent run touching a project). */
export const highlights = new Map<string, number>();
export const stats = {
  loaded: false,
  error: "",
  events: 0,
  seq: 0,
  lastEventType: "",
  latencyMs: null as number | null, // backend publish -> applied in the browser, for the last delta
  truncated: {} as Record<string, number>,
};

const HIGHLIGHT_MS = 8000;
const PER_TYPE_LIMIT = 300; // progressive loading: newest N per type; project code graphs load on demand
let version = 0;
const subs = new Set<() => void>();
const bump = () => {
  version++;
  subs.forEach((f) => f());
};

let queue: Array<{ delta: Delta; ts?: number; highlight?: string[] }> = [];
let flushTimer = 0;
let expiryTimer = 0;
let resyncTimer = 0;

/** Batch bursts of events into one store update / one render. */
function flush() {
  flushTimer = 0;
  const batch = queue;
  queue = [];
  const now = Date.now();
  for (const { delta, ts, highlight } of batch) {
    applyDelta(graph, delta);
    if (ts) stats.latencyMs = Math.max(0, Math.round(now - ts * 1000));
    for (const id of highlight ?? []) highlights.set(id, now + HIGHLIGHT_MS);
  }
  clearTimeout(expiryTimer);
  if (highlights.size) {
    expiryTimer = window.setTimeout(() => {
      const t = Date.now();
      for (const [id, exp] of highlights) if (exp <= t) highlights.delete(id);
      bump();
    }, HIGHLIGHT_MS + 50);
  }
  bump();
}

/** Temporarily highlight specific real nodes (used by neighbor/search exploration without inventing data). */
export function highlightNodes(ids: string[], durationMs = 5000) {
  const exp = Date.now() + durationMs;
  for (const id of ids) highlights.set(id, exp);
  bump();
  clearTimeout(expiryTimer);
  expiryTimer = window.setTimeout(() => {
    const t = Date.now();
    for (const [id, e] of highlights) if (e <= t) highlights.delete(id);
    bump();
  }, durationMs + 50);
}

/** Diff against a fresh snapshot (initial load, reconnect, and changes the backend cannot express as a delta). Debounced. */
export function resync() {
  clearTimeout(resyncTimer);
  resyncTimer = window.setTimeout(async () => {
    try {
      const snap = await apiFetch<{ seq?: number; nodes: GNode[]; links: GLink[]; truncated: Record<string, number> }>(`/v2/graph?limit=${PER_TYPE_LIMIT}`);
      applySnapshot(graph, snap);
      if (typeof snap.seq === "number" && snap.seq > stats.seq) stats.seq = snap.seq;
      stats.truncated = snap.truncated || {};
      stats.loaded = true;
      stats.error = "";
    } catch (e) {
      stats.error = (e as Error).message;
    }
    bump();
  }, 180);
}

let stop: (() => void) | null = null;
/** Idempotent. Reconnect checks `resync_required` and resyncs when sequence gap detected. */
export function startGraph(): () => void {
  if (!stop) {
    const off = onEvent((ev) => {
      if (typeof ev.seq === "number" && ev.seq > stats.seq) stats.seq = ev.seq;
      if (ev.type === "graph_event") {
        stats.events++;
        stats.lastEventType = String(ev.event_type || ev.event || "graph_event");
        if (ev.resync) resync();
        queue.push({ delta: { seq: ev.seq, nodes: ev.nodes, links: ev.links, removed: ev.removed }, ts: ev.ts, highlight: ev.highlight });
        if (!flushTimer) flushTimer = window.setTimeout(flush, 80);
      } else if (ev.type === "hello") {
        if (!stats.loaded || ev.resync_required) resync();
      } else if (ev.type === "graph_changed" || ev.type === "memory_purged" || ev.type === "knowledge_updated") {
        resync();
      }
    });
    resync();
    stop = () => {
      off();
      stop = null;
    };
  }
  return stop;
}

/** Progressive loading: pull one project's Graphify code graph into the view (Project contains files). */
export async function expandProject(pid: string): Promise<number> {
  type PG = { nodes: Array<{ id: string; label: string; group?: string; path?: string }>; links: Array<{ source: string; target: string; relation?: string }> };
  let g = await apiFetch<PG>(`/v2/projects/${pid}/graph`);
  if (!g.nodes?.length) {
    await apiFetch(`/v2/projects/${pid}/graphify/sync`, { method: "POST" });
    g = await apiFetch<PG>(`/v2/projects/${pid}/graph`);
  }
  const fid = (id: string) => `file:${pid}:${id}`;
  applyDelta(graph, {
    nodes: g.nodes.map((n) => ({ id: fid(n.id), type: "file", label: n.label, source: n.path, category: n.group, project_id: pid, local: true })),
    links: [
      ...g.links.map((l) => ({ source: fid(l.source), target: fid(l.target), relation: l.relation || "linked", local: true })),
      ...g.nodes.map((n) => ({ source: `project:${pid}`, target: fid(n.id), relation: "contains", local: true })),
    ],
  });
  bump();
  return g.nodes.length;
}

export function collapseProject(pid: string) {
  const removed = [...graph.nodes.values()].filter((n) => n.type === "file" && n.project_id === pid).map((n) => n.id);
  if (applyDelta(graph, { removed })) bump();
}

export const projectExpanded = (pid: string) => [...graph.nodes.values()].some((n) => n.type === "file" && n.project_id === pid);

/** Relationship exploration: every edge touching `id`, with the node on the other side. */
export function neighbors(id: string): Array<{ node: GNode; relation: string; outgoing: boolean }> {
  const out = [];
  for (const l of graph.links.values()) {
    const other = l.source === id ? l.target : l.target === id ? l.source : null;
    const node = other && graph.nodes.get(other);
    if (node) out.push({ node, relation: l.relation, outgoing: l.source === id });
  }
  return out;
}

export const graphDegrees = () => degreeMap(graph);
export const graphTopHubs = (limit = 8) => topHubs(graph, limit);

const subscribe = (f: () => void) => {
  subs.add(f);
  return () => subs.delete(f);
};
/** Re-render when the graph changes; read `graph` / `highlights` / `stats` directly. */
export const useGraphVersion = (): number => useSyncExternalStore(subscribe, () => version);
