import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { SystemOverview, SystemStatus } from "../features/OverviewPanel";
import type { ConnState } from "../live";
import { runStatus, type ShellProps } from "../shell";
import { HologramCore, type CoreMode } from "./HologramCore";

const CORE_MODE_KEY = "jarvis.core.mode";

type Health = "online" | "offline" | "degraded" | "unknown";
const TONE: Record<Health, string> = { online: "ok", offline: "danger", degraded: "warn", unknown: "" };

export function computeHealthRows(
  st: SystemStatus | null,
  ov: SystemOverview | null,
  backendDown: boolean,
  live: ConnState
): Array<[string, Health, string]> {
  const breakers = Object.entries(ov?.gateway.breakers || {});
  const tripped = breakers.filter(([, s]) => s !== "CLOSED").map(([p]) => p);
  const speech = st ? [st.ready.tts, st.ready.stt].filter(Boolean).length : 0;
  return [
    ["Backend", backendDown ? "offline" : st ? "online" : "unknown", "GET /api/status"],
    ["Database", backendDown ? "offline" : ov ? (ov.db.up ? "online" : "offline") : "unknown", ov ? ov.db.dialect : "pending"],
    ["Memory", backendDown ? "offline" : ov ? (typeof ov.memory.personal_memories === "number" ? "online" : "degraded") : "unknown", ov ? `${ov.memory.personal_memories} mem` : "pending"],
    ["Vectors", backendDown ? "offline" : ov ? (!ov.db.up ? "offline" : ov.db.dialect.startsWith("postgres") && !ov.db.pgvector ? "degraded" : "online") : "unknown", ov ? ov.memory.vector_backend : "pending"],
    ["Ollama", backendDown ? "unknown" : st ? (st.ollama.up ? "online" : "offline") : "unknown", st?.ollama.up ? st.brain.model : "unreachable"],
    ["Gateway", backendDown ? "offline" : !breakers.length ? "unknown" : tripped.length ? "degraded" : "online", tripped.length ? tripped.join(",") : `${breakers.length} ok`],
    ["Workforce", backendDown ? "offline" : ov ? "online" : "unknown", ov ? `${ov.runs.length} runs` : "pending"],
    ["Voice", backendDown ? "offline" : !st ? "unknown" : st.voice.error ? "degraded" : speech === 2 ? "online" : speech === 1 ? "degraded" : "offline", st ? `TTS:${st.ready.tts ? "ok" : "no"} STT:${st.ready.stt ? "ok" : "no"}` : "pending"],
    ["SSE", live === "online" ? "online" : live === "offline" ? "offline" : "degraded", live],
  ];
}

interface JarvisCorePanelProps extends ShellProps {
  open: boolean;
  onToggleOpen: () => void;
  assistantState: string;
  stateDetail: string;
  audioLevel: number;
  status: SystemStatus | null;
  overview: SystemOverview | null;
  backendDown: boolean;
  live: ConnState;
  onOpenDrawer?: (drawer: "approvals" | "workforce" | "memory" | "system" | "learning" | "voice") => void;
}

