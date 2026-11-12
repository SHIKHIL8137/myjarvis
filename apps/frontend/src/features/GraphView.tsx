import React, { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import {
  GalaxyCanvas,
  styleOf,
  type ForceConfig,
  type GalaxyHandle,
  type LabelMode,
  type LayoutMode,
} from "../components/GalaxyCanvas";
import {
  collapseProject,
  expandProject,
  graph,
  highlightNodes,
  neighbors,
  projectExpanded,
  resync,
  stats,
  useGraphVersion,
} from "../graphStore";
import { connState, onConnState } from "../live";
import type { ShellProps } from "../shell";

interface GraphViewProps extends ShellProps {
  selectedId: string | null;
  query: string;
  forces: ForceConfig;
  onChangeForces: (next: ForceConfig) => void;
  canvasRef: React.RefObject<GalaxyHandle>;
}

export const GraphView: React.FC<GraphViewProps> = ({
  onInspect,
  onNotify,
  selectedId,
  query,
  forces,
  onChangeForces,
  canvasRef,
}) => {
  const version = useGraphVersion();
  const wrap = useRef<HTMLDivElement>(null);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [full, setFull] = useState(false);
  const [, setConn] = useState(0);
  const [ctxMenu, setCtxMenu] = useState<{ id: string; x: number; y: number } | null>(null);

  useEffect(() => onConnState(() => setConn((c) => c + 1)), []);
  useEffect(() => {
    const f = () => setFull(document.fullscreenElement === wrap.current);
    document.addEventListener("fullscreenchange", f);
    return () => document.removeEventListener("fullscreenchange", f);
  }, []);
  useEffect(() => {
    const close = () => setCtxMenu(null);
    addEventListener("click", close);
    return () => removeEventListener("click", close);
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of graph.nodes.values()) c[n.type] = (c[n.type] || 0) + 1;
    return c;
  }, [version]);

  const select = (id: string | null) => {
    setCtxMenu(null);
    onInspect?.(id ? { kind: "node", id } : null);
  };
  const truncated = Object.entries(stats.truncated);
  const live = connState();
  const ctxNode = ctxMenu ? graph.nodes.get(ctxMenu.id) : null;
  const ctxKey = ctxNode ? ctxNode.id.slice(ctxNode.type.length + 1) : "";

  const cycleLabelMode = () => {
    const order: LabelMode[] = ["smart", "all", "off"];
    const next = order[(order.indexOf(forces.labelMode) + 1) % order.length];
    onChangeForces({ ...forces, labelMode: next });
  };

  return (
    <div className="graph-view" ref={wrap}>
      <div className="graph-stage">
        <GalaxyCanvas
          ref={canvasRef}
          hidden={hidden}
          query={query}
          selectedId={selectedId}
          forces={forces}
          onSelect={select}
          onContextMenu={(id, x, y) => {
            const rect = wrap.current?.getBoundingClientRect();
            const px = Math.min((rect?.width || 800) - 210, Math.max(12, x - (rect?.left || 0)));
            const py = Math.min((rect?.height || 600) - 220, Math.max(54, y - (rect?.top || 0)));
            setCtxMenu({ id, x: px, y: py });
          }}
        />

        {/* Floating Minimal HUD Canvas Toolbar */}
        <div className="graph-float-bar" role="toolbar" aria-label="Knowledge graph controls">
          <div className="row wrap">
            <button className="ghost" onClick={() => canvasRef.current?.fit()} title="Fit entire knowledge graph">Fit</button>
            <button className="ghost" onClick={() => canvasRef.current?.reset()} title="Recompute force layout">Reset</button>
            <button
              className="ghost"
              aria-pressed={forces.frozen}
              onClick={() => onChangeForces({ ...forces, frozen: !forces.frozen })}
              title="Freeze or unfreeze simulation"
            >
              {forces.frozen ? "Unfreeze" : "Freeze"}
            </button>
            <button className="ghost" onClick={cycleLabelMode} title="Toggle node label visibility">
              Labels: {forces.labelMode}
            </button>
            <select
              aria-label="Graph layout mode"
              style={{ width: "auto", padding: "4px 8px", fontSize: 12 }}
              value={forces.layoutMode}
              onChange={(e) => onChangeForces({ ...forces, layoutMode: e.target.value as LayoutMode })}
            >
              <option value="force">Force</option>
              <option value="cluster">Cluster</option>
              <option value="radial">Radial</option>
            </select>
            <button className="ghost" aria-pressed={filtersOpen} onClick={() => setFiltersOpen((o) => !o)}>
              Filters {hidden.size ? `(${hidden.size} hidden)` : ""}
            </button>
            <button className="ghost" onClick={resync} title="Resync snapshot from backend">Resync</button>
            <button className="ghost" onClick={() => (full ? document.exitFullscreen() : wrap.current?.requestFullscreen())}>
              {full ? "Exit Full" : "Full"}
            </button>
          </div>
          <div className="row mono dim" style={{ fontSize: 11 }}>
            <span className={`status ${live === "online" ? "ok" : live === "offline" ? "danger" : "warn"}`}>
              <i className="dot" /> {live === "online" ? "live" : live}
            </span>
            <span>seq #{stats.seq}</span>
            {stats.latencyMs !== null && <span>{stats.latencyMs}ms</span>}
          </div>
        </div>

        {filtersOpen && (
          <div className="graph-filters-popover card" role="group" aria-label="Filter graph node types">
            <div className="row between" style={{ marginBottom: 8 }}>
              <span className="eyebrow">Entity Types ({Object.keys(counts).length})</span>
              <div className="row">
                <button className="ghost" onClick={() => setHidden(new Set())}>Show all</button>
                <button className="ghost icon" onClick={() => setFiltersOpen(false)}>✕</button>
              </div>
            </div>
            <div className="row wrap chips">
              {Object.keys(counts).sort().map((t) => (
                <button
                  key={t}
                  className={`chip ${hidden.has(t) ? "off" : ""}`}
                  aria-pressed={!hidden.has(t)}
                  onClick={() =>
                    setHidden((h) => {
                      const n = new Set(h);
                      if (!n.delete(t)) n.add(t);
                      return n;
                    })
                  }
                >
                  <i className="dot" style={{ background: styleOf(t).color }} /> {styleOf(t).label} <b>{counts[t]}</b>
                </button>
              ))}
            </div>
          </div>
        )}

        {ctxNode && ctxMenu && (
          <div
            className="graph-context-menu card"
            style={{ left: ctxMenu.x, top: ctxMenu.y }}
            role="menu"
            aria-label={`Actions for ${ctxNode.label}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="row" style={{ marginBottom: 6 }}>
              <i className="dot" style={{ background: styleOf(ctxNode.type).color }} />
              <strong style={{ fontSize: 12 }}>{ctxNode.label}</strong>
            </div>
            <button className="link" onClick={() => { canvasRef.current?.focus(ctxNode.id); setCtxMenu(null); }}>Focus camera</button>
            <button className="link" onClick={() => { onInspect?.({ kind: "node", id: ctxNode.id }); setCtxMenu(null); }}>Inspect details</button>
            <button
              className="link"
              onClick={() => {
                const ids = neighbors(ctxNode.id).map((x) => x.node.id);
                highlightNodes([ctxNode.id, ...ids], 6500);
                setCtxMenu(null);
              }}
            >
              Highlight neighbors ({neighbors(ctxNode.id).length})
            </button>
            <button className="link" onClick={() => { canvasRef.current?.unpin(ctxNode.id); setCtxMenu(null); }}>Unpin position</button>
            {ctxNode.type === "project" && (
              <button
                className="link"
                onClick={async () => {
                  setCtxMenu(null);
                  try {
                    if (projectExpanded(ctxKey)) collapseProject(ctxKey);
                    else await expandProject(ctxKey);
                  } catch (e) {
                    onNotify((e as Error).message);
                  }
                }}
              >
                {projectExpanded(ctxKey) ? "Hide code graph" : "Load code graph"}
              </button>
            )}
            {ctxNode.type === "task" && ["running", "awaiting_approval"].includes(String(ctxNode.state)) && (
              <button
                className="link err-text"
                onClick={() => {
                  setCtxMenu(null);
                  apiFetch(`/v2/workforce/runs/${ctxKey}/cancel`, { method: "POST" }).catch((e) => onNotify((e as Error).message));
                }}
              >
                Cancel active run
              </button>
            )}
            {ctxNode.type === "memory" && (
              <button
                className="link err-text"
                onClick={() => {
                  setCtxMenu(null);
                  const path = ctxNode.store === "five_way" ? `/v2/memory/personal/${ctxKey}` : `/memory/items/${ctxKey}`;
                  apiFetch(path, { method: "DELETE" }).catch((e) => onNotify((e as Error).message));
                }}
              >
                Delete memory
              </button>
            )}
          </div>
        )}

        {!stats.loaded && !stats.error && <div className="stage-msg">Loading real knowledge graph…</div>}
        {stats.error && (
          <div className="stage-msg">
            <p>Could not load the graph: {stats.error}</p>
            <button onClick={resync}>Retry</button>
          </div>
        )}
        {stats.loaded && !graph.nodes.size && (
          <div className="stage-msg">Knowledge graph ready. Speak or ask Jarvis below to grow nodes and links live.</div>
        )}
      </div>

      <div className="graph-status-hud mono">
        <span>{graph.nodes.size} nodes · {graph.links.size} edges</span>
        <span>{stats.events} deltas</span>
        {stats.lastEventType && <span>last: {stats.lastEventType}</span>}
        {truncated.length > 0 && (
          <span title="Capped per-type limit">
            capped: {truncated.map(([t, n]) => `${t}:${n}`).join(", ")}
          </span>
        )}
        <span className="dim">click: inspect · dbl-click: focus · drag: pin · right-click: menu</span>
      </div>
    </div>
  );
};
