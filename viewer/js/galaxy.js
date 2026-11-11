// The 3D knowledge galaxy (vendored 3d-force-graph). Real data only: nodes and links come from GET /api/graph.
import { $, api, colorOf, emit, sleep } from "./api.js";

const DIM = "#121a26";
let G, nodes = [], links = [], neighbors = new Map(), byPath = new Map(), byTitle = new Map();
let highlight = new Set(), selected = null, hidden = new Set(), pulse = new Map(), version = -1;
let showLabels = true, showLinks = true, home = null, lastClick = { path: null, t: 0 };

export const galaxy = {
  get nodes() { return nodes; }, get links() { return links; }, get selected() { return selected; },
  node: (path) => byPath.get(path), byTitle: (t) => byTitle.get(t.toLowerCase()),
  neighborsOf: (n) => [...(neighbors.get(n.id) || [])].map((i) => nodes[i]),
  degree: (n) => neighbors.get(n.id)?.size || 0,

  init() {
    const box = $("#galaxy");
    G = ForceGraph3D()(box).backgroundColor("rgba(0,0,0,0)").showNavInfo(false)
      .nodeLabel((n) => `${n.label} · ${n.group}`)
      .nodeColor((n) => pulse.has(n.path) ? "#ffffff" : highlight.size && !highlight.has(n.id) ? DIM : colorOf(n.group))
      .nodeVal((n) => 1.6 + galaxy.degree(n) * 0.55 + (pulse.get(n.path) || 0) * 12 + (n === selected ? 3 : 0))
      .nodeOpacity(0.95).nodeResolution(12).nodeRelSize(2.4)
      .nodeVisibility((n) => !hidden.has(n.group))
      .linkVisibility((l) => showLinks && !hidden.has(l.source.group) && !hidden.has(l.target.group))
      .linkColor((l) => lit(l) ? "#7fe9ff" : "#2c4a68").linkWidth((l) => lit(l) ? 0.7 : 0.2).linkOpacity(0.35)
      .onNodeClick((n) => {
        const dbl = lastClick.path === n.path && Date.now() - lastClick.t < 400;
        lastClick = { path: n.path, t: Date.now() };
        dbl ? galaxy.explore(n) : galaxy.select(n.path);
      })
      .onBackgroundClick(() => galaxy.clear())
      .warmupTicks(40).cooldownTicks(220);
    G.renderer().setPixelRatio(Math.min(devicePixelRatio, 1.5));  // GTX 1650: do not render at 2x on HiDPI
    const c = G.controls(); c.autoRotate = true; c.autoRotateSpeed = 0.3;
    box.addEventListener("pointerdown", () => { c.autoRotate = false; emit("rotate", false); });
    new ResizeObserver(() => G.width(innerWidth).height(innerHeight)).observe(document.body);
    document.addEventListener("visibilitychange", () => (document.hidden ? G.pauseAnimation() : G.resumeAnimation()));
    G.onEngineStop(() => { if (!home) { G.zoomToFit(600, 90); setTimeout(() => (home = { ...G.cameraPosition() }), 700); } });
    requestAnimationFrame(labelLoop);
  },

  async refresh(newPaths = []) {
    const g = await api("/graph");
    if (g.version === version && !newPaths.length) return;
    version = g.version;
    nodes = g.nodes.map((n) => Object.assign(byPath.get(n.path) || {}, n));
    byPath = new Map(nodes.map((n) => [n.path, n])); byTitle = new Map(nodes.map((n) => [n.label.toLowerCase(), n]));
    neighbors = new Map(nodes.map((n) => [n.id, new Set()]));
    g.links.forEach((l) => { neighbors.get(l.source).add(l.target); neighbors.get(l.target).add(l.source); });
    for (const p of newPaths) {  // a new note is born next to its most related existing node
      const n = byPath.get(p); if (!n || n.x !== undefined) continue;
      const nb = galaxy.neighborsOf(n).find((o) => o.x !== undefined);
      if (nb) Object.assign(n, { x: nb.x + (Math.random() - 0.5) * 12, y: nb.y + (Math.random() - 0.5) * 12, z: nb.z + (Math.random() - 0.5) * 12 });
    }
    links = g.links;
    const big = nodes.length > 400;  // keep large collections interactive
    G.nodeResolution(big ? 4 : 12).linkOpacity(big ? 0.14 : 0.35).cooldownTicks(big ? 120 : 220);
    G.graphData({ nodes, links: links.map((l) => ({ source: nodes[l.source], target: nodes[l.target] })) });
    if (selected) selected = byPath.get(selected.path) || null;
    highlight = selected ? new Set([selected.id, ...neighbors.get(selected.id)]) : new Set();
    paint(); emit("graph", { nodes, links });
    newPaths.forEach((p) => { const n = byPath.get(p); if (n) birth(n); });
  },

  select(path, fly = true) {
    const n = byPath.get(path); if (!n) return;
    selected = n; highlight = new Set([n.id, ...neighbors.get(n.id)]); paint();
    if (fly) flyTo(n);
    emit("select", n);
  },
  explore(n) {  // double-click: light up everything within two steps
    selected = n; highlight = new Set([n.id]);
    for (const a of neighbors.get(n.id)) { highlight.add(a); for (const b of neighbors.get(a)) highlight.add(b); }
    paint(); emit("select", n); emit("explore", highlight.size - 1);
  },
  clear() { selected = null; highlight = new Set(); paint(); emit("select", null); },

  /** Highlight a set of notes (answer sources, search hits). Flies to one, frames many. */
  async focus(paths, isNew = false, group = false) {
    if (paths.some((p) => !byPath.has(p))) await galaxy.refresh(isNew ? paths : []);
    const ns = paths.map((p) => byPath.get(p)).filter(Boolean); if (!ns.length) return;
    if (isNew) await sleep(300);
    if (ns.length === 1 || (!group && ns.length < 4)) return galaxy.select(ns[0].path);  // an answer flies to its top source
    selected = null; highlight = new Set(ns.map((n) => n.id)); paint(); emit("select", null);
    G.controls().autoRotate = false;
    const c = ns.reduce((a, n) => ({ x: a.x + n.x / ns.length, y: a.y + n.y / ns.length, z: a.z + n.z / ns.length }), { x: 0, y: 0, z: 0 });
    G.cameraPosition({ x: c.x + 150, y: c.y + 40, z: c.z + 150 }, c, 1000);
  },
  search(q) {
    q = q.trim().toLowerCase();
    const hits = q ? nodes.filter((n) => (n.label + " " + n.excerpt).toLowerCase().includes(q)) : [];
    selected = null; highlight = new Set(hits.map((n) => n.id)); paint();
    return hits.sort((a, b) => b.label.toLowerCase().includes(q) - a.label.toLowerCase().includes(q));  // title matches first
  },

  fit() { G.zoomToFit(700, 90); },
  reset() { if (home) G.cameraPosition(home, { x: 0, y: 0, z: 0 }, 800); else G.zoomToFit(700, 90); },
  forces(repel, dist) { G.d3Force("charge").strength(-repel); G.d3Force("link").distance(dist); G.d3ReheatSimulation(); },
  rotate(v) { G.controls().autoRotate = v; },
  setLabels(v) { showLabels = v; }, setLinks(v) { showLinks = v; G.linkVisibility(G.linkVisibility()); },
  toggleGroup(g) { hidden.has(g) ? hidden.delete(g) : hidden.add(g); G.nodeVisibility(G.nodeVisibility()).linkVisibility(G.linkVisibility()); return !hidden.has(g); },
  holo(v) { G.linkOpacity(v ? 0.7 : nodes.length > 400 ? 0.14 : 0.35); G.controls().autoRotateSpeed = v ? 0.9 : 0.3; if (v) G.controls().autoRotate = true; },
  hubs: (k = 8) => [...nodes].sort((a, b) => galaxy.degree(b) - galaxy.degree(a)).slice(0, k),
};

