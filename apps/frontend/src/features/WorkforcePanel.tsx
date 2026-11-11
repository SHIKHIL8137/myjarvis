import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { GraphLink, GraphNode } from "../components/GalaxyCanvas";

interface Props {
  onNotify: (msg: string) => void;
  onShowProjectGraph: (name: string, nodes: GraphNode[], links: GraphLink[]) => void;
}

export const WorkforcePanel: React.FC<Props> = ({ onNotify, onShowProjectGraph }) => {
  const [projects, setProjects] = useState<Array<{ id: string; name: string; root_path: string; graphify_status: string }>>([]);
  const [runs, setRuns] = useState<Array<{ id: string; workflow_name: string; state: string; input_summary: string; output_summary: string }>>([]);
  const [approvals, setApprovals] = useState<Array<{ token: string; action_type: string; risk_tier: string; target_resource: string; preview_diff: string }>>([]);
  const [pName, setPName] = useState("");
  const [pPath, setPPath] = useState("");
  const [selProj, setSelProj] = useState("");
  const [taskPrompt, setTaskPrompt] = useState("");

  const refresh = async () => {
    try {
      const [p, r, a] = await Promise.all([
        apiFetch<{ projects: typeof projects }>("/v2/projects"),
        apiFetch<{ runs: typeof runs }>("/v2/workforce/runs"),
        apiFetch<{ pending: typeof approvals }>("/v2/approvals"),
      ]);
      setProjects(p.projects || []);
      if (!selProj && p.projects?.length) setSelProj(p.projects[0].id);
      setRuns(r.runs || []);
      setApprovals(a.pending || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const registerProj = async () => {
    if (!pName.trim() || !pPath.trim()) return;
    try {
      await apiFetch("/v2/projects", { method: "POST", body: { name: pName.trim(), root_path: pPath.trim() } });
      setPName("");
      setPPath("");
      refresh();
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const viewGraph = async (pid: string, name: string) => {
    try {
      const g = await apiFetch<{ nodes: GraphNode[]; links: GraphLink[]; node_count: number }>(`/v2/projects/${pid}/graph`);
      if (!g.nodes?.length) {
        onNotify("Syncing Graphify AST first…");
        await apiFetch(`/v2/projects/${pid}/graphify/sync`, { method: "POST" });
        const g2 = await apiFetch<{ nodes: GraphNode[]; links: GraphLink[] }>(`/v2/projects/${pid}/graph`);
        onShowProjectGraph(name, g2.nodes || [], g2.links || []);
      } else {
        onShowProjectGraph(name, g.nodes, g.links || []);
      }
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>PROJECT REGISTRY & GRAPHIFY</h3>
        <div className="grid-2" style={{ marginBottom: 8 }}>
          <input value={pName} onChange={(e) => setPName(e.target.value)} placeholder="Project name (e.g. myjarvis)" />
          <input value={pPath} onChange={(e) => setPPath(e.target.value)} placeholder="Local repository path" />
        </div>
        <button onClick={registerProj}>Register Project</button>
        <div style={{ marginTop: 10 }}>
          {projects.map((p) => (
            <div key={p.id} className="row" style={{ justifyContent: "space-between", padding: "5px 0" }}>
              <div>
                <strong>{p.name}</strong> <span className="dim">({p.graphify_status})</span>
                <div className="dim">{p.root_path}</div>
              </div>
              <div className="row">
                <button className="ghost" onClick={async () => { await apiFetch(`/v2/projects/${p.id}/graphify/sync`, { method: "POST" }); refresh(); }}>Sync Graph</button>
                <button onClick={() => viewGraph(p.id, p.name)}>3D Graph</button>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>SUPERVISED WORKFORCE RUN (WORKTREE + OPENCODE + QA)</h3>
        <select value={selProj} onChange={(e) => setSelProj(e.target.value)} style={{ marginBottom: 8 }}>
          <option value="">Select registered project…</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <div className="row">
          <input value={taskPrompt} onChange={(e) => setTaskPrompt(e.target.value)} placeholder="Describe coding task cleanly…" />
          <button
            onClick={async () => {
              if (!selProj || !taskPrompt.trim()) return;
              try {
                await apiFetch("/v2/workforce/runs", { method: "POST", body: { project_id: selProj, task_prompt: taskPrompt.trim() } });
                setTaskPrompt("");
                refresh();
              } catch (e) {
                onNotify((e as Error).message);
              }
            }}
          >
            Launch Run
          </button>
        </div>
      </div>

      <div className="card">
        <h3>PENDING HUMAN APPROVALS (EXECUTE TIER)</h3>
        {approvals.map((a) => (
          <div key={a.token} style={{ marginBottom: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span><strong>{a.action_type}</strong> → {a.target_resource}</span>
              <span className="pill warn">{a.risk_tier}</span>
            </div>
            <pre className="diff">{a.preview_diff || "(no diff preview)"}</pre>
            <div className="row">
              <button className="ok" onClick={async () => { await apiFetch(`/api/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: true } }); refresh(); }}>Approve & Merge</button>
              <button className="danger" onClick={async () => { await apiFetch(`/api/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: false } }); refresh(); }}>Reject</button>
            </div>
          </div>
        ))}
        {!approvals.length && <p className="dim">No actions awaiting approval.</p>}
      </div>

      <div className="card">
        <h3>RECENT AGENT RUNS</h3>
        {runs.map((r) => (
          <div key={r.id} style={{ padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>[{r.workflow_name}] {r.input_summary.slice(0, 60)}</strong>
              <span className="pill ok">{r.state}</span>
            </div>
            {r.output_summary && <div className="dim">{r.output_summary}</div>}
          </div>
        ))}
      </div>
    </div>
  );
};

