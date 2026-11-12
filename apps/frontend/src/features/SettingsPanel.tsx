import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { AudioVideoPanel, SettingsErrorBoundary, type HostAudioInventory } from "../components/AudioVideoPanel";
import { fmtTime, type ShellProps } from "../shell";

type SectionId = "providers" | "voice" | "permissions" | "system";

interface ConsolidatedSettings {
  tts: { enabled: boolean; voice: string; speed: number; output_device?: number | string | null };
  stt: {
    model: string;
    language: string;
    quality: string;
    preprocessing_profile?: string;
    vocab_hints?: string;
    malayalam_model?: string;
    max_retry_attempts?: number;
  };
  audio: {
    voice_mode: string;
    barge_in_enabled: boolean;
    suppress_mic_during_tts: boolean;
    prefer_native_sample_rate?: boolean;
    input_device: number | null;
    output_device: number | null;
  };
  translation?: {
    mode: string;
    target_language: string;
    auto_correct: boolean;
    prefer_whisper_for_english: boolean;
    verify_translations: boolean;
    preserve_mixed_code_switching: boolean;
  };
  wake: { enabled: boolean };
  internet: { enabled: boolean; provider: string };
  filesystem: {
    sensitive_file_policy: string;
    cloud_data_policy: string;
    allow_always_read_only: boolean;
    max_read_bytes: number;
  };
  providers: {
    active_provider: string;
    fast_model: string;
    advanced_model: string;
    coding_model: string;
    embedding_model: string;
    fallback_provider: string;
    streaming: boolean;
    allow_cloud: boolean;
    allow_paid_models: boolean;
    openai_base_url: string;
    openai_model: string;
    openrouter_base_url: string;
    openrouter_model: string;
    gemini_base_url: string;
    gemini_model: string;
    huggingface_base_url: string;
    huggingface_model: string;
    anthropic_base_url: string;
    anthropic_model: string;
    custom_base_url: string;
    custom_model: string;
  };
  gateway: {
    request_timeout_s: number;
    scrub_outbound_secrets: boolean;
    daily_token_budget: number;
  };
  preferences: {
    theme: string;
    compact_mode: boolean;
    reduced_motion: boolean;
    graph_detail: string;
    stt_provider: string;
    tts_provider: string;
    personality_preset?: string;
    custom_instructions?: string;
  };
  actions?: {
    enabled: boolean;
    allow_screenshots: boolean;
    allow_media_controls: boolean;
    allow_window_controls: boolean;
    allow_input_automation: boolean;
  };
  monitoring?: {
    enabled: boolean;
    cpu_alert_pct: number;
    ram_alert_pct: number;
    gpu_vram_alert_pct: number;
    disk_free_alert_gb: number;
  };
  proactive?: {
    enabled: boolean;
    cooldown_s: number;
  };
  provider_keys: Record<string, boolean>;
  history_turns: number;
  num_ctx: number;
  offline_enforced: boolean;
  mode: string;
  voices: string[];
  stt_models: string[];
  stt_languages: Record<string, string> | string[];
}

interface ModelItem {
  key: string;
  model: string;
  provider: string;
  installed: boolean;
  current: boolean;
  thinking: boolean;
}

interface CapabilityItem {
  provider: string;
  model_id: string;
  streaming: boolean;
  tool_calling: boolean;
  local: boolean;
  max_ctx: number;
  pricing_tier: string;
  breaker_state: string;
  key_configured: boolean;
}

interface FsPermItem {
  permission_id: string;
  canonical_folder_path: string;
  granted_operations: string[];
  scope: string;
  status: string;
  granted_at: number;
  expires_at?: number | null;
  purpose: string;
}

interface FsAuditItem {
  id: string;
  actor: string;
  decision: string;
  target: string;
  details: Record<string, unknown>;
  created_at: number;
}

interface DeviceInfo {
  audio?: HostAudioInventory;
}

const ALL_OPS = [
  "list_directory",
  "read_file",
  "search_files",
  "read_metadata",
  "create_file",
  "write_file",
  "edit_file",
  "execute_in_directory",
  "delete_file",
];
const READ_OPS = ["list_directory", "read_file", "search_files", "read_metadata"];
const CLOUD_PROVIDERS = ["openrouter", "gemini", "huggingface", "openai", "anthropic", "custom"] as const;

