import React, { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { degreeMap } from "../graphCore";
import { graph, highlights, useGraphVersion } from "../graphStore";

/** Visual treatment per entity type matching the electric cyan / deep-space Jarvis aesthetic. */
export const TYPE_STYLE: Record<string, { color: string; size: number; label: string }> = {
  note: { color: "#22d3ee", size: 3.4, label: "Notes" },
  memory: { color: "#818cf8", size: 4.2, label: "Memory" },
  experience: { color: "#38bdf8", size: 4.4, label: "Experience" },
  knowledge: { color: "#a78bfa", size: 5.2, label: "Knowledge" },
  conversation: { color: "#e0f2fe", size: 7.5, label: "Conversations" },
  project: { color: "#38bdf8", size: 10.5, label: "Projects" },
  file: { color: "#64748b", size: 1.8, label: "Files" },
  document: { color: "#bae6fd", size: 3.6, label: "Documents" },
  agent: { color: "#34d399", size: 8.2, label: "Agents" },
  task: { color: "#60a5fa", size: 5.4, label: "Tasks" },
  workflow: { color: "#0ea5e9", size: 7.0, label: "Workflows" },
  skill: { color: "#2dd4bf", size: 4.5, label: "Skills" },
  tool: { color: "#06b6d4", size: 4.2, label: "Tools" },
  model: { color: "#3b82f6", size: 4.5, label: "Models" },
};
const FALLBACK = { color: "#38bdf8", size: 3.2, label: "Other" };
export const styleOf = (type: string) => TYPE_STYLE[type] || FALLBACK;

export type LayoutMode = "force" | "cluster" | "radial";
export type LabelMode = "smart" | "all" | "off";
export type EdgeMode = "all" | "highlighted" | "hidden";

export interface ForceConfig {
  repulsion: number; // -380 .. -20
  linkDistance: number; // 25 .. 220
  nodeScale: number; // 0.5 .. 2.2
  labelMode: LabelMode;
  edgeMode: EdgeMode;
  layoutMode: LayoutMode;
  clustering: boolean;
  frozen: boolean;
  speed: number; // 0.25 .. 2.0
}

export const DEFAULT_FORCES: ForceConfig = {
  repulsion: -125,
  linkDistance: 72,
  nodeScale: 1.0,
  labelMode: "smart",
  edgeMode: "all",
  layoutMode: "force",
  clustering: false,
  frozen: false,
  speed: 1.0,
};

const esc = (s: string) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export interface GalaxyHandle {
  fit(): void;
  reset(): void;
  focus(id: string): void;
  unpin(id: string): void;
  reheat(): void;
  sampleFps(): number;
}

interface Props {
  hidden: Set<string>;
  query: string;
  selectedId: string | null;
  forces: ForceConfig;
  onSelect: (id: string | null) => void;
  onFocusNode?: (id: string) => void;
  onContextMenu?: (id: string, x: number, y: number) => void;
}

/* eslint-disable @typescript-eslint/no-explicit-any -- vendored 3d-force-graph + THREE globals */
type FG = any;
declare global {
  interface Window {
    ForceGraph3D?: () => (el: HTMLElement) => FG;
    THREE?: any;
  }
}

const glowTextures = new Map<string, any>();
function getGlowTexture(color: string, hot: boolean) {
  const THREE = window.THREE;
  if (!THREE || typeof document === "undefined") return null;
  const key = `${color}|${hot ? 1 : 0}`;
  const hit = glowTextures.get(key);
  if (hit) return hit;
  const cvs = document.createElement("canvas");
  cvs.width = 96;
  cvs.height = 96;
  const ctx = cvs.getContext("2d");
  if (!ctx) return null;
  const g = ctx.createRadialGradient(48, 48, 2, 48, 48, 48);
  g.addColorStop(0, hot ? "#ffffff" : "#e0f7ff");
  g.addColorStop(0.18, color);
  g.addColorStop(hot ? 0.55 : 0.42, `${color}66`);
  g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 96, 96);
  const tex = new THREE.CanvasTexture(cvs);
  tex.needsUpdate = true;
  glowTextures.set(key, tex);
  return tex;
}

function makeLabelSprite(text: string, color: string) {
  const THREE = window.THREE;
  if (!THREE || typeof document === "undefined") return null;
  const cvs = document.createElement("canvas");
  const ctx = cvs.getContext("2d");
  if (!ctx) return null;
  const clean = text.length > 26 ? `${text.slice(0, 24)}…` : text;
  cvs.width = 256;
  cvs.height = 56;
  ctx.font = "600 18px 'Segoe UI', Inter, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(4, 10, 20, 0.72)";
  const tw = Math.min(244, ctx.measureText(clean).width + 20);
  ctx.fillRect(128 - tw / 2, 12, tw, 32);
  ctx.strokeStyle = `${color}66`;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(128 - tw / 2, 12, tw, 32);
  ctx.fillStyle = "#e2f5ff";
  ctx.fillText(clean, 128, 28);
  const tex = new THREE.CanvasTexture(cvs);
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(28, 6.2, 1);
  sp.position.set(0, 8, 0);
  return sp;
}

