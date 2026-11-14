import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export interface ModelPickerInit {
  provider?: string;
  free?: boolean | null;
  task?: string;
  q?: string;
}

interface CatalogModel {
  key: string;
  model: string;
  provider: string;
  free: boolean;
  price_in: number | null;
  price_out: number | null;
  context: number | null;
  inputs: string[];
  tools: boolean | null;
  current: boolean;
  usable: boolean;
  metadata: string;
}
interface ProviderState {
  provider: string;
  status: string;
  count: number;
  stale?: boolean;
  error?: string;
  connected?: boolean;
}
interface Catalog {
  current: { key: string; model: string; provider: string };
  providers: ProviderState[];
  models: CatalogModel[];
  total: number;
  offline_enforced: boolean;
}
interface Recommendation {
  key: string;
  model: string;
  provider: string;
  free: boolean;
  reasons: string[];
  needs_paid_confirmation: boolean;
}
interface SelectResult {
  ok: boolean;
  status: string;
  error?: string;
  pricing?: { input_per_1m_usd: number | null; output_per_1m_usd: number | null };
  current?: { provider: string; model: string };
}

const TASKS = ["", "general", "coding", "translation", "reasoning", "long_documents", "fast", "low_cost", "vision", "speech"];

function price(m: CatalogModel): string {
  if (m.free) return m.provider === "ollama" ? "installed locally" : "no per-token cost";
  if (m.price_in == null) return "paid · price not provided";
  return `$${m.price_in} in / $${m.price_out} out per 1M`;
}

export function ModelPicker({ init, onSwitched, compact }: { init?: ModelPickerInit; onSwitched?: () => void; compact?: boolean }) {
  const [provider, setProvider] = useState(init?.provider || "");
  const [freeF, setFreeF] = useState<string>(init?.free === true ? "free" : init?.free === false ? "paid" : "");
  const [q, setQ] = useState(init?.q || "");
  const [task, setTask] = useState(init?.task || "");
  const [data, setData] = useState<Catalog | null>(null);
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{ key: string; text: string } | null>(null);

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true);
      setErr("");
      try {
        const p = new URLSearchParams();
        if (provider) p.set("provider", provider);
        if (freeF) p.set("free", String(freeF === "free"));
        if (q) p.set("q", q);
        if (refresh) p.set("refresh", "true");
        setData(await apiFetch<Catalog>(`/models/catalog?${p.toString()}`));
        if (task) {
          const r = await apiFetch<{ recommendations: Recommendation[] }>(`/models/recommend?task=${encodeURIComponent(task)}${provider ? `&provider=${provider}` : ""}`);
          setRecs(r.recommendations);
        } else setRecs([]);
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [provider, freeF, q, task]
  );

  useEffect(() => {
    const t = window.setTimeout(() => void load(false), q ? 250 : 0);
    return () => window.clearTimeout(t);
  }, [load, q]);

  const choose = async (key: string, confirmPaid = false) => {
    setMsg(null);
    try {
      const r = await apiFetch<SelectResult>("/models/select", { method: "POST", body: { key, confirm_paid: confirmPaid } });
      if (r.ok) {
        setConfirm(null);
        setMsg({ ok: true, text: `Switched to ${r.current?.provider}:${r.current?.model}. The next message uses it.` });
        await load(false);
        onSwitched?.();
      } else if (r.status === "needs_confirmation") {
        const pr = r.pricing;
        setConfirm({ key, text: `${key} is paid (input ${pr?.input_per_1m_usd ?? "n/a"}, output ${pr?.output_per_1m_usd ?? "n/a"} USD per 1M tokens). Confirm to switch; charges may apply.` });
      } else {
        setMsg({ ok: false, text: r.error || `Could not switch (${r.status}).` });
      }
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  const row = (m: { key: string; model: string; provider: string; free: boolean }, extra?: React.ReactNode, cur?: boolean, usable = true) => (
    <li key={`${m.key}-${extra ? "r" : "m"}`} className="model-row" data-model-key={m.key}>
      <div className="model-row-main">
        <span className="mono">{m.provider}:{m.model}</span>
        {cur && <span className="pill ok">current</span>}
        <span className={`pill ${m.free ? "ok" : "warn"}`}>{m.free ? "free" : "paid"}</span>
        {extra}
      </div>
      <button className={cur ? "active" : "ghost"} disabled={cur || !usable} onClick={() => void choose(m.key)} aria-label={`Use ${m.key}`}>
        {cur ? "Selected" : "Use"}
      </button>
    </li>
  );

  const stale = (data?.providers || []).filter((p) => p.stale || p.error);

  return (
    <div className={`model-picker ${compact ? "compact" : ""}`} role="region" aria-label="Model selector">
      <div className="row wrap model-filters">
        <span className="chip static"><b>Current</b> {data ? `${data.current.provider}:${data.current.model}` : "…"}</span>
        <select aria-label="Provider filter" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">All providers</option>
          <option value="ollama">Local (Ollama)</option>
          <option value="openai">OpenAI</option>
          <option value="openrouter">OpenRouter</option>
        </select>
        <select aria-label="Price filter" value={freeF} onChange={(e) => setFreeF(e.target.value)}>
          <option value="">Free + paid</option>
          <option value="free">Free</option>
          <option value="paid">Paid</option>
        </select>
        <select aria-label="Task for recommendations" value={task} onChange={(e) => setTask(e.target.value)}>
          {TASKS.map((t) => <option key={t} value={t}>{t ? `Recommend: ${t.replace("_", " ")}` : "No recommendation"}</option>)}
        </select>
        <input aria-label="Search models" placeholder="Search models" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="ghost" onClick={() => void load(true)} disabled={loading} aria-label="Refresh catalog">{loading ? "…" : "Refresh"}</button>
      </div>
      {data?.providers.some((p) => p.status === "offline_blocked") && <div className="dim" role="status">Local Mode / offline is enforced: cloud catalogs are not fetched.</div>}
      {stale.map((p) => (
        <div key={p.provider} className="dim" role="status">
          {p.provider}: {p.error || p.status}{p.stale ? " — showing a stale cache, not freshly verified." : ""}
        </div>
      ))}
      {err && <div className="status danger" role="alert">{err}</div>}
      {msg && <div className={`status ${msg.ok ? "ok" : "danger"}`} role="status">{msg.text}</div>}
      {confirm && (
        <div className="status warn" role="alertdialog">
          {confirm.text}
          <div className="row">
            <button onClick={() => void choose(confirm.key, true)}>Confirm paid model</button>
            <button className="ghost" onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        </div>
      )}
      {recs.length > 0 && (
        <div className="model-recs" aria-label="Recommended models">
          <b>Recommended for {task.replace("_", " ")}</b>
          <ul className="model-list">
            {recs.map((r) => row(r, <small className="dim">{r.reasons.join("; ")}</small>, data?.current.key === r.key))}
          </ul>
        </div>
      )}
      <ul className="model-list" aria-label="Available models">
        {(data?.models || []).map((m) =>
          row(
            m,
            <small className="dim">
              {price(m)}
              {m.context ? ` · ${m.context.toLocaleString()} ctx` : ""}
              {m.inputs.length ? ` · ${m.inputs.join("/")}` : ""}
              {m.tools ? " · tools" : ""}
              {!m.usable ? " · not connected" : ""}
            </small>,
            m.current,
            m.usable
          )
        )}
        {data && data.models.length === 0 && <li className="dim">No models match. Check provider connection or refresh.</li>}
      </ul>
      {data && data.total > data.models.length && <div className="dim">Showing {data.models.length} of {data.total}. Narrow with search or filters.</div>}
    </div>
  );
}
