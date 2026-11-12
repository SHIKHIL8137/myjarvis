/** Pure incremental graph merge. No imports, so `node --test` runs it directly (see graphCore.test.ts). */
export interface GNode {
  id: string; // stable "<type>:<key>" from the backend
  type: string;
  label: string;
  local?: boolean; // loaded on demand by this client (project code graph); snapshots must not delete it
  [k: string]: unknown;
}
export interface GLink {
  source: string;
  target: string;
  relation: string;
  local?: boolean;
  [k: string]: unknown;
}
export interface Delta {
  nodes?: GNode[];
  links?: GLink[];
  removed?: string[];
  seq?: number;
}
export interface Graph {
  nodes: Map<string, GNode>;
  links: Map<string, GLink>;
  lastSeq: number;
  seenSeqs: Set<number>;
}

export const linkKey = (l: GLink): string => `${l.source}|${l.relation}|${l.target}`;
export const createGraph = (): Graph => ({ nodes: new Map(), links: new Map(), lastSeq: 0, seenSeqs: new Set() });

/** Guard against duplicate or stale out-of-order deliveries when events carry monotonic `seq`. */
export function acceptSeq(g: Graph, seq?: number): boolean {
  if (typeof seq !== "number" || !Number.isFinite(seq) || seq <= 0) return true;
  if (g.seenSeqs.has(seq) || seq <= g.lastSeq) return false;
  g.seenSeqs.add(seq);
  if (seq > g.lastSeq) g.lastSeq = seq;
  if (g.seenSeqs.size > 1000) {
    const cutoff = g.lastSeq - 600;
    for (const s of g.seenSeqs) if (s < cutoff) g.seenSeqs.delete(s);
  }
  return true;
}

/** Upsert by id (existing objects are updated in place), drop removed nodes with their edges. Returns true if anything changed. */
export function applyDelta(g: Graph, d: Delta): boolean {
  if (!acceptSeq(g, d.seq)) return false;
  let changed = false;
  for (const n of d.nodes ?? []) {
    const cur = g.nodes.get(n.id);
    if (!cur) {
      g.nodes.set(n.id, { ...n });
      changed = true;
    } else if (Object.keys(n).some((k) => cur[k] !== n[k])) {
      Object.assign(cur, n);
      changed = true;
    }
  }
  for (const id of d.removed ?? []) {
    if (g.nodes.delete(id)) changed = true;
    for (const [k, l] of g.links) {
      if (l.source === id || l.target === id) {
        g.links.delete(k);
        changed = true;
      }
    }
  }
  for (const l of d.links ?? []) {
    const k = linkKey(l);
    const curL = g.links.get(k);
    if (!curL) {
      g.links.set(k, { ...l });
      changed = true;
    } else if (Object.keys(l).some((key) => curL[key] !== l[key])) {
      Object.assign(curL, l);
      changed = true;
    }
  }
  return changed;
}

/** Diff against a full server snapshot: whatever the server no longer has disappears, client-loaded (local) items stay. */
export function applySnapshot(g: Graph, snap: { nodes: GNode[]; links: GLink[]; seq?: number }): boolean {
  const ids = new Set(snap.nodes.map((n) => n.id));
  const keys = new Set(snap.links.map(linkKey));
  const removed = [...g.nodes.values()].filter((n) => !n.local && !ids.has(n.id)).map((n) => n.id);
  let changed = applyDelta(g, { nodes: snap.nodes, links: snap.links, removed });
  if (typeof snap.seq === "number" && snap.seq > g.lastSeq) g.lastSeq = snap.seq;
  for (const [k, l] of g.links) {
    if (!l.local && !keys.has(k)) {
      g.links.delete(k);
      changed = true;
    }
  }
  return changed;
}

/** Compute real undirected degree (number of incident edges) per node id. */
export function degreeMap(g: Graph): Map<string, number> {
  const deg = new Map<string, number>();
  for (const l of g.links.values()) {
    if (g.nodes.has(l.source) && g.nodes.has(l.target)) {
      deg.set(l.source, (deg.get(l.source) || 0) + 1);
      deg.set(l.target, (deg.get(l.target) || 0) + 1);
    }
  }
  return deg;
}

/** Return top connected hubs ranked strictly by real degree centrality (highest first). */
export function topHubs(g: Graph, limit = 8): Array<{ node: GNode; degree: number }> {
  const deg = degreeMap(g);
  return [...deg.entries()]
    .map(([id, degree]) => ({ node: g.nodes.get(id)!, degree }))
    .filter((x) => Boolean(x.node))
    .sort((a, b) => b.degree - a.degree || a.node.label.localeCompare(b.node.label))
    .slice(0, Math.max(0, limit));
}