const CLUSTER_CENTERS = (() => {
  const types = Object.keys(TYPE_STYLE);
  const out: Record<string, { x: number; y: number; z: number }> = {};
  const radius = 135;
  const phi = Math.PI * (3 - Math.sqrt(5));
  types.forEach((t, i) => {
    const y = 1 - (i / Math.max(1, types.length - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = phi * i;
    out[t] = { x: Math.cos(theta) * r * radius, y: y * radius, z: Math.sin(theta) * r * radius };
  });
  return out;
})();

const RADIAL_RING: Record<string, number> = {
  conversation: 22,
  project: 40,
  workflow: 62,
  agent: 78,
  task: 95,
  tool: 112,
  skill: 120,
  memory: 142,
  note: 156,
  experience: 168,
  knowledge: 175,
  document: 185,
  model: 196,
  file: 215,
};

/** WebGL knowledge graph. Reads the live store and feeds ForceGraph3D incrementally with glowing cyan WebGL sprites,
 *  interactive physics controls, clustering/radial layouts, drag pinning, double-click focus, and context menu. */
export const GalaxyCanvas = React.memo(
  forwardRef<GalaxyHandle, Props>(({ hidden, query, selectedId, forces, onSelect, onFocusNode, onContextMenu }, ref) => {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const fg = useRef<FG>(null);
    const rn = useRef(new Map<string, any>());
    const rl = useRef(new Map<string, any>());
    const shown = useRef({ nodes: new Set<string>(), links: new Set<string>() });
    const view = useRef({ query: "", selectedId: null as string | null, forces });
    view.current = { query: query.trim().toLowerCase(), selectedId, forces };
    const fitted = useRef(false);
    const lastClick = useRef<{ id: string; at: number } | null>(null);
    const degRef = useRef(new Map<string, number>());
    const fpsRef = useRef(60);
    const cb = useRef({ onSelect, onFocusNode, onContextMenu });
    cb.current = { onSelect, onFocusNode, onContextMenu };
    const version = useGraphVersion();

    const focusNode = (id: string) => {
      const r = rn.current.get(id);
      if (!r || r.x === undefined || !fg.current) return;
      const dist = 105;
      const len = Math.max(1, Math.hypot(r.x, r.y, r.z));
      const k = 1 + dist / len;
      fg.current.cameraPosition({ x: r.x * k, y: r.y * k, z: r.z * k }, r, 750);
    };

    const refresh = () => {
      const g = fg.current;
      if (!g) return;
      g.nodeColor(g.nodeColor())
        .nodeVal(g.nodeVal())
        .linkColor(g.linkColor())
        .linkWidth(g.linkWidth())
        .linkOpacity(view.current.forces.edgeMode === "hidden" ? 0 : 0.56)
        .linkVisibility((l: any) => {
          const em = view.current.forces.edgeMode;
          if (em === "hidden") return false;
          if (em === "highlighted") {
            const s = typeof l.source === "object" ? l.source.id : l.source;
            const t = typeof l.target === "object" ? l.target.id : l.target;
            const sel = view.current.selectedId;
            return s === sel || t === sel || (highlights.get(s) ?? 0) > Date.now() || (highlights.get(t) ?? 0) > Date.now();
          }
          return true;
        })
        .linkDirectionalParticles(g.linkDirectionalParticles())
        .nodeThreeObject(g.nodeThreeObject());
    };

    useEffect(() => {
      const el = hostRef.current;
      if (!el || !window.ForceGraph3D) return;
      const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
      const lit = (id: string) => (highlights.get(id) ?? 0) > Date.now();
      const endId = (e: any) => (typeof e === "object" ? e.id : e);
      const isNeighborOfSelected = (id: string) => {
        const sel = view.current.selectedId;
        if (!sel || sel === id) return false;
        for (const l of graph.links.values()) {
          if ((l.source === sel && l.target === id) || (l.target === sel && l.source === id)) return true;
        }
        return false;
      };
      const litLink = (l: any) => {
        const s = endId(l.source), t = endId(l.target), sel = view.current.selectedId;
        return (lit(s) && lit(t)) || s === sel || t === sel;
      };
      const matches = (n: any) =>
        !view.current.query ||
        `${n.label} ${n.type} ${n.category || ""} ${n.source || ""}`.toLowerCase().includes(view.current.query);

      const g = window.ForceGraph3D()(el)
        .backgroundColor("#03060c")
        .showNavInfo(false)
        .enableNodeDrag(true)
        .nodeLabel((n: any) => {
          const deg = degRef.current.get(n.id) || 0;
          const st = n.state ? ` · ${esc(n.state)}` : "";
          return `<div style="line-height:1.35"><strong>${esc(n.label)}</strong><div style="opacity:.78;font-size:11px">${esc(n.type)}${st} · ${deg} links</div></div>`;
        })
        .nodeColor((n: any) => {
          if (lit(n.id) || n.id === view.current.selectedId) return "#e0f7ff";
          if (isNeighborOfSelected(n.id)) return "#67e8f9";
          return matches(n) ? styleOf(n.type).color : "#0f172a";
        })
        .nodeVal((n: any) => {
          const scale = view.current.forces.nodeScale;
          const deg = degRef.current.get(n.id) || 0;
          const base = styleOf(n.type).size + Math.min(4.5, deg * 0.35);
          const boost = lit(n.id) || n.id === view.current.selectedId ? 2.1 : isNeighborOfSelected(n.id) ? 1.4 : 1;
          return base * boost * scale;
        })
        .nodeThreeObjectExtend(true)
        .nodeThreeObject((n: any) => {
          const THREE = window.THREE;
          if (!THREE) return null;
          const hot = lit(n.id) || n.id === view.current.selectedId;
          const matched = matches(n);
          const color = hot ? "#e0f7ff" : styleOf(n.type).color;
          const tex = getGlowTexture(color, hot);
          if (!tex) return null;
          const group = new THREE.Group();
          const mat = new THREE.SpriteMaterial({
            map: tex,
            transparent: true,
            depthWrite: false,
            opacity: matched ? (hot ? 0.96 : 0.72) : 0.14,
            blending: THREE.AdditiveBlending,
          });
          const sprite = new THREE.Sprite(mat);
          const deg = degRef.current.get(n.id) || 0;
          const s = (7 + Math.min(10, deg * 0.85)) * view.current.forces.nodeScale * (hot ? 1.65 : 1);
          sprite.scale.set(s, s, 1);
          group.add(sprite);

          const lm = view.current.forces.labelMode;
          const showLabel = lm === "all" || (lm === "smart" && (hot || isNeighborOfSelected(n.id) || (view.current.query && matched) || deg >= 3));
          if (showLabel && n.label) {
            const label = makeLabelSprite(String(n.label), color);
            if (label) group.add(label);
          }
          return group;
        })
        .linkLabel((l: any) => esc(l.relation))
        .linkOpacity(0.56)
        .linkColor((l: any) => (litLink(l) ? "#67e8f9" : "#1d4ed8"))
        .linkWidth((l: any) => (litLink(l) ? 1.8 : 0.55))
        .linkDirectionalParticles((l: any) => (!calm && litLink(l) ? 3 : 0))
        .linkDirectionalParticleWidth(1.8)
        .linkDirectionalParticleSpeed(0.014)
        .cooldownTime(6000)
        .onNodeClick((n: any) => {
          const now = performance.now();
          const prev = lastClick.current;
          lastClick.current = { id: n.id, at: now };
          cb.current.onSelect(n.id);
          if (prev && prev.id === n.id && now - prev.at < 360) {
            focusNode(n.id);
            cb.current.onFocusNode?.(n.id);
          }
        })
        .onNodeRightClick((n: any, event: MouseEvent) => {
          event?.preventDefault?.();
          cb.current.onSelect(n.id);
          if (event) cb.current.onContextMenu?.(n.id, event.clientX, event.clientY);
        })
        .onNodeDragEnd((n: any) => {
          n.fx = n.x;
          n.fy = n.y;
          n.fz = n.z;
        })
        .onBackgroundClick(() => cb.current.onSelect(null));

      const initW = el.clientWidth > 40 ? el.clientWidth : 640;
      const initH = el.clientHeight > 40 ? el.clientHeight : 480;
      g.width(initW).height(initH);
      g.d3Force("charge").distanceMax(260);
      fg.current = g;

      // Measure real FPS smoothly for diagnostics/performance reporting
      let frames = 0;
      let f0 = performance.now();
      let fRaf = 0;
      const trackFps = (now: number) => {
        frames++;
        if (now - f0 >= 1000) {
          fpsRef.current = Math.round((frames * 1000) / (now - f0));
          frames = 0;
          f0 = now;
        }
        fRaf = requestAnimationFrame(trackFps);
      };
      fRaf = requestAnimationFrame(trackFps);

      const ro = new ResizeObserver(() => {
        const w = el.clientWidth;
        const h = el.clientHeight;
        if (w > 40 && h > 40) {
          g.width(w).height(h);
        }
      });
      ro.observe(el);
      const onVis = () => (document.hidden ? g.pauseAnimation() : !calm && g.resumeAnimation());
      document.addEventListener("visibilitychange", onVis);
      return () => {
        cancelAnimationFrame(fRaf);
        ro.disconnect();
        document.removeEventListener("visibilitychange", onVis);
        g._destructor?.();
        fg.current = null;
      };
    }, []);

    // Apply live ForceConfig & LayoutMode updates directly to simulation
    useEffect(() => {
      const g = fg.current;
      if (!g) return;
      const charge = g.d3Force("charge");
      if (charge) charge.strength(forces.repulsion);
      const link = g.d3Force("link");
      if (link) link.distance(forces.linkDistance);
      g.d3VelocityDecay(Math.max(0.15, Math.min(0.85, 0.45 / Math.max(0.25, forces.speed))));
      g.linkDirectionalParticleSpeed(0.012 * forces.speed);

      if (forces.layoutMode === "cluster" || forces.clustering) {
        g.d3Force("cluster", (alpha: number) => {
          const k = alpha * 0.24;
          for (const node of rn.current.values()) {
            const c = CLUSTER_CENTERS[node.type] || { x: 0, y: 0, z: 0 };
            if (typeof node.vx === "number") {
              node.vx += (c.x - (node.x || 0)) * k;
              node.vy += (c.y - (node.y || 0)) * k;
              node.vz += (c.z - (node.z || 0)) * k;
            }
          }
        });
        g.d3Force("radial", null);
      } else if (forces.layoutMode === "radial") {
        g.d3Force("cluster", null);
        g.d3Force("radial", (alpha: number) => {
          const k = alpha * 0.22;
          for (const node of rn.current.values()) {
            const targetR = RADIAL_RING[node.type] || 150;
            const cur = Math.hypot(node.x || 1, node.y || 1, node.z || 1);
            const scale = (targetR - cur) / cur;
            if (typeof node.vx === "number") {
              node.vx += (node.x || 0) * scale * k;
              node.vy += (node.y || 0) * scale * k;
              node.vz += (node.z || 0) * scale * k;
            }
          }
        });
      } else {
        g.d3Force("cluster", null);
        g.d3Force("radial", null);
      }

      if (forces.frozen) {
        g.cooldownTicks(0);
      } else {
        g.cooldownTicks(Infinity);
        g.d3ReheatSimulation();
      }
      refresh();
    }, [forces]);

    useEffect(() => {
      const g = fg.current;
      if (!g) return;
      degRef.current = degreeMap(graph);
      const visN = new Set<string>();
      const nodes: any[] = [];
      for (const n of graph.nodes.values()) {
        if (hidden.has(n.type)) continue;
        visN.add(n.id);
        let r = rn.current.get(n.id);
        if (!r) rn.current.set(n.id, (r = { ...n }));
        else Object.assign(r, n);
        nodes.push(r);
      }
      for (const id of rn.current.keys()) if (!graph.nodes.has(id)) rn.current.delete(id);

      const visL = new Set<string>();
      const links: any[] = [];
      for (const [k, l] of graph.links) {
        if (!visN.has(l.source) || !visN.has(l.target)) continue;
        visL.add(k);
        let r = rl.current.get(k);
        if (!r) rl.current.set(k, (r = { source: l.source, target: l.target, relation: l.relation }));
        links.push(r);
      }
      for (const k of rl.current.keys()) if (!graph.links.has(k)) rl.current.delete(k);

      const same =
        visN.size === shown.current.nodes.size &&
        visL.size === shown.current.links.size &&
        [...visN].every((x) => shown.current.nodes.has(x)) &&
        [...visL].every((x) => shown.current.links.has(x));
      shown.current = { nodes: visN, links: visL };

      if (!same) {
        g.graphData({ nodes, links });
        if (!fitted.current && nodes.length) {
          fitted.current = true;
          setTimeout(() => fg.current?.zoomToFit(550, 32), 700);
        }
      } else refresh();
    }, [version, hidden]);

    useEffect(() => {
      refresh();
    }, [query, selectedId]);

    useImperativeHandle(ref, () => ({
      fit: () => fg.current?.zoomToFit(600, 60),
      reset: () => {
        const g = fg.current;
        if (!g) return;
        for (const r of rn.current.values()) for (const k of ["x", "y", "z", "vx", "vy", "vz", "fx", "fy", "fz"]) delete r[k];
        g.graphData(g.graphData());
        g.d3ReheatSimulation();
        setTimeout(() => fg.current?.zoomToFit(600, 60), 1500);
      },
      focus: focusNode,
      unpin: (id: string) => {
        const r = rn.current.get(id);
        if (r) {
          delete r.fx;
          delete r.fy;
          delete r.fz;
          fg.current?.d3ReheatSimulation();
        }
      },
      reheat: () => fg.current?.d3ReheatSimulation(),
      sampleFps: () => fpsRef.current,
    }));

    return <div ref={hostRef} className="galaxy-canvas" role="img" aria-label="3D knowledge graph canvas" />;
  })
);