const lit = (l) => highlight.size && highlight.has(l.source.id) && highlight.has(l.target.id);
function paint() {
  G.nodeColor(G.nodeColor()).nodeVal(G.nodeVal()).linkColor(G.linkColor()).linkWidth(G.linkWidth());
  requestAnimationFrame(glow);
}
function glow() {  // make spheres self-lit so they read as glowing stars (materials are cached per colour by the library)
  for (const n of nodes) {
    const m = n.__threeObj?.material;
    if (m && m.emissive && m.__glow !== m.color.getHex()) { m.emissive.copy(m.color); m.emissiveIntensity = 0.38; m.__glow = m.color.getHex(); }
  }
}
function flyTo(n) {
  G.controls().autoRotate = false; emit("rotate", false);
  // keep the current viewing direction and stop at a readable distance, so neighbours stay in frame
  const c = G.cameraPosition(), v = { x: c.x - n.x, y: c.y - n.y, z: c.z - n.z }, len = Math.hypot(v.x, v.y, v.z) || 1, d = 190;
  G.cameraPosition({ x: n.x + (v.x / len) * d, y: n.y + (v.y / len) * d, z: n.z + (v.z / len) * d }, n, 1100);
}
async function birth(n) {
  pulse.set(n.path, 1); paint();
  for (let i = 1; i <= 12; i++) { await sleep(110); pulse.set(n.path, 1 - i / 12); paint(); }
  pulse.delete(n.path); paint();
}

// Labels: an HTML overlay for hubs and highlighted nodes only (at most 40), positioned once per frame.
const pool = [];
function labelLoop() {
  requestAnimationFrame(labelLoop);
  if (document.hidden || !G || !nodes.length) return;
  const box = $("#labels");
  let want = [];
  if (highlight.size) want = [...highlight].slice(0, 40).map((i) => nodes[i]);
  else if (showLabels) want = galaxy.hubs(Math.min(12, Math.ceil(nodes.length / 3)));
  want = want.filter((n) => n && n.x !== undefined && !hidden.has(n.group));
  while (pool.length < want.length) { const s = document.createElement("span"); box.append(s); pool.push(s); }
  pool.forEach((s, i) => {
    const n = want[i];
    if (!n) { s.style.display = "none"; return; }
    const p = G.graph2ScreenCoords(n.x, n.y, n.z);
    s.style.display = ""; s.style.left = p.x + "px"; s.style.top = p.y + "px";
    if (s.textContent !== n.label) s.textContent = n.label;
    s.className = highlight.has(n.id) ? "hl" : "";
  });
  glow();
}
