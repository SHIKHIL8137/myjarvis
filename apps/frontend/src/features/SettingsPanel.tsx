import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { ShellProps } from "../shell";

interface Config {
  tts: { enabled: boolean; voice: string; speed: number };
  audio: { voice_mode: string };
  internet: { enabled: boolean; provider: string };
  history_turns: number;
  num_ctx: number;
  offline_enforced: boolean;
  voices: string[];
}
interface ModelItem { key: string; model: string; provider: string; installed: boolean; current: boolean; thinking: boolean }

/** Everyday settings. All writes go through the existing /api/config, /api/models/switch and /api/internet/mode endpoints. */
export const SettingsPanel: React.FC<ShellProps> = ({ onNotify }) => {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [models, setModels] = useState<ModelItem[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [c, m] = await Promise.all([apiFetch<Config>("/config"), apiFetch<{ models: ModelItem[] }>("/models")]);
      setCfg(c);
      setModels(m.models);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      onNotify(label);
    } catch (e) {
      onNotify((e as Error).message);
    }
    load();
  };
  const put = (label: string, patch: Record<string, unknown>) => save(label, () => apiFetch("/config", { method: "PUT", body: patch }));

  if (error) return <div className="scroll-body"><div className="card"><p className="err-text">Settings could not be loaded: {error}</p><button onClick={load}>Retry</button></div></div>;
  if (!cfg) return <div className="scroll-body"><p className="empty">Loading settings…</p></div>;

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>Language model</h3>
        <label className="field">Active model
          <select value={models.find((m) => m.current)?.key || ""} onChange={(e) => save(`Switched to ${e.target.value}`, () => apiFetch("/models/switch", { method: "POST", body: { model: e.target.value } }))}>
            {models.map((m) => <option key={m.key} value={m.key} disabled={!m.installed}>{m.model} ({m.provider}{m.thinking ? ", reasoning" : ""}{m.installed ? "" : ", not installed"})</option>)}
          </select>
        </label>
        <label className="field">Context window (tokens)
          <select value={cfg.num_ctx} onChange={(e) => put("Context size updated", { num_ctx: Number(e.target.value) })}>
            {[...new Set([2048, 4096, 8192, cfg.num_ctx])].sort((a, b) => a - b).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="field">Conversation turns kept in the prompt
          <select value={cfg.history_turns} onChange={(e) => put("History window updated", { history_turns: Number(e.target.value) })}>
            {[...new Set([0, 2, 4, 6, 8, cfg.history_turns])].sort((a, b) => a - b).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
      </div>

      <div className="card">
        <h3>Network</h3>
        <div className="row between">
          <span>{cfg.internet.enabled ? `Online assisted (${cfg.internet.provider})` : "Local mode: outbound internet requests are blocked"}</span>
          <button aria-pressed={cfg.internet.enabled} onClick={() => save(cfg.internet.enabled ? "Local mode active" : "Online assisted mode enabled", () => apiFetch("/internet/mode", { method: "POST", body: { enabled: !cfg.internet.enabled } }))}>
            {cfg.internet.enabled ? "Switch to local" : "Go online"}
          </button>
        </div>
        {cfg.offline_enforced && <p className="dim">Offline enforcement is on in config.json; going online is refused by the backend until it is turned off there.</p>}
      </div>

      <div className="card">
        <h3>Voice</h3>
        <div className="row between">
          <span>Spoken replies</span>
          <button aria-pressed={cfg.tts.enabled} onClick={() => put(cfg.tts.enabled ? "Voice muted" : "Voice enabled", { tts: { enabled: !cfg.tts.enabled } })}>{cfg.tts.enabled ? "On" : "Off"}</button>
        </div>
        <label className="field">Voice
          <select value={cfg.tts.voice} onChange={(e) => put("Voice changed", { tts: { voice: e.target.value } })}>
            {cfg.voices.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        <label className="field">Listening mode
          <select value={cfg.audio.voice_mode} onChange={(e) => put("Listening mode changed", { audio: { voice_mode: e.target.value }, wake: { enabled: e.target.value === "wake_word" } })}>
            {[...new Set(["continuous", "wake_word", "ptt", cfg.audio.voice_mode])].map((m) => <option key={m} value={m}>{m === "ptt" ? "push to talk" : m.replace(/_/g, " ")}</option>)}
          </select>
        </label>
      </div>

      <div className="card">
        <h3>Advanced</h3>
        <p className="dim">Speech recognition, translation, filesystem roots, provider keys and the transcript editor live in the classic console.</p>
        <a className="btn ghost" href="/legacy/" target="_blank" rel="noreferrer">Open classic console</a>
      </div>
    </div>
  );
};
