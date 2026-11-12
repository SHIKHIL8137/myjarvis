import React, { useMemo, useState } from "react";
import { graph, graphTopHubs, stats, useGraphVersion } from "../graphStore";
import type { ConnState } from "../live";
import type { Inspectable, ShellProps } from "../shell";
import { DEFAULT_FORCES, styleOf, type EdgeMode, type ForceConfig, type LabelMode, type LayoutMode } from "./GalaxyCanvas";
import { NodeDetails, SystemEvents } from "./Inspector";

interface BrainExplorerProps extends ShellProps {
  open: boolean;
  onToggleOpen: () => void;
  selectedId: string | null;
  inspect: Inspectable | null;
  searchInput: string;
  onSearchInput: (q: string) => void;
  forces: ForceConfig;
  onChangeForces: (next: ForceConfig) => void;
  conn: ConnState;
  modeLabel: string;
  onFocusNode: (id: string) => void;
  onUnpinNode: (id: string) => void;
}

type Tab = "inspect" | "hubs" | "forces";

export const BrainExplorer: React.FC<BrainExplorerProps> = ({
  open,
  onToggleOpen,
  selectedId,
  inspect,
  searchInput,
  onSearchInput,
  forces,
  onChangeForces,
  conn,
  modeLabel,
  onFocusNode,
  onUnpinNode,
  ...shell
}) => {
  const version = useGraphVersion();
  const [tab, setTab] = useState<Tab>("inspect");

  const hits = useMemo(() => {
    const q = searchInput.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const n of graph.nodes.values()) {
      const hay = `${n.label} ${n.type} ${n.category || ""} ${n.source || ""}`.toLowerCase();
      if (hay.includes(q)) out.push(n);
      if (out.length >= 10) break;
    }
    return out;
  }, [searchInput, version]);

  const hubs = useMemo(() => graphTopHubs(10), [version]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of graph.nodes.values()) c[n.type] = (c[n.type] || 0) + 1;
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  }, [version]);
  const maxCount = counts[0]?.[1] || 1;

  if (!open) {
    return (
      <aside className="hud-rail left collapsed" aria-label="Brain Explorer collapsed">
        <button className="ghost icon" onClick={onToggleOpen} title="Expand Brain Explorer" aria-label="Expand Brain Explorer">
          ›
        </button>
        <span className="rail-vertical mono dim">BRAIN EXPLORER</span>
      </aside>
    );
  }

  return (
    <aside className="hud-rail left" aria-label="Brain Explorer">
      <div className="rail-head row between">
        <div>
          <div className="eyebrow">Brain Explorer</div>
          <div className="row" style={{ gap: 6, marginTop: 2 }}>
            <span className={`status ${conn === "online" ? "ok" : conn === "offline" ? "danger" : "warn"}`}>
              <i className="dot" /> {conn}
            </span>
            <span className="pill">{modeLabel}</span>
          </div>
        </div>
        <button className="ghost icon" onClick={onToggleOpen} title="Collapse Brain Explorer" aria-label="Collapse Brain Explorer">
          ‹
        </button>
      </div>

      <div className="kpi-grid">
        <div className="kpi-tile">
          <span className="mono">{graph.nodes.size}</span>
          <small className="eyebrow">Nodes</small>
        </div>
        <div className="kpi-tile">
          <span className="mono">{graph.links.size}</span>
          <small className="eyebrow">Links</small>
        </div>
        <div className="kpi-tile">
          <span className="mono">#{stats.seq}</span>
          <small className="eyebrow">Seq</small>
        </div>
      </div>

      <div className="brain-search">
        <input
          type="search"
          value={searchInput}
          onChange={(e) => onSearchInput(e.target.value)}
          placeholder="Search brain nodes…"
          aria-label="Search brain nodes"
        />
        {hits.length > 0 && (
          <ul className="search-hits-inline" aria-label="Matching brain nodes">
            {hits.map((n) => (
              <li key={n.id}>
                <button
                  className="link"
                  onClick={() => {
                    shell.onInspect?.({ kind: "node", id: n.id });
                    onFocusNode(n.id);
                    setTab("inspect");
                  }}
                >
                  <i className="dot" style={{ background: styleOf(n.type).color }} />
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{n.label}</span>
                  <span className="mono dim">{n.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="tab-strip" role="tablist" aria-label="Brain Explorer Sections">
        <button role="tab" aria-selected={tab === "inspect"} className={tab === "inspect" ? "active" : "ghost"} onClick={() => setTab("inspect")}>
          {selectedId ? "Node" : "Events"}
        </button>
        <button role="tab" aria-selected={tab === "hubs"} className={tab === "hubs" ? "active" : "ghost"} onClick={() => setTab("hubs")}>
          Top Hubs
        </button>
        <button role="tab" aria-selected={tab === "forces"} className={tab === "forces" ? "active" : "ghost"} onClick={() => setTab("forces")}>
          Forces
        </button>
      </div>

      <div className="rail-body">
        {tab === "inspect" && (
          <>
            {selectedId && (
              <div className="row between" style={{ marginBottom: 8 }}>
                <span className="eyebrow">Selected Entity</span>
                <button className="ghost" onClick={() => shell.onInspect?.(null)}>Live Events</button>
              </div>
            )}
            {inspect?.kind === "node" ? (
              <NodeDetails id={inspect.id} onFocusNode={onFocusNode} onUnpinNode={onUnpinNode} {...shell} />
            ) : (
              <SystemEvents />
            )}
          </>
        )}

        {tab === "hubs" && (
          <div className="hubs-view">
            <h3>Most Connected Hubs</h3>
            {!hubs.length && <p className="empty">No connected hubs yet.</p>}
            <ul className="rel-list">
              {hubs.map(({ node, degree }) => (
                <li key={node.id}>
                  <button
                    className={`link ${selectedId === node.id ? "active-hit" : ""}`}
                    onClick={() => {
                      shell.onInspect?.({ kind: "node", id: node.id });
                      onFocusNode(node.id);
                      setTab("inspect");
                    }}
                  >
                    <i className="dot" style={{ background: styleOf(node.type).color }} />
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{node.label}</span>
                    <span className="pill">{degree}</span>
                  </button>
                </li>
              ))}
            </ul>
            <h3 style={{ marginTop: 14 }}>Entity Breakdown</h3>
            {counts.map(([type, count]) => (
              <div key={type} className="count-row">
                <span className="row" style={{ gap: 6 }}>
                  <i className="dot" style={{ background: styleOf(type).color }} />
                  {styleOf(type).label}
                </span>
                <div className="bar"><i style={{ width: `${(100 * count) / maxCount}%`, background: styleOf(type).color }} /></div>
                <span className="mono">{count}</span>
              </div>
            ))}
          </div>
        )}

        {tab === "forces" && (
          <div className="forces-view">
            <h3>Physics & Visual Controls</h3>

            <label className="field">
              <div className="row between"><span>Repulsion</span><span className="mono">{forces.repulsion}</span></div>
              <input
                type="range"
                min={-380}
                max={-20}
                step={5}
                value={forces.repulsion}
                onChange={(e) => onChangeForces({ ...forces, repulsion: Number(e.target.value) })}
              />
            </label>

            <label className="field">
              <div className="row between"><span>Link Distance</span><span className="mono">{forces.linkDistance}</span></div>
              <input
                type="range"
                min={25}
                max={220}
                step={5}
                value={forces.linkDistance}
                onChange={(e) => onChangeForces({ ...forces, linkDistance: Number(e.target.value) })}
              />
            </label>

            <label className="field">
              <div className="row between"><span>Node Size</span><span className="mono">{forces.nodeScale.toFixed(1)}x</span></div>
              <input
                type="range"
                min={0.5}
                max={2.2}
                step={0.1}
                value={forces.nodeScale}
                onChange={(e) => onChangeForces({ ...forces, nodeScale: Number(e.target.value) })}
              />
            </label>

            <label className="field">
              <div className="row between"><span>Animation Speed</span><span className="mono">{forces.speed.toFixed(2)}x</span></div>
              <input
                type="range"
                min={0.25}
                max={2.0}
                step={0.05}
                value={forces.speed}
                onChange={(e) => onChangeForces({ ...forces, speed: Number(e.target.value) })}
              />
            </label>

            <label className="field">
              <span>Layout Mode</span>
              <select value={forces.layoutMode} onChange={(e) => onChangeForces({ ...forces, layoutMode: e.target.value as LayoutMode })}>
                <option value="force">3D Force Directed</option>
                <option value="cluster">Entity Cluster Constellations</option>
                <option value="radial">Radial Hierarchy Orbit</option>
              </select>
            </label>

            <label className="field">
              <span>Labels Visibility</span>
              <select value={forces.labelMode} onChange={(e) => onChangeForces({ ...forces, labelMode: e.target.value as LabelMode })}>
                <option value="smart">Smart (Selected & Hubs)</option>
                <option value="all">All Visible Nodes</option>
                <option value="off">Off (Hover Only)</option>
              </select>
            </label>

            <label className="field">
              <span>Edge Visibility</span>
              <select value={forces.edgeMode} onChange={(e) => onChangeForces({ ...forces, edgeMode: e.target.value as EdgeMode })}>
                <option value="all">All Relationships</option>
                <option value="highlighted">Selected & Active Only</option>
                <option value="hidden">Hide Edges</option>
              </select>
            </label>

            <div className="row wrap" style={{ marginTop: 12 }}>
              <button
                className="ghost"
                aria-pressed={forces.clustering}
                onClick={() => onChangeForces({ ...forces, clustering: !forces.clustering })}
              >
                Cluster {forces.clustering ? "On" : "Off"}
              </button>
              <button
                className="ghost"
                aria-pressed={forces.frozen}
                onClick={() => onChangeForces({ ...forces, frozen: !forces.frozen })}
              >
                {forces.frozen ? "Unfreeze" : "Freeze"}
              </button>
              <button className="ghost" onClick={() => onChangeForces(DEFAULT_FORCES)}>Reset</button>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
};

