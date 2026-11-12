import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { expandProject } from "../graphStore";
import { onEvent } from "../live";
import { fmtTime, runStatus, type ShellProps } from "../shell";

interface Props extends ShellProps {
  section: "workforce" | "projects"; // the registry lives under Projects, everything about runs under Workforce
  initialProject?: string;
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
interface Run {
  id: string;
  workflow_name: string;
  agent_role: string;
  project_id?: string | null;
  state: string;
  input_summary: string;
  output_summary: string;
  started_at: number;
  ended_at?: number | null;
  model?: string | null;
  current_step?: string | null;
  last_activity?: number | null;
}

export const WorkforcePanel: React.FC<Props> = ({ onNotify, onInspect, onNavigate, section, initialProject }) => {
  const [projects, setProjects] = useState<Array<{ id: string; name: string; root_path: string; graphify_status: string; privacy_level?: string }>>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [worktrees, setWorktrees] = useState<Array<{ id: string; project_id: string; run_id: string; branch_name: string; worktree_path: string; status: string }>>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [pName, setPName] = useState("");
  const [pPath, setPPath] = useState("");
  const [selProj, setSelProj] = useState(initialProject || "");
  const [taskPrompt, setTaskPrompt] = useState("");
  const timer = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const [p, r, a, wt] = await Promise.all([
        apiFetch<{ projects: typeof projects }>("/v2/projects"),
        apiFetch<{ runs: Run[] }>("/v2/workforce/runs"),
        apiFetch<{ pending: ApprovalItem[] }>("/v2/approvals"),
        apiFetch<{ worktrees: typeof worktrees }>("/v2/workforce/worktrees"),
      ]);
      setProjects(p.projects || []);
      setSelProj((cur) => cur || p.projects?.[0]?.id || "");
      setRuns(r.runs || []);
      setApprovals(a.pending || []);
      setWorktrees(wt.worktrees || []);
      setLoaded(true);
    } catch (e) {
      onNotify((e as Error).message);
    }
  }, [onNotify]);

  useEffect(() => {
    refresh();
    // Live: every backend run event (started / LangGraph node finished / completed / failed) refreshes the lists. No polling.
    return onEvent((ev) => {
      if (ev.type === "agent_run_update" || (ev.type === "graph_event" && (ev.run_id || /^project\./.test(ev.event)))) {
        if (ev.run_id && ev.step) setRuns((rs) => rs.map((r) => (r.id === ev.run_id ? { ...r, current_step: ev.step, state: ev.state || r.state, last_activity: ev.ts } : r)));
        clearTimeout(timer.current);
        timer.current = window.setTimeout(refresh, 300);
      }
    });
  }, [refresh]);

  const act = (fn: () => Promise<unknown>, okMsg?: string) => async () => {
    try {
      await fn();
      if (okMsg) onNotify(okMsg);
    } catch (e) {
      onNotify((e as Error).message);
    }
    refresh();
  };
  const projectName = (id?: string | null) => projects.find((p) => p.id === id)?.name;

  if (section === "projects") {
    return (
      <div className="scroll-body">
        <div className="card">
          <h3>Register a project</h3>
          <div className="grid-2" style={{ marginBottom: 8 }}>
            <input value={pName} onChange={(e) => setPName(e.target.value)} placeholder="Project name (e.g. myjarvis)" aria-label="Project name" />
            <input value={pPath} onChange={(e) => setPPath(e.target.value)} placeholder="Local repository path" aria-label="Repository path" />
          </div>
          <button disabled={!pName.trim() || !pPath.trim()} onClick={act(async () => {
            await apiFetch("/v2/projects", { method: "POST", body: { name: pName.trim(), root_path: pPath.trim() } });
            setPName("");
            setPPath("");
          }, "Project registered.")}>Register project</button>
        </div>
        <div className="card">
          <h3>Project registry & Graphify ({projects.length})</h3>
          {loaded && !projects.length && <p className="empty">No project registered yet. Register a local repository above to index it and run agents on it.</p>}
          {projects.map((p) => (
            <div key={p.id} className="row between wrap" style={{ padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
              <button className="link" style={{ width: "auto", flex: 1 }} onClick={() => onInspect?.({ kind: "node", id: `project:${p.id}` })} title="Inspect project">
                <span>
                  <strong>{p.name}</strong> <span className="pill">{p.graphify_status}</span>
                  {p.privacy_level && <span className="pill" style={{ marginLeft: 4 }}>{p.privacy_level}</span>}
                  <div className="dim mono">{p.root_path}</div>
                </span>
              </button>
              <div className="row">
                <button className="ghost" onClick={act(() => apiFetch(`/v2/projects/${p.id}/graphify/sync`, { method: "POST" }), "Graph synced.")}>Sync graph</button>
                <button className="ghost" onClick={act(async () => { onNotify("Loading code graph…"); await expandProject(p.id); onNavigate?.("graph", `project:${p.id}`); })}>Code graph</button>
                <button onClick={() => onNavigate?.("workforce", p.id)}>New run</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>Supervised workforce run (worktree + OpenCode + QA)</h3>
        {loaded && !projects.length && <p className="empty">Register a project first. <button className="ghost" onClick={() => onNavigate?.("projects")}>Open Projects</button></p>}
        <select value={selProj} onChange={(e) => setSelProj(e.target.value)} style={{ marginBottom: 8 }} aria-label="Project">
          <option value="">Select registered project…</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <div className="row">
          <input value={taskPrompt} onChange={(e) => setTaskPrompt(e.target.value)} placeholder="Describe the coding task…" aria-label="Task" />
          <button
            disabled={!selProj || taskPrompt.trim().length < 3}
            onClick={() => {
              // The request stays open until the run pauses or ends; progress arrives over the event stream meanwhile.
              const body = { project_id: selProj, task_prompt: taskPrompt.trim() };
              setTaskPrompt("");
              apiFetch("/v2/workforce/runs", { method: "POST", body }).then(refresh, (e) => { onNotify((e as Error).message); refresh(); });
            }}
          >
            Launch run
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Agent runs ({runs.length})</h3>
        {loaded && !runs.length && <p className="empty">No agent has run yet.</p>}
        {runs.map((r) => {
          const st = runStatus(r.state);
          const failed = st.tone === "danger";
          return (
            <div key={r.id} style={{ padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
              <div className="row between wrap">
                <button className="link" style={{ width: "auto", flex: 1 }} title="Inspect run" onClick={() => onInspect?.({ kind: "detail", title: r.input_summary.slice(0, 80) || r.id, subtitle: `${r.workflow_name} run`, fields: { ...r, project: projectName(r.project_id), started_at: fmtTime(r.started_at), ended_at: fmtTime(r.ended_at), last_activity: fmtTime(r.last_activity) }, text: r.output_summary })}>
                  <span><strong>{r.agent_role}</strong> <span className="dim">· {r.workflow_name}{projectName(r.project_id) ? ` · ${projectName(r.project_id)}` : ""}</span><br />{r.input_summary.slice(0, 110)}</span>
                </button>
                <div className="row">
                  {r.current_step && <span className="pill info" title="Last finished LangGraph node">{r.current_step}</span>}
                  <span className={`pill ${st.tone}`}>{st.label}</span>
                  {["awaiting_approval", "running"].includes(r.state) && (
                    <button className="ghost" onClick={act(() => apiFetch(`/v2/workforce/runs/${r.id}/cancel`, { method: "POST" }), "Cancel requested.")}>Cancel</button>
                  )}
                </div>
              </div>
              <div className="dim mono">
                started {fmtTime(r.started_at)} · last activity {fmtTime(r.last_activity)}{r.model ? ` · model ${r.model}` : ""}
              </div>
              {r.output_summary && <div className={failed ? "err-text" : "dim"}>{failed ? "Error: " : "Result: "}{r.output_summary}</div>}
            </div>
          );
        })}
      </div>

      <div className="card">
        <h3>Pending owner merge approvals ({approvals.length})</h3>
        {approvals.map((a) => {
          const d = a.args || {};
          return (
            <div key={a.token} style={{ marginBottom: 12, borderBottom: "1px solid var(--border)", paddingBottom: 10 }}>
              <div className="row between wrap">
                <button className="link" style={{ width: "auto" }} title="Inspect approval" onClick={() => onInspect?.({ kind: "detail", title: d.project_name || a.action_type, subtitle: "Approval request", fields: { token: a.token, action_type: a.action_type, risk_tier: a.risk_tier, target: a.target_resource, ...d, changed_files: d.changed_files?.map((f) => f.path).join(", "), test_results: d.test_results?.returncode }, text: a.preview_diff })}>
                  <span><strong>{d.project_name || a.action_type}</strong> → {a.target_resource}</span>
                </button>
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
              {d.warnings && d.warnings.length > 0 && <div className="pill warn" style={{ marginBottom: 6 }}>Warnings: {d.warnings.join("; ")}</div>}
              <pre className="diff">{a.preview_diff || "(no diff preview)"}</pre>
              <div className="row">
                <button className="ok" onClick={act(() => apiFetch(`/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: true } }))}>Approve & merge</button>
                <button className="danger" onClick={act(() => apiFetch(`/v2/approvals/${a.token}/decide`, { method: "POST", body: { approved: false } }))}>Reject</button>
              </div>
            </div>
          );
        })}
        {loaded && !approvals.length && <p className="empty">No actions awaiting approval.</p>}
      </div>

      <div className="card">
        <h3>Isolated git worktrees</h3>
        {worktrees.slice(0, 8).map((w) => (
          <div key={w.id} className="row between" style={{ padding: "4px 0" }}>
            <div>
              <strong>{w.branch_name}</strong> <span className="dim">({w.run_id})</span>
              <div className="dim mono">{w.worktree_path}</div>
            </div>
            <span className={`pill ${w.status === "merged" ? "ok" : w.status.includes("fail") || w.status.includes("block") ? "danger" : "warn"}`}>{w.status}</span>
          </div>
        ))}
        {loaded && !worktrees.length && <p className="empty">No recorded worktrees.</p>}
      </div>
    </div>
  );
};
