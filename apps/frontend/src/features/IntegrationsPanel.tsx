import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import {
  hotkeyError,
  mcpTogglePatch,
  mcpTone,
  meterPercent,
  missingItems,
  nextStep,
  parseBlockedTitles,
  prevStep,
  stepIndex,
  WIZARD_STEPS,
  type HealthCheck,
  type McpServerStatus,
  type WizardStep,
} from "./setupLogic";

interface SettingsSlice {
  mcp?: { servers: Record<string, Record<string, unknown>> };
  dictation?: { enabled: boolean; hotkey: string; double_tap_hands_free: boolean; remove_fillers: boolean; history_enabled: boolean; dictionary_file: string };
  screen?: { enabled: boolean; first_use_approved: boolean; blocked_window_titles: string[] };
  stt?: { model: string };
  tts?: { voice: string };
  stt_models?: string[];
  voices?: string[];
}
interface ModelItem { key: string; model: string; installed: boolean; current: boolean; local?: boolean }
interface Props { onNotify: (msg: string) => void }

const tone = (s: HealthCheck["status"]) => (s === "ok" ? "ok" : s === "warn" ? "warn" : "danger");

/** System health: every row is a real probe; failed rows show the exact manual step (nothing is downloaded automatically). */
const HealthCard: React.FC<Props & { checks: HealthCheck[]; refresh: () => void; loading: boolean }> = ({ checks, refresh, loading }) => (
  <div className="card">
    <div className="row" style={{ justifyContent: "space-between" }}>
      <h3>SYSTEM HEALTH</h3>
      <button className="ghost" onClick={refresh} disabled={loading}>{loading ? "Checking…" : "Re-check"}</button>
    </div>
    {checks.map((c) => (
      <div key={c.id} style={{ padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <strong>{c.label}</strong>
          <span className={`pill ${tone(c.status)}`}>{c.status.toUpperCase()}</span>
        </div>
        <div className="dim">{c.detail}</div>
        {c.action && c.status !== "ok" && <pre className="diff" style={{ whiteSpace: "pre-wrap" }}>{c.action}</pre>}
      </div>
    ))}
    {!checks.length && <p className="dim">No results yet.</p>}
  </div>
);

/** First-run wizard. Saves through the existing settings / model APIs; hardware tests call the setup endpoints. */
const Wizard: React.FC<Props & { checks: HealthCheck[]; settings: SettingsSlice; reloadAll: () => void; onClose: () => void }> = ({ onNotify, checks, settings, reloadAll, onClose }) => {
  const [step, setStep] = useState<WizardStep>("welcome");
  const [models, setModels] = useState<ModelItem[]>([]);
  const [level, setLevel] = useState(0);
  const [metering, setMetering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [wake, setWake] = useState<string>("");
  const meterTimer = useRef<number | null>(null);
  const [mics, setMics] = useState<Array<{ id: number; name: string; default: boolean; ok: boolean; frames: number; error: string }>>([]);
  const [micDiag, setMicDiag] = useState<{ status: string; detail: string; used: { id: number } | null }>({ status: "", detail: "", used: null });
  const loadMics = useCallback(async () => {
    try { const r = await apiFetch<{ devices: typeof mics; diagnosis: typeof micDiag }>("/v2/setup/mic-devices"); setMics(r.devices); setMicDiag(r.diagnosis); } catch (e) { onNotify((e as Error).message); }
  }, [onNotify]);
  useEffect(() => { if (step === "mic") loadMics(); }, [step, loadMics]);

  useEffect(() => {
    apiFetch<{ models: ModelItem[] }>("/models").then((r) => setModels(r.models || [])).catch(() => setModels([]));
  }, []);
  useEffect(() => () => { if (meterTimer.current) window.clearTimeout(meterTimer.current); }, []);

  const save = async (label: string, patch: Record<string, unknown>) => {
    try { await apiFetch("/v2/settings", { method: "PUT", body: patch }); onNotify(label); reloadAll(); } catch (e) { onNotify((e as Error).message); }
  };

  const runMeter = () => {
    setMetering(true);
    const started = Date.now();
    const tick = async () => {
      try {
        const r = await apiFetch<{ ok: boolean; level?: number; error?: string }>("/v2/setup/mic-level");
        if (!r.ok) { onNotify(r.error || "Microphone unavailable"); setMetering(false); return; }
        setLevel(r.level ?? 0);
      } catch (e) { onNotify((e as Error).message); setMetering(false); return; }
      if (Date.now() - started < 8000) meterTimer.current = window.setTimeout(tick, 150);
      else setMetering(false);
    };
    tick();
  };

  const run = async (path: string, ok: (r: Record<string, unknown>) => string) => {
    setBusy(true);
    try { const r = await apiFetch<Record<string, unknown>>(path, { method: "POST", body: {} }); onNotify(ok(r)); return r; }
    catch (e) { onNotify((e as Error).message); return null; }
    finally { setBusy(false); }
  };

  const missing = missingItems(checks);
  const local = models.filter((m) => m.installed);

  return (
    <div className="card" role="region" aria-label="First-run setup">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3>SETUP WIZARD — STEP {stepIndex(step) + 1} OF {WIZARD_STEPS.length}</h3>
        <button className="ghost" onClick={onClose}>Close</button>
      </div>

      {step === "welcome" && (
        <>
          <p>JARVIS runs offline by default. Models are installed by you, never downloaded automatically.</p>
          {checks.length === 0 ? <p className="dim">Checking your system…</p> : missing.length === 0 ? <p className="dim">All required components were found.</p> : missing.map((m) => (
            <div key={m.id}><strong>{m.label}:</strong> {m.detail}<pre className="diff" style={{ whiteSpace: "pre-wrap" }}>{m.action}</pre></div>
          ))}
        </>
      )}
      {step === "llm" && (
        <>
          <p>Installed local models (from Ollama):</p>
          {local.length ? local.map((m) => (
            <div className="row" key={m.key}>
              <span>{m.model}</span>
              {m.current ? <span className="pill ok">ACTIVE</span> : <button className="ghost" onClick={async () => { try { await apiFetch("/models/switch", { method: "POST", body: { model: m.key } }); onNotify(`Switched to ${m.model}`); setModels((all) => all.map((x) => ({ ...x, current: x.key === m.key }))); } catch (e) { onNotify((e as Error).message); } }}>Use this</button>}
            </div>
          )) : <><p className="dim">No local model found.</p><pre className="diff">ollama pull qwen3:4b-instruct</pre></>}
        </>
      )}
      {step === "stt" && (
        <>
          <p>Speech-to-text model (must already be downloaded):</p>
          {(settings.stt_models || []).length ? (
            <select value={settings.stt?.model} onChange={(e) => save("Speech model saved", { stt: { model: e.target.value } })}>
              {(settings.stt_models || []).map((m) => <option key={m}>{m}</option>)}
            </select>
          ) : <>{checks.filter((c) => c.id === "stt_model").map((c) => <pre key={c.id} className="diff" style={{ whiteSpace: "pre-wrap" }}>{c.action || c.detail}</pre>)}</>}
        </>
      )}
      {step === "voice" && (
        <>
          <p>Piper voice:</p>
          {(settings.voices || []).length ? (
            <select value={settings.tts?.voice} onChange={(e) => save("Voice saved", { tts: { voice: e.target.value } })}>
              {(settings.voices || []).map((v) => <option key={v}>{v}</option>)}
            </select>
          ) : <>{checks.filter((c) => c.id === "tts_voice").map((c) => <pre key={c.id} className="diff" style={{ whiteSpace: "pre-wrap" }}>{c.action || c.detail}</pre>)}</>}
        </>
      )}
      {step === "mic" && (
        <>
          <label>Input device{" "}
            <select aria-label="Input device" value={micDiag.used?.id ?? ""} onChange={async (e) => { await save("Input device saved", { audio: { input_device: Number(e.target.value) } }); loadMics(); }}>
              {mics.map((m) => <option key={m.id} value={m.id}>{m.name}{m.default ? " (system default)" : ""} — {m.ok ? "audio OK" : "NO AUDIO"}</option>)}
            </select>
          </label>
          {micDiag.detail && <p className={micDiag.status === "ok" ? "dim" : ""} role="status">{micDiag.detail}</p>}
          <p>Speak normally; the bar should move.</p>
          <div style={{ height: 12, background: "rgba(255,255,255,0.1)", borderRadius: 6, overflow: "hidden" }} role="progressbar" aria-valuenow={meterPercent(level)} aria-valuemin={0} aria-valuemax={100} aria-label="Microphone level">
            <div style={{ width: `${meterPercent(level)}%`, height: "100%", background: "var(--accent, #4cf)", transition: "width 120ms" }} />
          </div>
          <button className="ghost" onClick={runMeter} disabled={metering}>{metering ? "Listening…" : "Start microphone test"}</button>
        </>
      )}
      {step === "speaker" && <button className="ghost" disabled={busy} onClick={() => run("/v2/setup/speaker-test", (r) => (r.ok ? "Did you hear the tone?" : String(r.error || "Speaker test failed")))}>Play test tone</button>}
      {step === "wake" && (
        <>
          <p>Say “Hey Jarvis” after pressing the button (5 s).</p>
          <button className="ghost" disabled={busy} onClick={async () => { const r = await run("/v2/setup/wake-test", () => "Wake test finished"); if (r) setWake(r.ok ? (r.detected ? `Detected (score ${r.best_score})` : `Not detected (best score ${r.best_score}, needs ${r.threshold})`) : String(r.error)); }}>Test wake word</button>
          {wake && <p className="dim">{wake}</p>}
        </>
      )}
      {step === "finish" && (
        <>
          <p>Finish to mark setup complete. You can re-run this wizard from Settings at any time.</p>
          <button onClick={async () => { await run("/v2/setup/complete", () => "Setup complete"); onClose(); }}>Finish setup</button>
        </>
      )}

      <div className="row" style={{ marginTop: 12 }}>
        <button className="ghost" onClick={() => setStep(prevStep(step))} disabled={step === "welcome"}>Back</button>
        <button className="ghost" onClick={() => setStep(nextStep(step))} disabled={step === "finish"}>Next</button>
      </div>
    </div>
  );
};

const McpCard: React.FC<Props & { settings: SettingsSlice; reloadAll: () => void }> = ({ onNotify, settings, reloadAll }) => {
  const [servers, setServers] = useState<McpServerStatus[]>([]);
  const [tools, setTools] = useState<Array<{ full_name: string; risk: string; description: string }>>([]);
  const [presets, setPresets] = useState<Array<{ name: string; installed: boolean; description: string; network: boolean }>>([]);

  const refresh = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        apiFetch<{ servers: McpServerStatus[]; tools: Array<{ full_name: string; risk: string; description: string }> }>("/v2/mcp/servers"),
        apiFetch<{ presets: Array<{ name: string; installed: boolean; description: string; network: boolean }> }>("/v2/mcp/presets"),
      ]);
      setServers(s.servers); setTools(s.tools); setPresets(p.presets);
    } catch (e) { onNotify((e as Error).message); }
  }, [onNotify]);
  useEffect(() => { refresh(); }, [refresh, settings.mcp]);

  const op = async (name: string, action: "start" | "stop" | "restart") => {
    try { await apiFetch(`/v2/mcp/servers/${name}/${action}`, { method: "POST", body: {} }); onNotify(`${name}: ${action} ok`); } catch (e) { onNotify((e as Error).message); }
    refresh();
  };
  const toggle = async (name: string, enabled: boolean) => {
    try { await apiFetch("/v2/settings", { method: "PUT", body: mcpTogglePatch(settings.mcp?.servers || {}, name, enabled) }); reloadAll(); } catch (e) { onNotify((e as Error).message); }
  };

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3>MCP SERVERS (disabled by default)</h3>
        <button className="ghost" onClick={refresh}>Refresh</button>
      </div>
      <p className="dim">Tools appear as <code>server.tool</code>. Anything that writes, sends, deletes or is unrecognised needs your approval; results are treated as untrusted. Credentials are ${"{ENV_VAR}"} references, never stored here.</p>
      {presets.filter((p) => !p.installed).map((p) => (
        <div className="row" key={p.name}>
          <span><strong>{p.name}</strong> preset{p.network ? " (needs Online Assisted mode)" : ""}: {p.description}</span>
          <button className="ghost" onClick={async () => { try { await apiFetch(`/v2/mcp/presets/${p.name}/install`, { method: "POST", body: {} }); onNotify(`${p.name} added (disabled)`); reloadAll(); } catch (e) { onNotify((e as Error).message); } }}>Add (disabled)</button>
        </div>
      ))}
      {servers.length === 0 && <p className="dim">No servers configured.</p>}
      {servers.map((s) => (
        <div key={s.name} style={{ padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
            <div><strong>{s.name}</strong> <span className={`pill ${mcpTone(s)}`}>{s.enabled ? s.state.toUpperCase() : "DISABLED"}</span> <span className="dim">{s.tool_count} tools · {s.restarts} restarts{s.network ? " · network" : ""}</span></div>
            <div className="row">
              <button className="ghost" aria-pressed={s.enabled} onClick={() => toggle(s.name, !s.enabled)}>{s.enabled ? "Disable" : "Enable"}</button>
              <button className="ghost" disabled={!s.enabled} onClick={() => op(s.name, "start")}>Start</button>
              <button className="ghost" onClick={() => op(s.name, "stop")}>Stop</button>
              <button className="ghost" disabled={!s.enabled} onClick={() => op(s.name, "restart")}>Restart</button>
            </div>
          </div>
          {s.last_error && <div className="dim">Last error: {s.last_error}</div>}
          <div className="row wrap">
            {tools.filter((t) => t.full_name.startsWith(`${s.name}.`)).map((t) => (
              <span key={t.full_name} className={`chip static ${t.risk === "read" ? "" : "off"}`} title={t.description}>{t.full_name} · {t.risk}</span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
};

const DictationCard: React.FC<Props & { settings: SettingsSlice; reloadAll: () => void }> = ({ onNotify, settings, reloadAll }) => {
  const d = settings.dictation;
  const [hotkey, setHotkey] = useState(d?.hotkey || "ctrl+alt+space");
  const [history, setHistory] = useState<Array<{ t: number; text: string }>>([]);
  useEffect(() => { if (d) setHotkey(d.hotkey); }, [d?.hotkey]);
  const loadHistory = useCallback(() => { apiFetch<{ items: Array<{ t: number; text: string }> }>("/v2/dictation/history").then((r) => setHistory(r.items)).catch(() => setHistory([])); }, []);
  useEffect(() => { loadHistory(); }, [loadHistory, d?.history_enabled]);
  if (!d) return null;
  const patch = async (label: string, p: Partial<typeof d>) => { try { await apiFetch("/v2/settings", { method: "PUT", body: { dictation: p } }); onNotify(label); reloadAll(); } catch (e) { onNotify((e as Error).message); } };
  const err = hotkeyError(hotkey);
  return (
    <div className="card">
      <h3>PUSH-TO-TALK DICTATION</h3>
      <p className="dim">Hold the hotkey, speak, release: the text goes into the focused app. Your text clipboard is restored after pasting; if it holds non-text data (images, rich text) the text is typed instead and the clipboard is left alone. Dictation never reaches the assistant.</p>
      <div className="row wrap">
        <button className="ghost" aria-pressed={d.enabled} onClick={() => patch("Dictation updated", { enabled: !d.enabled })}>Dictation: {d.enabled ? "On" : "Off"}</button>
        <button className="ghost" aria-pressed={d.double_tap_hands_free} onClick={() => patch("Hands-free updated", { double_tap_hands_free: !d.double_tap_hands_free })}>Double-tap hands-free: {d.double_tap_hands_free ? "On" : "Off"}</button>
        <button className="ghost" aria-pressed={d.remove_fillers} onClick={() => patch("Filler removal updated", { remove_fillers: !d.remove_fillers })}>Remove um/uh: {d.remove_fillers ? "On" : "Off"}</button>
        <button className="ghost" aria-pressed={d.history_enabled} onClick={() => patch("History setting updated", { history_enabled: !d.history_enabled })}>Keep text history: {d.history_enabled ? "On" : "Off"}</button>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <label>Hotkey <input value={hotkey} onChange={(e) => setHotkey(e.target.value)} aria-invalid={!!err} /></label>
        <button className="ghost" disabled={!!err || hotkey === d.hotkey} onClick={() => patch("Hotkey saved", { hotkey })}>Save hotkey</button>
        {err && <span className="pill danger">{err}</span>}
      </div>
      <p className="dim">Custom words: edit <code>{d.dictionary_file}</code> (one word, or <code>heard =&gt; written</code>, per line).</p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>History ({history.length})</strong>
        <button className="ghost" disabled={!history.length} onClick={async () => { try { await apiFetch("/v2/dictation/history", { method: "DELETE" }); onNotify("Dictation history deleted"); loadHistory(); } catch (e) { onNotify((e as Error).message); } }}>Delete history</button>
      </div>
      {history.slice(0, 5).map((h) => <div key={h.t} className="dim">{new Date(h.t * 1000).toLocaleTimeString()} — {h.text}</div>)}
    </div>
  );
};

const ScreenCard: React.FC<Props & { settings: SettingsSlice; reloadAll: () => void }> = ({ onNotify, settings, reloadAll }) => {
  const sc = settings.screen;
  const [titles, setTitles] = useState((sc?.blocked_window_titles || []).join("\n"));
  useEffect(() => { if (sc) setTitles(sc.blocked_window_titles.join("\n")); }, [sc?.blocked_window_titles?.join("|")]);
  if (!sc) return null;
  const patch = async (label: string, p: Record<string, unknown>) => { try { await apiFetch("/v2/settings", { method: "PUT", body: { screen: p } }); onNotify(label); reloadAll(); } catch (e) { onNotify((e as Error).message); } };
  return (
    <div className="card">
      <h3>SCREEN READING (local OCR)</h3>
      <p className="dim">Off by default. When on, JARVIS reads the screen only when you ask, with local OCR; nothing is saved or uploaded. The first use needs your approval. Capture is refused while a blocked window is visible.</p>
      <div className="row wrap">
        <button className="ghost" aria-pressed={sc.enabled} onClick={() => patch("Screen reading updated", { enabled: !sc.enabled })}>Screen reading: {sc.enabled ? "On" : "Off"}</button>
        <span className={`pill ${sc.first_use_approved ? "ok" : "warn"}`}>{sc.first_use_approved ? "First use approved" : "First use pending approval"}</span>
        {sc.first_use_approved && <button className="ghost" onClick={() => patch("Approval revoked", { first_use_approved: false })}>Revoke approval</button>}
      </div>
      <label className="dim" htmlFor="blocked-titles">Blocked window titles (one per line; matches any visible window)</label>
      <textarea id="blocked-titles" rows={5} style={{ width: "100%" }} value={titles} onChange={(e) => setTitles(e.target.value)} />
      <button className="ghost" onClick={() => patch("Blocked windows saved", { blocked_window_titles: parseBlockedTitles(titles) })}>Save blocked list</button>
    </div>
  );
};

export const IntegrationsPanel: React.FC<Props> = ({ onNotify }) => {
  const [checks, setChecks] = useState<HealthCheck[]>([]);
  const [loading, setLoading] = useState(false);
  const [settings, setSettings] = useState<SettingsSlice>({});
  const [wizard, setWizard] = useState(false);

  const loadHealth = useCallback(async () => {
    setLoading(true);
    try { setChecks((await apiFetch<{ checks: HealthCheck[] }>("/v2/setup/health")).checks); } catch (e) { onNotify((e as Error).message); } finally { setLoading(false); }
  }, [onNotify]);
  const loadSettings = useCallback(async () => {
    try { setSettings(await apiFetch<SettingsSlice>("/v2/settings")); } catch (e) { onNotify((e as Error).message); }
  }, [onNotify]);
  const reloadAll = useCallback(() => { loadSettings(); loadHealth(); }, [loadSettings, loadHealth]);

  useEffect(() => {
    loadHealth();
    loadSettings();
    apiFetch<{ completed: boolean }>("/v2/setup/state").then((s) => setWizard(!s.completed)).catch(() => undefined);
  }, [loadHealth, loadSettings]);

  return (
    <>
      {wizard ? <Wizard onNotify={onNotify} checks={checks} settings={settings} reloadAll={reloadAll} onClose={() => setWizard(false)} /> : (
        <div className="card"><div className="row" style={{ justifyContent: "space-between" }}><h3>SETUP</h3><button className="ghost" onClick={() => setWizard(true)}>Run setup wizard</button></div></div>
      )}
      <HealthCard onNotify={onNotify} checks={checks} refresh={loadHealth} loading={loading} />
      <McpCard onNotify={onNotify} settings={settings} reloadAll={reloadAll} />
      <DictationCard onNotify={onNotify} settings={settings} reloadAll={reloadAll} />
      <ScreenCard onNotify={onNotify} settings={settings} reloadAll={reloadAll} />
    </>
  );
};