export const SettingsPanel: React.FC<ShellProps> = ({ onNotify }) => {
  const [section, setSection] = useState<SectionId>("providers");
  const [cfg, setCfg] = useState<ConsolidatedSettings | null>(null);
  const [models, setModels] = useState<ModelItem[]>([]);
  const [caps, setCaps] = useState<CapabilityItem[]>([]);
  const [devices, setDevices] = useState<DeviceInfo>({});
  const [perms, setPerms] = useState<FsPermItem[]>([]);
  const [audit, setAudit] = useState<FsAuditItem[]>([]);
  const [overview, setOverview] = useState<Record<string, unknown> | null>(null);
  const [skillsDiag, setSkillsDiag] = useState<{
    skills?: Array<{ skill_id?: string; name: string; enabled?: boolean }>;
    installed?: Array<{ skill_id?: string; name: string; enabled?: boolean }>;
  } | null>(null);
  const [decisionDiag, setDecisionDiag] = useState<{ enabled?: boolean; mode?: string; active_policy?: { policy_id?: string } } | null>(null);
  const [error, setError] = useState("");

  // Provider key & health/route test state
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [healthOut, setHealthOut] = useState<Record<string, unknown> | null>(null);
  const [routeOut, setRouteOut] = useState<Record<string, unknown> | null>(null);
  const [routeRole, setRouteRole] = useState<"fast" | "advanced" | "coding" | "embedding">("fast");

  // Local editable provider & custom routing form
  const [provDraft, setProvDraft] = useState<Record<string, string>>({});
  const [timeoutDraft, setTimeoutDraft] = useState<number>(45);

  // Add Folder Permission form
  const [newFolder, setNewFolder] = useState("");
  const [newOps, setNewOps] = useState<string[]>(READ_OPS);
  const [newScope, setNewScope] = useState<"persistent" | "session">("persistent");

  const loadAll = useCallback(async () => {
    try {
      const [s, m, c, d, p, a, o, sk, dc] = await Promise.all([
        apiFetch<ConsolidatedSettings>("/v2/settings"),
        apiFetch<{ models: ModelItem[] }>("/models"),
        apiFetch<{ capabilities: CapabilityItem[] }>("/v2/gateway/capabilities").catch(() => ({ capabilities: [] })),
        apiFetch<DeviceInfo>("/v2/settings/devices").catch(() => ({})),
        apiFetch<{ permissions: FsPermItem[] }>("/v2/filesystem/permissions").catch(() => ({ permissions: [] })),
        apiFetch<{ events: FsAuditItem[] }>("/v2/filesystem/audit?limit=40").catch(() => ({ events: [] })),
        apiFetch<Record<string, unknown>>("/v2/system/overview").catch(() => null),
        apiFetch<{ skills?: Array<{ skill_id?: string; name: string; enabled?: boolean }>; installed?: Array<{ skill_id?: string; name: string; enabled?: boolean }> }>("/v2/skills").catch(() => null),
        apiFetch<{ enabled?: boolean; mode?: string; active_policy?: { policy_id?: string } }>("/v2/decision/status").catch(() => null),
      ]);
      setCfg(s);
      setModels(m.models || []);
      setCaps(c.capabilities || []);
      setDevices(d);
      setPerms(p.permissions || []);
      setAudit(a.events || []);
      setOverview(o);
      setSkillsDiag(sk);
      setDecisionDiag(dc);
      setProvDraft({
        active_provider: s.providers.active_provider || "ollama",
        fast_model: s.providers.fast_model || "",
        advanced_model: s.providers.advanced_model || "",
        coding_model: s.providers.coding_model || "",
        embedding_model: s.providers.embedding_model || "",
        fallback_provider: s.providers.fallback_provider || "ollama",
        custom_base_url: s.providers.custom_base_url || "",
        custom_model: s.providers.custom_model || "",
      });
      setTimeoutDraft(Number(s.gateway.request_timeout_s || 45));
      setError("");
    } catch (e) {
      const msg = (e as Error).message || "Unknown error";
      setError(
        msg.toLowerCase().includes("not found")
          ? `${msg} — the backend server on this port may be running an older build without /api/v2/settings. Restart server.py to activate P10.20 routes.`
          : msg
      );
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const mutate = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      onNotify(label);
    } catch (e) {
      onNotify((e as Error).message);
    }
    loadAll();
  };

  const patchSettings = (label: string, patch: Record<string, unknown>) =>
    mutate(label, () => apiFetch("/v2/settings", { method: "PUT", body: patch }));

  const saveProviderKey = async (prov: string) => {
    const k = (keyInputs[prov] || "").trim();
    if (!k) {
      onNotify(`Enter an API key for ${prov} first.`);
      return;
    }
    await mutate(`Saved encrypted key for ${prov}`, () =>
      apiFetch(`/v2/gateway/providers/${prov}/key`, { method: "POST", body: { api_key: k } })
    );
    setKeyInputs((prev) => ({ ...prev, [prov]: "" }));
  };

  const pickFolder = async () => {
    try {
      const r = await apiFetch<{ ok: boolean; cancelled: boolean; path: string | null; reason?: string }>(
        "/v2/filesystem/pick-folder",
        { method: "POST" }
      );
      if (r.path) setNewFolder(r.path);
      else if (r.reason) onNotify(r.reason);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  if (error) {
    return (
      <div className="scroll-body">
        <div className="card">
          <p className="err-text">Settings failed to load: {error}</p>
          <button onClick={loadAll}>Retry</button>
        </div>
      </div>
    );
  }
  if (!cfg) return <div className="scroll-body"><p className="empty">Loading settings…</p></div>;

  return (
    <div className="scroll-body">
      <div className="card row between wrap">
        <div>
          <span className="eyebrow">Consolidated Control Plane</span>
          <h2 style={{ fontSize: 16 }}>SETTINGS</h2>
        </div>
        <div className="row wrap">
          {(
            [
              ["providers", "AI Providers & Routing"],
              ["voice", "Voice & Audio"],
              ["permissions", "Permissions & Privacy"],
              ["system", "System & Data"],
            ] as Array<[SectionId, string]>
          ).map(([id, label]) => (
            <button key={id} className={section === id ? "active" : "ghost"} onClick={() => setSection(id)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* SECTION 1: AI PROVIDERS & MODEL ROUTING */}
      {section === "providers" && (
        <SettingsErrorBoundary title="AI Providers & Routing">
          <div className="card">
            <h3>Primary Model &amp; Task Routing</h3>
            <p className="field-hint" style={{ marginBottom: 10 }}>
              Choose which local or cloud model answers requests, and how Jarvis assigns quick, reasoning, coding, and memory roles.
            </p>
            <div className="grid-2">
              <label className="field">
                Active Local Brain (Ollama)
                <span className="field-hint">Model used for normal Jarvis conversations.</span>
                <select
                  value={models.find((m) => m.current)?.key || ""}
                  onChange={(e) => mutate(`Switched brain to ${e.target.value}`, () => apiFetch("/models/switch", { method: "POST", body: { model: e.target.value } }))}
                >
                  {models.map((m) => (
                    <option key={m.key} value={m.key} disabled={!m.installed}>
                      {m.model} ({m.provider}{m.thinking ? ", thinking" : ""}{m.installed ? "" : ", not installed"})
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                Active Gateway Provider
                <span className="field-hint">Service used to connect to the selected model.</span>
                <select
                  value={provDraft.active_provider || "ollama"}
                  onChange={(e) => setProvDraft((d) => ({ ...d, active_provider: e.target.value }))}
                >
                  <option value="ollama">ollama (Local)</option>
                  {CLOUD_PROVIDERS.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Fast Model Role
                <span className="field-hint">Model role for quick responses.</span>
                <input
                  value={provDraft.fast_model || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, fast_model: e.target.value }))}
                  placeholder="fast or provider:model"
                />
              </label>

              <label className="field">
                Advanced Reasoning Model Role
                <span className="field-hint">Model role for complex reasoning.</span>
                <input
                  value={provDraft.advanced_model || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, advanced_model: e.target.value }))}
                  placeholder="think or provider:model"
                />
              </label>

              <label className="field">
                Coding Model Role
                <span className="field-hint">Model role for coding tasks.</span>
                <input
                  value={provDraft.coding_model || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, coding_model: e.target.value }))}
                  placeholder="fast or openai:gpt-4o-mini"
                />
              </label>

              <label className="field">
                Embedding Model
                <span className="field-hint">Converts text into vectors for semantic search and memory.</span>
                <input
                  value={provDraft.embedding_model || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, embedding_model: e.target.value }))}
                  placeholder="nomic-embed-text"
                />
              </label>

              <label className="field">
                Fallback Provider
                <span className="field-hint">Backup provider if the primary one fails.</span>
                <select
                  value={provDraft.fallback_provider || "ollama"}
                  onChange={(e) => setProvDraft((d) => ({ ...d, fallback_provider: e.target.value }))}
                >
                  <option value="ollama">ollama</option>
                  {CLOUD_PROVIDERS.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Context Window (num_ctx)
                <span className="field-hint">Amount of conversation/context the model can process (4096 recommended on 4 GB VRAM).</span>
                <select value={cfg.num_ctx} onChange={(e) => patchSettings("Context window updated", { num_ctx: Number(e.target.value) })}>
                  {[...new Set([2048, 4096, 8192, cfg.num_ctx])].sort((a, b) => a - b).map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Custom Provider Base URL (OpenAI-compatible)
                <span className="field-hint">Endpoint URL when connecting to a self-hosted or third-party OpenAI-compatible gateway.</span>
                <input
                  value={provDraft.custom_base_url || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, custom_base_url: e.target.value }))}
                  placeholder="https://api.example.com/v1"
                />
              </label>

              <label className="field">
                Custom Model ID
                <span className="field-hint">Exact model identifier served by your custom provider endpoint.</span>
                <input
                  value={provDraft.custom_model || ""}
                  onChange={(e) => setProvDraft((d) => ({ ...d, custom_model: e.target.value }))}
                  placeholder="custom-model-name"
                />
              </label>

              <label className="field">
                Request Timeout (seconds)
                <span className="field-hint">Maximum time Jarvis waits for a provider response (30–45s recommended).</span>
                <input
                  type="number"
                  min={5}
                  max={180}
                  value={timeoutDraft}
                  onChange={(e) => setTimeoutDraft(Number(e.target.value))}
                />
              </label>
            </div>

            <div className="row wrap" style={{ marginTop: 12 }}>
              <button
                className="active"
                onClick={() =>
                  patchSettings("Provider routing settings saved", {
                    providers: provDraft,
                    gateway: { request_timeout_s: timeoutDraft },
                  })
                }
              >
                Save Routing Settings
              </button>
              <button
                aria-pressed={cfg.providers.streaming}
                className="ghost"
                title="Stream reply tokens live instead of waiting for the full completion"
                onClick={() => patchSettings("Streaming updated", { providers: { streaming: !cfg.providers.streaming } })}
              >
                Streaming: {cfg.providers.streaming ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.providers.allow_cloud}
                className="ghost"
                title="Permit routed model calls to cloud providers when keys are configured"
                onClick={() => patchSettings("Cloud policy updated", { providers: { allow_cloud: !cfg.providers.allow_cloud } })}
              >
                Allow Cloud: {cfg.providers.allow_cloud ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.providers.allow_paid_models}
                className="ghost"
                title="Explicit safety lock preventing paid cloud models unless authorized"
                onClick={() => patchSettings("Paid models updated", { providers: { allow_paid_models: !cfg.providers.allow_paid_models } })}
              >
                Paid Models: {cfg.providers.allow_paid_models ? "Authorized" : "Blocked"}
              </button>
              <button
                aria-pressed={cfg.internet.enabled}
                className={cfg.internet.enabled ? "warn" : "ok"}
                title="Toggle Local Mode (offline) vs Online Assisted Mode (web search + external providers)"
                onClick={() =>
                  mutate(
                    cfg.internet.enabled ? "Switched to Local Mode" : "Online Assisted Mode enabled",
                    () => apiFetch("/internet/mode", { method: "POST", body: { enabled: !cfg.internet.enabled } })
                  )
                }
              >
                {cfg.internet.enabled ? "Online Assisted (Switch to Local)" : "Local Mode (Go Online)"}
              </button>
            </div>
          </div>

          <div className="card">
            <h3>Encrypted Cloud API Keys (Windows DPAPI — Never Displayed)</h3>
            <div className="stack-list">
              {CLOUD_PROVIDERS.map((prov) => {
                const configured = Boolean(cfg.provider_keys?.[prov]);
                return (
                  <div key={prov} className="perm-row">
                    <div className="row between wrap">
                      <div className="row">
                        <strong style={{ textTransform: "uppercase" }}>{prov}</strong>
                        <span className={`pill ${configured ? "ok" : ""}`}>
                          {configured ? "Encrypted Key Stored" : "Not Configured"}
                        </span>
                      </div>
                      <div className="row wrap">
                        <input
                          type="password"
                          autoComplete="new-password"
                          value={keyInputs[prov] || ""}
                          onChange={(e) => setKeyInputs((k) => ({ ...k, [prov]: e.target.value }))}
                          placeholder={`Enter ${prov} API key`}
                          style={{ width: 220 }}
                        />
                        <button className="active" onClick={() => saveProviderKey(prov)}>Save Key</button>
                        {configured && (
                          <button
                            className="danger"
                            onClick={() => mutate(`Removed ${prov} key`, () => apiFetch(`/v2/gateway/providers/${prov}/key`, { method: "DELETE" }))}
                          >
                            Clear
                          </button>
                        )}
                        <button
                          className="ghost"
                          onClick={async () => {
                            try {
                              const r = await apiFetch<Record<string, unknown>>(`/v2/gateway/providers/${prov}/health`, { method: "POST" });
                              setHealthOut(r);
                              onNotify(r.ok ? `${prov} healthy` : `${prov}: ${r.error || "check failed"}`);
                            } catch (e) {
                              onNotify((e as Error).message);
                            }
                          }}
                        >
                          Health Check
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
              <div className="row wrap" style={{ marginTop: 8 }}>
                <button
                  className="ghost"
                  onClick={async () => {
                    const r = await apiFetch<Record<string, unknown>>("/v2/gateway/providers/ollama/health", { method: "POST" });
                    setHealthOut(r);
                    onNotify(r.ok ? "Ollama healthy" : "Ollama unavailable");
                  }}
                >
                  Check Ollama Health
                </button>
                <select value={routeRole} onChange={(e) => setRouteRole(e.target.value as typeof routeRole)} style={{ width: 150 }}>
                  <option value="fast">Role: fast</option>
                  <option value="advanced">Role: advanced</option>
                  <option value="coding">Role: coding</option>
                  <option value="embedding">Role: embedding</option>
                </select>
                <button
                  className="active"
                  onClick={async () => {
                    const r = await apiFetch<Record<string, unknown>>("/v2/gateway/route-test", {
                      method: "POST",
                      body: { role: routeRole },
                    });
                    setRouteOut(r);
                    onNotify(`Route selected: ${r.selected || "none"}`);
                  }}
                >
                  Test Gateway Route
                </button>
              </div>
              {healthOut && <pre className="diff">{JSON.stringify(healthOut, null, 2)}</pre>}
              {routeOut && <pre className="diff">{JSON.stringify(routeOut, null, 2)}</pre>}
            </div>
          </div>

          <div className="card">
            <h3>Model Capabilities & Circuit Breakers</h3>
            <div className="stack-list">
              {caps.map((c) => (
                <div key={`${c.provider}:${c.model_id}`} className="line">
                  <div>
                    <strong className="mono">{c.provider}:{c.model_id}</strong>
                    <div className="dim" style={{ fontSize: 11.5 }}>
                      ctx {c.max_ctx} · {c.pricing_tier} · {c.streaming ? "streaming" : "batch"} · {c.tool_calling ? "tools" : "no-tools"}
                    </div>
                  </div>
                  <div className="row">
                    <span className={`pill ${c.key_configured ? "ok" : "warn"}`}>{c.key_configured ? "Ready" : "No Key"}</span>
                    <span className={`pill ${c.breaker_state === "CLOSED" ? "ok" : "danger"}`}>{c.breaker_state}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </SettingsErrorBoundary>
      )}

      {/* SECTION 2: VOICE & AUDIO */}
      {section === "voice" && (
        <SettingsErrorBoundary title="Voice & Audio">
          {/* Recommended Settings Overview */}
          <div className="recommended-banner">
            <div className="row between wrap" style={{ gap: 6 }}>
              <strong>Recommended Settings</strong>
              <span className="pill info">Balanced Local Setup</span>
            </div>
            <div className="row wrap" style={{ gap: 6 }}>
              <span className="chip static">STT Model: small (or base for lower RAM)</span>
              <span className="chip static">Language: Auto-detect</span>
              <span className="chip static">Preset: Balanced</span>
              <span className="chip static">Preprocessing: Light</span>
              <span className="chip static">Translation: Transcribe only</span>
              <span className="chip static">Listening: Continuous / PTT + Suppress Mic During TTS On</span>
            </div>
          </div>

          {/* 2A: SPEECH RECOGNITION */}
          <div className="card">
            <h3>Speech Recognition</h3>
            <p className="field-hint" style={{ marginBottom: 10 }}>
              Converts microphone speech into text locally on your computer.
            </p>
            <div className="grid-2">
              <label className="field">
                STT Provider
                <span className="field-hint">Speech-to-text engine backend (Recommended: faster-whisper Local).</span>
                <select
                  value={cfg.preferences?.stt_provider || "faster-whisper"}
                  onChange={(e) => patchSettings("STT provider saved", { preferences: { stt_provider: e.target.value } })}
                >
                  <option value="faster-whisper">faster-whisper (Local)</option>
                  <option value="whisper-cpp">whisper-cpp</option>
                </select>
              </label>

              <label className="field">
                STT Model
                <span className="field-hint">
                  Model size for speech recognition. Larger models reduce word errors at higher CPU/RAM usage (Recommended: small, or base for lower RAM).
                </span>
                <select
                  value={cfg.stt?.model || "base"}
                  onChange={(e) => patchSettings("STT model changed", { stt: { model: e.target.value } })}
                >
                  {[...new Set([cfg.stt?.model || "base", ...(cfg.stt_models || [])])].map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Language Selection &amp; Auto Language Detection
                <span className="field-hint">
                  Spoken language hint passed to speech recognition. Auto-detect identifies spoken language automatically per clip (Recommended: Auto-detect for multilingual use, or pin English/Malayalam).
                </span>
                <select
                  value={cfg.stt?.language || "auto"}
                  onChange={(e) => patchSettings("STT language changed", { stt: { language: e.target.value } })}
                >
                  {(Array.isArray(cfg.stt_languages)
                    ? cfg.stt_languages.map((l) => [l, l] as [string, string])
                    : Object.entries(cfg.stt_languages || { auto: "Auto-detect", en: "English", ml: "Malayalam", mixed: "Mixed ML+EN" })
                  ).map(([code, label]) => (
                    <option key={code} value={code}>{label} ({code})</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Recognition Quality Preset (Fast / Balanced / High Accuracy)
                <span className="field-hint">
                  Trades speed for decoding beam search and retries: Fast gives quickest reply, Balanced works best overall, High Accuracy runs deeper passes (Recommended: Balanced).
                </span>
                <select
                  value={cfg.stt?.quality || "balanced"}
                  onChange={(e) => patchSettings("Recognition quality updated", { stt: { quality: e.target.value } })}
                >
                  <option value="fast">Fast (low latency)</option>
                  <option value="balanced">Balanced (recommended)</option>
                  <option value="high">High Accuracy (slower beam/retry)</option>
                </select>
              </label>

              <label className="field">
                Audio Preprocessing
                <span className="field-hint">
                  Applies high-pass filtering and loudness normalization before recognition (Recommended: Light; use Off for clean studio audio).
                </span>
                <select
                  value={cfg.stt?.preprocessing_profile || "light"}
                  onChange={(e) =>
                    patchSettings("Preprocessing profile updated", { stt: { preprocessing_profile: e.target.value } })
                  }
                >
                  <option value="off">Off (raw bandlimited 16kHz only)</option>
                  <option value="light">Light (HPF + safe headroom normalize · Recommended)</option>
                  <option value="standard">Standard (+ conservative spectral gate)</option>
                </select>
              </label>

              <label className="field">
                Malayalam Model Availability (Optional Local Checkpoint)
                <span className="field-hint">
                  Optional local specialised Malayalam provider or checkpoint path (e.g. malayalam_whisper or indic_conformer). If not installed locally, Jarvis never auto-downloads weights and falls back cleanly to Faster-Whisper.
                </span>
                <input
                  type="text"
                  defaultValue={cfg.stt?.malayalam_model || ""}
                  placeholder="Optional (leave blank unless local weights are installed)"
                  onBlur={(e) => {
                    const val = e.target.value.trim();
                    if (val !== (cfg.stt?.malayalam_model || "")) {
                      patchSettings("Malayalam ASR model path updated", { stt: { malayalam_model: val } });
                    }
                  }}
                />
              </label>
            </div>

            <label className="field" style={{ marginTop: 10 }}>
              Vocabulary Hints
              <span className="field-hint">
                Comma-separated names, project words, and technical terms so speech recognition spells them accurately (leave default or add your common terms).
              </span>
              <input
                type="text"
                defaultValue={cfg.stt?.vocab_hints || ""}
                onBlur={(e) => {
                  const val = e.target.value.trim();
                  if (val !== (cfg.stt?.vocab_hints || "")) {
                    patchSettings("Vocabulary hints saved", { stt: { vocab_hints: val } });
                  }
                }}
              />
            </label>
          </div>

          {/* 2B: TRANSLATION */}
          <div className="card">
            <h3>Translation</h3>
            <p className="field-hint" style={{ marginBottom: 10 }}>
              Transcription and translation are separate operations: Speech Recognition saves your original transcript verbatim first, and optional Translation converts that text into a second language afterwards without overwriting the original transcript.
            </p>
            <div className="grid-2">
              <label className="field">
                Translation Mode (Transcribe only vs Translate)
                <span className="field-hint">
                  Transcribe only skips translation and keeps spoken text as-is; Auto-translate translates recognized speech into your chosen Target Language (Recommended: Transcribe only unless live translation is needed).
                </span>
                <select
                  value={cfg.translation?.mode || "transcribe_only"}
                  onChange={(e) =>
                    patchSettings("Translation mode updated", {
                      translation: { mode: e.target.value },
                    })
                  }
                >
                  <option value="transcribe_only">Transcribe only (Recommended default)</option>
                  <option value="translate">Auto-translate after recognition</option>
                </select>
              </label>

              <label className="field">
                Target Language
                <span className="field-hint">
                  Destination language for translated text. Source language is supplied automatically by Speech Recognition above (Recommended: None when Transcribe only).
                </span>
                <select
                  value={cfg.translation?.target_language || "none"}
                  onChange={(e) =>
                    patchSettings("Target language updated", {
                      translation: { target_language: e.target.value },
                    })
                  }
                >
                  <option value="none">None</option>
                  <option value="en">English (en)</option>
                  <option value="ml">Malayalam (ml)</option>
                  <option value="hi">Hindi (hi)</option>
                  <option value="ta">Tamil (ta)</option>
                </select>
              </label>
            </div>

            <div className="perm-row" style={{ marginTop: 8 }}>
              <small className="dim">
                <strong>Source language:</strong> Currently follows detected or pinned Speech Language (
                <span className="mono">{cfg.stt?.language || "auto"}</span>). Original, corrected, and translated text are stored independently.
              </small>
            </div>

            <div className="row wrap" style={{ marginTop: 10, gap: 10 }}>
              <button
                aria-pressed={Boolean(cfg.translation?.auto_correct)}
                className="ghost"
                title="Fixes minor punctuation and vocabulary typos while rejecting paraphrases or number/date changes"
                onClick={() =>
                  patchSettings("Transcript post-correction updated", {
                    translation: { auto_correct: !cfg.translation?.auto_correct },
                  })
                }
              >
                Guarded Correction: {cfg.translation?.auto_correct ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.translation?.verify_translations ?? true}
                className="ghost"
                title="Structural check comparing numbers, negations, intent, tech terms, and target script (not semantic proof)"
                onClick={() =>
                  patchSettings("Translation structural verification updated", {
                    translation: { verify_translations: !(cfg.translation?.verify_translations ?? true) },
                  })
                }
              >
                Structural Translation Verification: {(cfg.translation?.verify_translations ?? true) ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.translation?.preserve_mixed_code_switching ?? true}
                className="ghost"
                title="Keeps English technical words intact inside Malayalam or Hindi sentences"
                onClick={() =>
                  patchSettings("Mixed code-switching preservation updated", {
                    translation: {
                      preserve_mixed_code_switching: !(cfg.translation?.preserve_mixed_code_switching ?? true),
                    },
                  })
                }
              >
                Preserve Mixed Code-Switching: {(cfg.translation?.preserve_mixed_code_switching ?? true) ? "On" : "Off"}
              </button>
            </div>
            <p className="field-hint" style={{ marginTop: 6 }}>
              <strong>Guarded Correction</strong> cleans punctuation/vocab only while preserving numbers and meaning (Recommended: Off unless needed).{" "}
              <strong>Structural Translation Verification</strong> checks numbers, negations, tech terms, and target script structurally—it flags issues rather than claiming full semantic proof (Recommended: On).
            </p>
          </div>

          {/* 2C: SPEECH OUTPUT */}
          <div className="card">
            <h3>Speech Output</h3>
            <p className="field-hint" style={{ marginBottom: 10 }}>
              Controls how Jarvis speaks replies aloud using local text-to-speech.
            </p>
            <div className="grid-2">
              <label className="field">
                TTS Provider
                <span className="field-hint">Local text-to-speech synthesizer backend (Recommended: piper).</span>
                <select
                  value={cfg.preferences?.tts_provider || "piper"}
                  onChange={(e) => patchSettings("TTS provider saved", { preferences: { tts_provider: e.target.value } })}
                >
                  <option value="piper">piper (Local Neural)</option>
                </select>
              </label>

              <label className="field">
                Piper Voice
                <span className="field-hint">Installed local voice model used for spoken output (Recommended: en_GB-alan-medium).</span>
                <select
                  value={cfg.tts?.voice || ""}
                  onChange={(e) => patchSettings("TTS voice changed", { tts: { voice: e.target.value } })}
                >
                  {(cfg.voices || []).map((v) => (
                    <option key={v} value={v}>{v}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Speech Rate ({cfg.tts?.speed ?? 1.0}x)
                <span className="field-hint">Playback speed multiplier for spoken responses (Recommended: 1.0x–1.15x).</span>
                <input
                  type="range"
                  min={0.6}
                  max={1.8}
                  step={0.05}
                  value={cfg.tts?.speed ?? 1.0}
                  onChange={(e) => patchSettings("Speech speed updated", { tts: { speed: Number(e.target.value) } })}
                />
              </label>

              <label className="field">
                Speaker Output Device
                <span className="field-hint">
                  Host audio playback device used by Piper TTS (Recommended: System Default Output).
                </span>
                <select
                  value={cfg.tts?.output_device === null || cfg.tts?.output_device === undefined ? "" : String(cfg.tts.output_device)}
                  onChange={(e) =>
                    patchSettings("Output device updated", {
                      tts: { output_device: e.target.value === "" ? null : Number(e.target.value) },
                    })
                  }
                >
                  <option value="">System Default Output</option>
                  {(devices.audio?.outputs || []).map((d) => (
                    <option key={d.index} value={d.index}>{d.index}: {d.name}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="row wrap" style={{ marginTop: 10 }}>
              <button
                aria-pressed={Boolean(cfg.tts?.enabled)}
                onClick={() => patchSettings(cfg.tts?.enabled ? "Spoken replies muted" : "Spoken replies enabled", { tts: { enabled: !cfg.tts?.enabled } })}
              >
                Spoken Replies: {cfg.tts?.enabled ? "On" : "Off"}
              </button>
              <span className="field-hint" style={{ alignSelf: "center" }}>
                Turn On for voice conversations or Off for silent text-only replies.
              </span>
            </div>
          </div>

          {/* 2D: LISTENING */}
          <div className="card">
            <h3>Listening</h3>
            <p className="field-hint" style={{ marginBottom: 10 }}>
              Controls how Jarvis listens to your microphone and handles audio capture.
            </p>
            <div className="grid-2">
              <label className="field">
                Listening Mode (Continuous / Wake Word / Push to Talk)
                <span className="field-hint">
                  Continuous listens hands-free whenever Host Mic is active; Wake word listens after hearing &ldquo;Hey Jarvis&rdquo;; Push to talk records only while triggered (Recommended: Continuous or Push to talk).
                </span>
                <select
                  value={cfg.audio?.voice_mode || "continuous"}
                  onChange={(e) =>
                    patchSettings("Voice mode updated", {
                      audio: { voice_mode: e.target.value },
                      wake: { enabled: e.target.value === "wake_word" },
                    })
                  }
                >
                  <option value="continuous">Continuous listening</option>
                  <option value="wake_word">Wake word triggered</option>
                  <option value="ptt">Push to talk (PTT)</option>
                </select>
              </label>

              <label className="field">
                Microphone Selection (Host Input Device)
                <span className="field-hint">
                  Microphone hardware used by the host voice loop (Recommended: System Default Input).
                </span>
                <select
                  value={cfg.audio?.input_device === null || cfg.audio?.input_device === undefined ? "" : String(cfg.audio.input_device)}
                  onChange={(e) =>
                    patchSettings("Input device updated", {
                      audio: { input_device: e.target.value === "" ? null : Number(e.target.value) },
                    })
                  }
                >
                  <option value="">System Default Input</option>
                  {(devices.audio?.inputs || []).map((d) => (
                    <option key={d.index} value={d.index}>{d.index}: {d.name}</option>
                  ))}
                </select>
              </label>
            </div>

            <div className="row wrap" style={{ marginTop: 12 }}>
              <button
                aria-pressed={Boolean(cfg.audio?.barge_in_enabled)}
                className="ghost"
                title="Allows interrupting Jarvis while it is speaking"
                onClick={() => patchSettings("Barge-in updated", { audio: { barge_in_enabled: !cfg.audio?.barge_in_enabled } })}
              >
                Voice Barge-in: {cfg.audio?.barge_in_enabled ? "On" : "Off"}
              </button>
              <button
                aria-pressed={Boolean(cfg.audio?.suppress_mic_during_tts)}
                className="ghost"
                title="Half-duplex echo prevention: pauses host mic while Jarvis speaks (Recommended: On)"
                onClick={() => patchSettings("Echo suppression updated", { audio: { suppress_mic_during_tts: !cfg.audio?.suppress_mic_during_tts } })}
              >
                Suppress Mic During TTS: {cfg.audio?.suppress_mic_during_tts ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.audio?.prefer_native_sample_rate ?? true}
                className="ghost"
                title="Captures at hardware native rate and resamples with polyphase filter to 16 kHz (Recommended: On)"
                onClick={() =>
                  patchSettings("Native sample rate capture updated", {
                    audio: { prefer_native_sample_rate: !(cfg.audio?.prefer_native_sample_rate ?? true) },
                  })
                }
              >
                Native Mic Sample Rate (Audio Capture): {(cfg.audio?.prefer_native_sample_rate ?? true) ? "On" : "Off"}
              </button>
            </div>
            <p className="field-hint" style={{ marginTop: 6 }}>
              Audio Capture uses polyphase resampling when Native Mic Sample Rate is On, and pauses mic input during TTS when Suppress Mic During TTS is On to prevent speaker bleed/echo (Recommended: both On).
            </p>
          </div>

          {/* 2E: AUDIO & VIDEO DEVICES (SAFE BROWSER & HOST DIAGNOSTICS) */}
          <SettingsErrorBoundary title="Audio & Video Devices">
            <AudioVideoPanel
              hostAudio={devices.audio}
              onRefreshHostDevices={loadAll}
              onNotify={onNotify}
            />
          </SettingsErrorBoundary>
        </SettingsErrorBoundary>
      )}

      {/* SECTION 3: PERMISSIONS & PRIVACY */}
      {section === "permissions" && (
        <SettingsErrorBoundary title="Permissions & Privacy">
          <div className="card">
            <h3>Security & Cloud Privacy Policies</h3>
            <div className="grid-2">
              <label className="field">
                Sensitive File Protection (.env, keys, credentials)
                <select
                  value={cfg.filesystem.sensitive_file_policy || "block"}
                  onChange={(e) =>
                    mutate("Sensitive file policy saved", () =>
                      apiFetch("/v2/filesystem/policy", { method: "PUT", body: { sensitive_file_policy: e.target.value } })
                    )
                  }
                >
                  <option value="block">Block all access & redact listings (Recommended)</option>
                  <option value="metadata_only">Metadata only (never read contents)</option>
                  <option value="warn">Warn before inspection</option>
                </select>
              </label>

              <label className="field">
                Cloud Data Sharing Policy
                <select
                  value={cfg.filesystem.cloud_data_policy || "redacted"}
                  onChange={(e) =>
                    mutate("Cloud privacy policy saved", () =>
                      apiFetch("/v2/filesystem/policy", { method: "PUT", body: { cloud_data_policy: e.target.value } })
                    )
                  }
                >
                  <option value="never">Never send file contents to cloud</option>
                  <option value="redacted">Redacted through PII/secret firewall</option>
                  <option value="approved_only">Only explicitly owner-approved files</option>
                </select>
              </label>
            </div>
            <div className="row wrap" style={{ marginTop: 10 }}>
              <button
                aria-pressed={cfg.filesystem.allow_always_read_only}
                className="ghost"
                onClick={() =>
                  mutate("Policy updated", () =>
                    apiFetch("/v2/filesystem/policy", {
                      method: "PUT",
                      body: { allow_always_read_only: !cfg.filesystem.allow_always_read_only },
                    })
                  )
                }
              >
                Allow "Always allow (read-only)" prompt option: {cfg.filesystem.allow_always_read_only ? "Enabled" : "Disabled"}
              </button>
              {cfg.actions && (
                <>
                  <button
                    aria-pressed={cfg.actions.allow_window_controls}
                    className="ghost"
                    onClick={() => patchSettings("Window controls updated", { actions: { allow_window_controls: !cfg.actions?.allow_window_controls } })}
                  >
                    Window Controls: {cfg.actions.allow_window_controls ? "On" : "Off"}
                  </button>
                  <button
                    aria-pressed={cfg.actions.allow_media_controls}
                    className="ghost"
                    onClick={() => patchSettings("Media controls updated", { actions: { allow_media_controls: !cfg.actions?.allow_media_controls } })}
                  >
                    Media Controls: {cfg.actions.allow_media_controls ? "On" : "Off"}
                  </button>
                  <button
                    aria-pressed={cfg.actions.allow_screenshots}
                    className="ghost"
                    onClick={() => patchSettings("Screenshots setting updated", { actions: { allow_screenshots: !cfg.actions?.allow_screenshots } })}
                  >
                    Screenshots: {cfg.actions.allow_screenshots ? "On" : "Off"}
                  </button>
                  <button
                    aria-pressed={cfg.actions.allow_input_automation}
                    className="ghost"
                    onClick={() => patchSettings("Input automation updated", { actions: { allow_input_automation: !cfg.actions?.allow_input_automation } })}
                    title="High-risk opt-in: every keyboard/mouse action still requires explicit confirmation."
                  >
                    Keyboard/Mouse Opt-In: {cfg.actions.allow_input_automation ? "Enabled (Confirm Req)" : "Disabled"}
                  </button>
                </>
              )}
            </div>
          </div>

          <div className="card">
            <h3>Grant Folder Permission</h3>
            <div className="row wrap" style={{ marginBottom: 8 }}>
              <input
                value={newFolder}
                onChange={(e) => setNewFolder(e.target.value)}
                placeholder="D:\Projects\ERP_V2 or C:\Users\...\Documents"
                style={{ flex: 1, minWidth: 260 }}
              />
              <button className="ghost" onClick={pickFolder}>Pick Folder…</button>
              <select value={newScope} onChange={(e) => setNewScope(e.target.value as typeof newScope)} style={{ width: 150 }}>
                <option value="persistent">Scope: Persistent</option>
                <option value="session">Scope: This Session</option>
              </select>
            </div>
            <div className="row wrap" style={{ marginBottom: 10 }}>
              {ALL_OPS.map((op) => {
                const checked = newOps.includes(op);
                return (
                  <label key={op} className={`chip ${checked ? "" : "off"}`} style={{ cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        setNewOps((prev) => (prev.includes(op) ? prev.filter((x) => x !== op) : [...prev, op]))
                      }
                    />
                    {op}
                  </label>
                );
              })}
            </div>
            <button
              className="active"
              onClick={() =>
                mutate("Folder permission granted", () =>
                  apiFetch("/v2/filesystem/permissions", {
                    method: "POST",
                    body: { folder_path: newFolder, operations: newOps, scope: newScope },
                  })
                ).then(() => setNewFolder(""))
              }
            >
              Add Folder Policy
            </button>
          </div>

          <div className="card">
            <h3>Approved Folders & Temporary Grants ({perms.length})</h3>
            <div className="stack-list">
              {perms.length === 0 ? (
                <p className="dim">No host folders granted yet. Jarvis asks inline when needed.</p>
              ) : (
                perms.map((p) => (
                  <div key={p.permission_id} className="perm-row">
                    <div className="row between wrap">
                      <div>
                        <div className="mono">{p.canonical_folder_path}</div>
                        <div className="row wrap" style={{ marginTop: 4 }}>
                          <span className={`pill ${p.status === "active" ? "ok" : p.status === "consumed" ? "info" : "danger"}`}>
                            {p.status} · {p.scope}
                          </span>
                          {p.granted_operations.map((op) => (
                            <span key={op} className="chip static">{op}</span>
                          ))}
                        </div>
                        <small className="dim">{fmtTime(p.granted_at)}{p.purpose ? ` · ${p.purpose}` : ""}</small>
                      </div>
                      {p.status === "active" && (
                        <div className="row wrap">
                          <button
                            className="ghost"
                            onClick={() =>
                              mutate("Downgraded to Read-Only", () =>
                                apiFetch(`/v2/filesystem/permissions/${p.permission_id}`, {
                                  method: "PUT",
                                  body: { granted_operations: READ_OPS },
                                })
                              )
                            }
                          >
                            Set Read-Only
                          </button>
                          <button
                            className="danger"
                            onClick={() =>
                              mutate("Permission revoked", () =>
                                apiFetch(`/v2/filesystem/permissions/${p.permission_id}/revoke`, { method: "POST" })
                              )
                            }
                          >
                            Revoke
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="card">
            <h3>Filesystem Security & Permission Audit Log</h3>
            <div className="stack-list" style={{ maxHeight: 280, overflowY: "auto" }}>
              {audit.length === 0 ? (
                <p className="dim">No filesystem audit events yet.</p>
              ) : (
                audit.map((ev) => (
                  <div key={ev.id} className="line">
                    <div>
                      <span className={`pill ${ev.decision.includes("block") || ev.decision.includes("denied") ? "danger" : ev.decision === "allowed" || ev.decision === "approved" ? "ok" : "info"}`}>
                        {ev.decision}
                      </span>{" "}
                      <span className="mono">{ev.target}</span>
                    </div>
                    <span className="dim mono">{ev.actor} · {fmtTime(ev.created_at)}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </SettingsErrorBoundary>
      )}

      {/* SECTION 4: SYSTEM & DATA */}
      {section === "system" && (
        <SettingsErrorBoundary title="System & Data">
          <div className="card">
            <h3>Workspace & Display Preferences</h3>
            <div className="grid-2">
              <label className="field">
                HUD Theme
                <select
                  value={cfg.preferences.theme || "dark-hud"}
                  onChange={(e) => patchSettings("Theme updated", { preferences: { theme: e.target.value } })}
                >
                  <option value="dark-hud">Deep Cyan HUD (Default)</option>
                  <option value="midnight-obsidian">Midnight Obsidian</option>
                </select>
              </label>

              <label className="field">
                Graph Detail Level
                <select
                  value={cfg.preferences.graph_detail || "standard"}
                  onChange={(e) => patchSettings("Graph detail updated", { preferences: { graph_detail: e.target.value } })}
                >
                  <option value="compact">Compact</option>
                  <option value="standard">Standard</option>
                  <option value="detailed">Detailed</option>
                </select>
              </label>

              <label className="field">
                History Turns in Prompt
                <span className="field-hint">Recent chat turns kept in each prompt (lower uses less memory).</span>
                <select
                  value={cfg.history_turns}
                  onChange={(e) => patchSettings("History turns updated", { history_turns: Number(e.target.value) })}
                >
                  {[0, 2, 4, 6, 8, 12].map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </label>

              <label className="field">
                Personality Preset
                <span className="field-hint">Response tone only (KV-cache friendly; never weakens security or approvals).</span>
                <select
                  value={cfg.preferences.personality_preset || "professional"}
                  onChange={(e) => patchSettings("Personality preset updated", { preferences: { personality_preset: e.target.value } })}
                >
                  <option value="professional">Professional (Balanced Butler)</option>
                  <option value="friendly">Friendly (Warm & Conversational)</option>
                  <option value="concise">Concise (Short & Direct)</option>
                  <option value="technical">Technical (Engineering Detail)</option>
                  <option value="detailed">Detailed (Step-by-Step)</option>
                  <option value="custom">Custom Instructions</option>
                </select>
              </label>
            </div>
            <div style={{ marginTop: 10 }}>
              <label className="field">
                Custom Style Instructions
                <span className="field-hint">Optional formatting or tone guidance applied alongside safety rules.</span>
                <div className="row" style={{ gap: 8 }}>
                  <input
                    defaultValue={cfg.preferences.custom_instructions || ""}
                    placeholder="e.g. Prefer short bullet points and include verification commands"
                    style={{ flex: 1 }}
                    onBlur={(e) => {
                      const val = e.target.value;
                      if (val !== (cfg.preferences.custom_instructions || "")) {
                        patchSettings("Custom instructions updated", { preferences: { custom_instructions: val } });
                      }
                    }}
                  />
                </div>
              </label>
            </div>
            <div className="row wrap" style={{ marginTop: 12 }}>
              {cfg.proactive && (
                <button
                  aria-pressed={cfg.proactive.enabled}
                  className="ghost"
                  onClick={() => patchSettings("Proactive suggestions updated", { proactive: { enabled: !cfg.proactive?.enabled } })}
                  title="Show dismissible non-executing suggestions on alerts or failed tasks."
                >
                  Proactive HUD Suggestions: {cfg.proactive.enabled ? "On" : "Off"}
                </button>
              )}
              <button
                aria-pressed={cfg.preferences.compact_mode}
                className="ghost"
                onClick={() => patchSettings("Compact mode updated", { preferences: { compact_mode: !cfg.preferences.compact_mode } })}
              >
                Compact Layout: {cfg.preferences.compact_mode ? "On" : "Off"}
              </button>
              <button
                aria-pressed={cfg.preferences.reduced_motion}
                className="ghost"
                onClick={() => patchSettings("Reduced motion updated", { preferences: { reduced_motion: !cfg.preferences.reduced_motion } })}
              >
                Reduced Motion: {cfg.preferences.reduced_motion ? "On" : "Off"}
              </button>
              <button
                className="active"
                onClick={() => mutate("Memory backup created", () => apiFetch("/memory/backup", { method: "POST" }))}
              >
                Export Encrypted Memory Backup
              </button>
            </div>
          </div>

          <div className="grid-2">
            <div className="card">
              <h3>DECISION ENGINE STATUS</h3>
              {decisionDiag ? (
                <div className="stack-list">
                  <div className="line">
                    <span className="dim">Classifier Mode</span>
                    <span className="pill info">{decisionDiag.mode || "heuristic"}</span>
                  </div>
                  <div className="line">
                    <span className="dim">Laya Sidecar</span>
                    <span className="mono">{decisionDiag.enabled ? "Enabled" : "Disabled"}</span>
                  </div>
                  <div className="line">
                    <span className="dim">Policy</span>
                    <span className="mono">{decisionDiag.active_policy?.policy_id || "baseline-default"}</span>
                  </div>
                </div>
              ) : (
                <p className="dim">Unavailable</p>
              )}
            </div>

            <div className="card">
              <h3>INSTALLED SKILLS</h3>
              {(() => {
                const list = skillsDiag?.skills || skillsDiag?.installed || [];
                return list.length ? (
                  <div className="row wrap">
                    {list.slice(0, 12).map((sk, idx) => (
                      <span key={sk.skill_id || sk.name || idx} className={`chip static ${sk.enabled === false ? "off" : ""}`}>
                        {sk.name || sk.skill_id}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="dim">No external agent skills installed. Capabilities are managed automatically by Jarvis.</p>
                );
              })()}
            </div>
          </div>

          <div className="card">
            <h3>System &amp; Database Health Snapshot</h3>
            {overview ? <pre className="diff">{JSON.stringify(overview, null, 2)}</pre> : <p className="dim">Unavailable</p>}
          </div>
        </SettingsErrorBoundary>
      )}
    </div>
  );
};
