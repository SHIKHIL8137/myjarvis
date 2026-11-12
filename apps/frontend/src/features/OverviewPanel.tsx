import React, { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { styleOf } from "../components/GalaxyCanvas";
import { graph, useGraphVersion } from "../graphStore";
import { fmtTime, runStatus, type ShellProps } from "../shell";

/** Shapes of the two existing status endpoints the shell polls (only the fields the UI reads). */
export interface SystemOverview {
  db: { up: boolean; dialect: string; pgvector: boolean };
  memory: { personal_memories: number; experience_lessons: number; vector_backend: string; embedding_model: string };
  gateway: { total_calls?: number; tokens_24h?: number; daily_budget?: number; breakers?: Record<string, string> };
  projects: Array<{ id: string; name: string }>;
  runs: Array<{ id: string; workflow_name: string; agent_role: string; project_id?: string; state: string; input_summary: string; current_step?: string; started_at: number }>;
  pending_approvals: Array<{ token: string; action_type: string; risk_tier: string; target_resource: string }>;
  skills: { installed: number; enabled: number };
}
export interface SystemStatus {
  ollama: { up: boolean; version?: string; loaded?: string[] };
  brain: { model: string; provider: string };
  voice: { state: string; audio_state: string; error?: string | null };
  ready: { tts: boolean; stt: boolean; wake: boolean };
  system: { ram: { total_gb: number; free_gb: number }; gpu: { name: string; total_gb: number; used_gb: number; util_pct: number } | null; cpu_pct: number | null };
  mode: string;
}

const Meter: React.FC<{ label: string; value: number; max: number; text: string }> = ({ label, value, max, text }) => (
  <div className="meter">
    <div className="row between"><span>{label}</span><span className="mono">{text}</span></div>
    <div className="bar" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <i style={{ width: `${Math.min(100, (100 * value) / max)}%` }} />
    </div>
  </div>
);

export const OverviewPanel: React.FC<ShellProps & { overview: SystemOverview | null; status: SystemStatus | null }> = ({ onNotify, onNavigate, onInspect, overview, status }) => {
  const version = useGraphVersion();
  const [ask, setAsk] = useState("");
  const [recent, setRecent] = useState<Array<{ role: string; content: string; t?: number }>>([]);
  const [caps, setCaps] = useState<Array<{ provider: string; model_id: string; local: boolean }>>([]);

  useEffect(() => {
    apiFetch<{ messages: typeof recent }>("/history").then((h) => setRecent(h.messages.filter((m) => m.role === "user").slice(-4).reverse())).catch((e) => onNotify((e as Error).message));
    apiFetch<{ capabilities: typeof caps }>("/v2/gateway/capabilities").then((c) => setCaps(c.capabilities || [])).catch(() => {});
  }, [onNotify]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of graph.nodes.values()) c[n.type] = (c[n.type] || 0) + 1;
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  }, [version]);
  const memories = useMemo(
    () => [...graph.nodes.values()].filter((n) => n.type === "memory" || n.type === "experience").sort((a, b) => Number(b.updated_at || b.created_at || 0) - Number(a.updated_at || a.created_at || 0)).slice(0, 5),
    [version]
  );
  const runs = overview?.runs || [];
  const active = runs.filter((r) => ["running", "awaiting_approval", "cancelling"].includes(r.state));
  const projectName = (id?: string) => overview?.projects.find((p) => p.id === id)?.name;
  const sys = status?.system;
  const maxCount = counts[0]?.[1] || 1;

  return (
    <div className="scroll-body overview">
      <form className="card ask" onSubmit={(e) => { e.preventDefault(); if (ask.trim()) onNavigate?.("chat", ask.trim()); }}>
        <h3>Ask Jarvis</h3>
        <div className="row">
          <input value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="Ask a question or give a command…" aria-label="Ask Jarvis" />
          <button type="submit" disabled={!ask.trim()}>Send</button>
        </div>
      </form>

      <div className="tiles">
        <section className="card">
          <h3>Recent conversation</h3>
          {!recent.length && <p className="empty">No messages yet.</p>}
          {recent.map((m, i) => <div key={i} className="line"><span>{m.content.slice(0, 90)}</span><span className="mono dim">{fmtTime(m.t)}</span></div>)}
          <button className="ghost" onClick={() => onNavigate?.("chat")}>Open chat</button>
        </section>

        <section className="card">
          <h3>Active agents</h3>
          {!overview && <p className="empty">Loading…</p>}
          {overview && !active.length && <p className="empty">No agent is running right now.</p>}
          {active.map((r) => (
            <button key={r.id} className="line link" onClick={() => onInspect?.({ kind: "node", id: `task:${r.id}` })}>
              <span><strong>{r.agent_role}</strong> {projectName(r.project_id) && <span className="dim">on {projectName(r.project_id)}</span>}<br /><span className="dim">{r.input_summary.slice(0, 70)}</span></span>
              <span className={`pill ${runStatus(r.state).tone}`}>{r.current_step || runStatus(r.state).label}</span>
            </button>
          ))}
        </section>

        <section className="card">
          <h3>Current workflows</h3>
          {overview && !runs.length && <p className="empty">No workflow has run yet.</p>}
          {runs.slice(0, 5).map((r) => (
            <button key={r.id} className="line link" onClick={() => onInspect?.({ kind: "node", id: `task:${r.id}` })}>
              <span>[{r.workflow_name}] {r.input_summary.slice(0, 60)}</span>
              <span className={`pill ${runStatus(r.state).tone}`}>{runStatus(r.state).label}</span>
            </button>
          ))}
          <button className="ghost" onClick={() => onNavigate?.("workforce")}>Open workforce</button>
        </section>

        <section className="card">
          <h3>Pending approvals</h3>
          {overview && !overview.pending_approvals.length && <p className="empty">Nothing is waiting for your approval.</p>}
          {overview?.pending_approvals.map((a) => (
            <button key={a.token} className="line link" onClick={() => onNavigate?.("workforce")}>
              <span>{a.action_type} → {a.target_resource}</span><span className="pill warn">{a.risk_tier}</span>
            </button>
          ))}
        </section>

        <section className="card">
          <h3>Recent memory updates</h3>
          {!memories.length && <p className="empty">No memories stored yet.</p>}
          {memories.map((n) => (
            <button key={n.id} className="line link" onClick={() => onInspect?.({ kind: "node", id: n.id })}>
              <span>{n.label}</span><span className="mono dim">{fmtTime(Number(n.updated_at || n.created_at) || null)}</span>
            </button>
          ))}
          <button className="ghost" onClick={() => onNavigate?.("memory")}>Open memory</button>
        </section>

        <section className="card">
          <h3>Knowledge graph</h3>
          {!counts.length && <p className="empty">The graph is empty.</p>}
          {counts.map(([t, c]) => (
            <div key={t} className="count-row">
              <span><i className="dot" style={{ background: styleOf(t).color }} /> {styleOf(t).label}</span>
              <div className="bar"><i style={{ width: `${(100 * c) / maxCount}%`, background: styleOf(t).color }} /></div>
              <span className="mono">{c}</span>
            </div>
          ))}
          <div className="row between"><span className="dim">{graph.links.size} relationships</span><button className="ghost" onClick={() => onNavigate?.("graph")}>Open graph</button></div>
        </section>

        <section className="card">
          <h3>Models & providers</h3>
          {!status && <p className="empty">Loading…</p>}
          {status && (
            <>
              <div className="line"><span>Active model</span><span className="mono">{status.brain.model}</span></div>
              <div className="line"><span>Ollama {status.ollama.version || ""}</span><span className={`pill ${status.ollama.up ? "ok" : "danger"}`}>{status.ollama.up ? "online" : "offline"}</span></div>
              <div className="line"><span>Registered models</span><span className="mono">{caps.filter((c) => c.local).length} local · {caps.filter((c) => !c.local).length} cloud</span></div>
            </>
          )}
          {Object.entries(overview?.gateway.breakers || {}).map(([p, s]) => (
            <div key={p} className="line"><span>{p}</span><span className={`pill ${s === "CLOSED" ? "" : "warn"}`} title="Circuit breaker state">{s === "CLOSED" ? "ready" : s.toLowerCase()}</span></div>
          ))}
          <button className="ghost" onClick={() => onNavigate?.("gateway")}>Open gateway</button>
        </section>

        <section className="card">
          <h3>System resources</h3>
          {!sys && <p className="empty">Loading…</p>}
          {sys && (
            <>
              <Meter label="RAM" value={sys.ram.total_gb - sys.ram.free_gb} max={sys.ram.total_gb} text={`${(sys.ram.total_gb - sys.ram.free_gb).toFixed(1)} / ${sys.ram.total_gb} GB`} />
              {sys.cpu_pct !== null ? <Meter label="CPU" value={sys.cpu_pct} max={100} text={`${sys.cpu_pct}%`} /> : <div className="line"><span>CPU</span><span className="dim">measuring…</span></div>}
              {sys.gpu ? (
                <>
                  <Meter label={`VRAM · ${sys.gpu.name}`} value={sys.gpu.used_gb} max={sys.gpu.total_gb} text={`${sys.gpu.used_gb} / ${sys.gpu.total_gb} GB`} />
                  <Meter label="GPU load" value={sys.gpu.util_pct} max={100} text={`${sys.gpu.util_pct}%`} />
                </>
              ) : <div className="line"><span>GPU</span><span className="dim">not reported</span></div>}
            </>
          )}
        </section>
      </div>
    </div>
  );
};