export const JarvisCorePanel: React.FC<JarvisCorePanelProps> = ({
  open,
  onToggleOpen,
  assistantState,
  stateDetail,
  audioLevel,
  status,
  overview,
  backendDown,
  live,
  onNotify,
  onNavigate,
  onInspect,
  onOpenDrawer,
}) => {
  const [mode, setMode] = useState<CoreMode>(() => {
    const saved = localStorage.getItem(CORE_MODE_KEY);
    return saved === "cube" || saved === "face" ? saved : "ring";
  });
  const [models, setModels] = useState<Array<{ key: string; model: string; provider?: string }>>([]);
  const [currentKey, setCurrentKey] = useState("");
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    localStorage.setItem(CORE_MODE_KEY, mode);
  }, [mode]);

  useEffect(() => {
    apiFetch<{ current: string; models: Array<{ key: string; model: string; provider?: string }> }>("/models")
      .then((m) => {
        setCurrentKey(m.current || "");
        setModels(m.models || []);
      })
      .catch(() => {});
  }, [status?.brain.model]);

  const rows = computeHealthRows(status, overview, backendDown, live);
  const activeRuns = (overview?.runs || []).filter((r) => ["running", "awaiting_approval", "cancelling"].includes(r.state));
  const pendingApprovals = overview?.pending_approvals || [];
  const sys = status?.system;
  const duplex = (status?.voice as unknown as { duplex?: { effective?: string } })?.duplex?.effective || "half_duplex";
  const voiceMode = status?.voice?.mode || "continuous";
  const wakeReady = Boolean(status?.ready?.wake);
  const voiceErr = status?.voice?.error;

  if (!open) {
    return (
      <aside className="hud-rail right collapsed" aria-label="Jarvis Core collapsed">
        <button className="ghost icon" onClick={onToggleOpen} title="Expand Jarvis Core" aria-label="Expand Jarvis Core">
          ‹
        </button>
        <span className="rail-vertical mono dim">JARVIS CORE · {assistantState.toUpperCase()}</span>
      </aside>
    );
  }

  return (
    <aside className="hud-rail right" aria-label="Jarvis Core & Telemetry">
      <div className="rail-head row between">
        <div>
          <div className="eyebrow">Jarvis Core</div>
          <div className="mono" style={{ fontSize: 11, color: "var(--accent)" }}>{assistantState}</div>
        </div>
        <button className="ghost icon" onClick={onToggleOpen} title="Collapse Jarvis Core" aria-label="Collapse Jarvis Core">
          ›
        </button>
      </div>

      <div className="core-mode-tabs row" role="group" aria-label="Hologram visualizer mode">
        {(["ring", "cube", "face"] as CoreMode[]).map((m) => (
          <button key={m} className={mode === m ? "active" : "ghost"} onClick={() => setMode(m)}>
            {m[0].toUpperCase() + m.slice(1)}
          </button>
        ))}
      </div>

      <div className="hologram-frame">
        <HologramCore mode={mode} assistantState={assistantState} audioLevel={audioLevel} />
        <div className="hologram-hud-label">
          <span className="mono">{assistantState}</span>
          {stateDetail && <small className="dim">{stateDetail}</small>}
        </div>
      </div>

      <div className="rail-body">
        <section className="hud-section">
          <h3>Active Brain & Voice</h3>
          <label className="field" style={{ marginTop: 4 }}>
            <span>Model ({status?.brain.provider || "ollama"})</span>
            <select
              value={currentKey}
              disabled={switching || !models.length}
              onChange={async (e) => {
                const target = e.target.value;
                setSwitching(true);
                try {
                  const res = await apiFetch<{ model: string }>("/models/switch", { method: "POST", body: { model: target } });
                  setCurrentKey(target);
                  onNotify(`Switched brain to ${res.model}`);
                } catch (err) {
                  onNotify((err as Error).message);
                } finally {
                  setSwitching(false);
                }
              }}
            >
              {models.map((m) => (
                <option key={m.key} value={m.key}>{m.key} ({m.model})</option>
              ))}
            </select>
          </label>
          <div className="row between dim mono" style={{ marginTop: 8 }}>
            <span>Voice Mode</span>
            <select
              style={{ width: "auto", padding: "2px 6px", fontSize: 11 }}
              value={voiceMode}
              onChange={async (e) => {
                try {
                  await apiFetch("/voice/session", { method: "POST", body: { action: "mode", mode: e.target.value } });
                  onNotify(`Voice mode: ${e.target.value}`);
                } catch (err) {
                  onNotify((err as Error).message);
                }
              }}
              aria-label="Voice Loop Mode"
            >
              <option value="continuous">Continuous</option>
              <option value="ptt">PTT</option>
              <option value="wake_word" disabled={!wakeReady}>{wakeReady ? "Wake Word" : "Wake Word (Unavailable)"}</option>
            </select>
          </div>
          {!wakeReady && (
            <div className="dim mono" style={{ marginTop: 4, fontSize: 10.5 }}>
              Wake-word detection offline (model not loaded)
            </div>
          )}
          <div className="row between dim mono" style={{ marginTop: 4 }}>
            <span>Audio Policy</span>
            <span>{duplex}</span>
          </div>
          {voiceErr && (
            <div className="alert-card" style={{ marginTop: 8, padding: 6 }}>
              <div className="err-text" style={{ fontSize: 11 }}>{voiceErr}</div>
              <button
                className="ghost"
                style={{ marginTop: 4, fontSize: 11 }}
                onClick={async () => {
                  try {
                    const r = await apiFetch<{ error?: string; state?: string }>("/voice/session", { method: "POST", body: { action: "recover" } });
                    onNotify(r.error ? r.error : `Microphone recovered (${r.state || "ready"})`);
                  } catch (err) {
                    onNotify((err as Error).message);
                  }
                }}
              >
                Recover Mic
              </button>
            </div>
          )}
        </section>

        {pendingApprovals.length > 0 && (
          <section className="hud-section alert-card">
            <div className="row between">
              <strong style={{ color: "var(--warn)", fontSize: 12 }}>Approvals Pending ({pendingApprovals.length})</strong>
              <button className="ghost" onClick={() => (onOpenDrawer ? onOpenDrawer("approvals") : onNavigate?.("workforce"))}>Review</button>
            </div>
            <p className="dim" style={{ marginTop: 4 }}>{pendingApprovals[0].action_type}: {pendingApprovals[0].target_resource.slice(0, 60)}</p>
          </section>
        )}

        {activeRuns.length > 0 && (
          <section className="hud-section">
            <h3>Active Agent Workflows ({activeRuns.length})</h3>
            {activeRuns.slice(0, 4).map((r) => (
              <button key={r.id} className="line link" onClick={() => onInspect?.({ kind: "node", id: `task:${r.id}` })}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{r.agent_role}</span>
                <span className={`pill ${runStatus(r.state).tone}`}>{r.current_step || runStatus(r.state).label}</span>
              </button>
            ))}
          </section>
        )}

        <section className="hud-section">
          <h3>System Telemetry</h3>
          <div className="health-matrix">
            {rows.map(([name, h, detail]) => (
              <div key={name} className="health-cell" title={detail}>
                <span className={`status ${TONE[h]}`}><i className="dot" /> {name}</span>
                <small className="mono dim">{detail}</small>
              </div>
            ))}
          </div>
          {sys && (
            <div className="mono dim" style={{ marginTop: 8, fontSize: 11 }}>
              CPU {sys.cpu_pct ?? "…"}% · RAM {(sys.ram.total_gb - sys.ram.free_gb).toFixed(1)}/{sys.ram.total_gb} GB
              {sys.gpu ? ` · GPU ${sys.gpu.used_gb}/${sys.gpu.total_gb} GB` : ""}
            </div>
          )}
        </section>
      </div>
    </aside>
  );
};

