import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { GraphLink, GraphNode } from "../components/GalaxyCanvas";

interface Props {
  onNotify: (msg: string) => void;
  onShowProjectGraph: (name: string, nodes: GraphNode[], links: GraphLink[]) => void;
}

interface ApprovalItem {
  token: string;
  action_type: string;
  risk_tier: string;
  target_resource: string;
  preview_diff: string;
  args?: {
    project_name?: string;
    task_description?: string;
    changed_files?: Array<{ path: string; change_type: string; security_result: string }>;
    manifest_sha256?: string;
    qa_passed?: boolean;
    security_clean?: boolean;
    test_commands?: string;
    test_results?: { returncode?: number; stdout_tail?: string };
    warnings?: string[];
    commit_sha?: string;
  };
}

export const WorkforcePanel: React.FC<Props> = ({ onNotify, onShowProjectGraph }) => {
  const [projects, setProjects] = useState<Array<{ id: string; name: string; root_path: string; graphify_status: string; privacy_level?: string }>>([]);
  const [runs, setRuns] = useState<Array<{ id: string; workflow_name: string; state: string; input_summary: string; output_summary: string }>>([]);
  const [worktrees, setWorktrees] = useState<Array<{ id: string; project_id: string; run_id: string; branch_name: string; worktree_path: string; status: string }>>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [pName, setPName] = useState("");
  const [pPath, setPPath] = useState("");
  const [selProj, setSelProj] = useState("");
  const [taskPrompt, setTaskPrompt] = useState("");

  const refresh = async () => {
    try {
      const [p, r, a, wt] = await Promise.all([
        apiFetch<{ projects: typeof projects }>("/v2/projects"),
        apiFetch<{ runs: typeof runs }>("/v2/workforce/runs"),
        apiFetch<{ pending: typeof approvals }>("/v2/approvals"),
        apiFetch<{ worktrees: typeof worktrees }>("/v2/workforce/worktrees"),
      ]);
      setProjects(p.projects || []);
      if (!selProj && p.projects?.length) setSelProj(p.projects[0].id);
      setRuns(r.runs || []);
      setApprovals(a.pending || []);
      setWorktrees(wt.worktrees || []);
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
                <strong>{p.name}</strong> <span className="pill">{p.graphify_status}</span>
                {p.privacy_level && <span className="pill" style={{ marginLeft: 4 }}>{p.privacy_level}</span>}
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
        <h3>PENDING OWNER MERGE APPROVALS</h3>
        {approvals.map((a) => {
          const d = a.args || {};
          return (
            <div key={a.token} style={{ marginBottom: 12, borderBottom: "1px solid var(--border)", paddingBottom: 10 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span><strong>{d.project_name || a.action_type}</strong> → {a.target_resource}</span>
                <div className="row">
                  <span className={`pill ${d.qa_passed ? "ok" : "danger"}`}>QA: {d.qa_passed ? "PASS" : "FAIL"}</span>
                  <span className={`pill ${d.security_clean ? "ok" : "danger"}`}>SEC: {d.security_clean ? "CLEAN" : "BLOCKED"}</span>
                  <span className="pill warn">{a.risk_tier}</span>
                </div>
              </div>
              {d.task_description && <div className="dim" style={{ margin: "4px 0" }}>Task: {d.task_description}</div>}
              {d.test_commands && <div className="dim">QA Command: <code>{d.test_commands}</code> (rc={d.test_results?.returncode ?? 0})</div>}
              {d.manifest_sha256 && <div className="dim">Manifest SHA-256: <code>{d.manifest_sha256.slice(0, 16)}…</code></div>}
              {d.changed_files && d.changed_files.length > 0 && (
                <div className="dim" style={{ margin: "4px 0" }}>
                  Files: {d.changed_files.map((f) => `${f.path} [${f.change_type}/${f.security_result}]`).join(", ")}
                </div>
              )}
              {d.warnings && d.warnings.length > 0 && (
                <div className="pill warn" style={{ marginBottom: 6 }}>Warnings: {d.warnings.join("; ")}</div>
              )}
              <pre className="diff">{a.preview_diff || "(no diff preview)"}</pre>
              <div className="row">
                <button className="ok" onClick={async () => { try { await apiFetch(`/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: true } }); refresh(); } catch (e) { onNotify((e as Error).message); } }}>Approve & Merge</button>
                <button className="danger" onClick={async () => { try { await apiFetch(`/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: false } }); refresh(); } catch (e) { onNotify((e as Error).message); } }}>Reject</button>
              </div>
            </div>
          );
        })}
        {!approvals.length && <p className="dim">No actions awaiting approval.</p>}
      </div>

      <div className="card">
        <h3>ISOLATED GIT WORKTREES</h3>
        {worktrees.slice(0, 8).map((w) => (
          <div key={w.id} className="row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
            <div>
              <strong>{w.branch_name}</strong> <span className="dim">({w.run_id})</span>
              <div className="dim">{w.worktree_path}</div>
            </div>
            <span className={`pill ${w.status === "merged" ? "ok" : w.status.includes("fail") || w.status.includes("block") ? "danger" : "warn"}`}>{w.status}</span>
          </div>
        ))}
        {!worktrees.length && <p className="dim">No recorded worktrees.</p>}
      </div>

      <div className="card">
        <h3>AGENT WORKFLOW RUNS</h3>
        {runs.map((r) => (
          <div key={r.id} style={{ padding: "5px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>[{r.workflow_name}] {r.input_summary.slice(0, 60)}</strong>
              <div className="row">
                <span className={`pill ${r.state === "completed" ? "ok" : r.state.includes("fail") || r.state.includes("block") ? "danger" : "warn"}`}>{r.state}</span>
                {r.state === "awaiting_approval" && (
                  <button className="ghost" onClick={async () => { await apiFetch(`/v2/workforce/runs/${r.id}/cancel`, { method: "POST" }); refresh(); }}>Cancel</button>
                )}
              </div>
            </div>
            {r.output_summary && <div className="dim">{r.output_summary}</div>}
          </div>
        ))}
      </div>
    </div>
  );
};
