import React, { useEffect, useRef } from "react";

export interface GraphNode {
  id: string | number;
  label: string;
  group?: string;
  path?: string;
}

export interface GraphLink {
  source: string | number;
  target: string | number;
}

interface Props {
  nodes: GraphNode[];
  links: GraphLink[];
  onSelectNode?: (node: GraphNode) => void;
}

interface ForceGraphInstance {
  backgroundColor(c: string): ForceGraphInstance;
  showNavInfo(b: boolean): ForceGraphInstance;
  nodeLabel(fn: (n: GraphNode) => string): ForceGraphInstance;
  nodeColor(fn: (n: GraphNode) => string): ForceGraphInstance;
  nodeVal(fn: () => number): ForceGraphInstance;
  linkColor(fn: () => string): ForceGraphInstance;
  linkWidth(w: number): ForceGraphInstance;
  onNodeClick(fn: (n: GraphNode) => void): ForceGraphInstance;
  width(w: number): ForceGraphInstance;
  height(h: number): ForceGraphInstance;
  graphData(d: { nodes: GraphNode[]; links: GraphLink[] }): ForceGraphInstance;
  _destructor?: () => void;
}

declare global {
  interface Window {
    ForceGraph3D?: () => (el: HTMLElement) => ForceGraphInstance;
  }
}

const PALETTE = ["#48e5ff", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa"];

function colorForGroup(group = "general"): string {
  let h = 0;
  for (let i = 0; i < group.length; i++) h = (h * 31 + group.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

/** Memoized WebGL 3D Graph Island: preserves GPU context cleanly across chat/UI updates. */
export const GalaxyCanvas: React.FC<Props> = React.memo(({ nodes, links, onSelectNode }) => {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<ForceGraphInstance | null>(null);
  const cbRef = useRef(onSelectNode);
  cbRef.current = onSelectNode;

  useEffect(() => {
    const el = hostRef.current;
    if (!el || !window.ForceGraph3D) return;
    const g = window.ForceGraph3D()(el);
    g.backgroundColor("#060913")
      .showNavInfo(false)
      .nodeLabel((n: GraphNode) => `${n.label} [${n.group || "node"}]`)
      .nodeColor((n: GraphNode) => colorForGroup(n.group))
      .nodeVal(() => 3)
      .linkColor(() => "rgba(72,229,255,0.22)")
      .linkWidth(0.7)
      .onNodeClick((n: GraphNode) => cbRef.current?.(n));
    graphRef.current = g;

    const onResize = () => {
      if (hostRef.current && graphRef.current) {
        graphRef.current.width(hostRef.current.clientWidth).height(hostRef.current.clientHeight);
      }
    };
    window.addEventListener("resize", onResize);
    setTimeout(onResize, 60);
    return () => {
      window.removeEventListener("resize", onResize);
      if (graphRef.current && typeof graphRef.current._destructor === "function") {
        graphRef.current._destructor();
      }
    };
  }, []);

  useEffect(() => {
    if (!graphRef.current) return;
    graphRef.current.graphData({
      nodes: nodes.map((n) => ({ ...n })),
      links: links.map((l) => ({ ...l })),
    });
  }, [nodes, links]);

  return <div ref={hostRef} className="galaxy-canvas" />;
});
