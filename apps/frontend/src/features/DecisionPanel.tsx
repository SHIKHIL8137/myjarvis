import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

interface DecisionStatus {
  enabled: boolean;
  mode: string;
  laya_spec: {
    enabled: boolean;
    mode: string;
    service_mode: string;
    base_url: string;
    checkpoint: string;
    device: string;
    timeout_ms: number;
    max_concurrency: number;
    confidence_threshold: number;
    abstain_enabled: boolean;
    cache_enabled: boolean;
  };
  shadow: {
    total_shadow_runs: number;
    agreements: number;
    disagreements: number;
    laya_errors: number;
    laya_abstained: number;
    recent: Array<Record<string, unknown>>;
    laya?: {
      metrics?: {
        requests: number;
        successes: number;
        failures: number;
        timeouts: number;
        circuit_open: boolean;
        last_latency_ms: number;
      };
    };
  };
  active_policy: {
    policy_id: string;
    mode: string;
    confidence_threshold: number;
    checkpoint: string;
  };
  corrections_count: number;
}

export const DecisionPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [status, setStatus] = useState<DecisionStatus | null>(null);
  const [query, setQuery] = useState("");
  const [routeResult, setRouteResult] = useState<Record<string, unknown> | null>(null);
  const [benchmarkResult, setBenchmarkResult] = useState<Record<string, unknown> | null>(null);
  const [datasetInfo, setDatasetInfo] = useState<Record<string, unknown> | null>(null);
  const [modeSel, setModeSel] = useState("shadow");
  const [checkpointSel, setCheckpointSel] = useState("multilingual");

  const refresh = async () => {
    try {
      const [st, ds] = await Promise.all([
        apiFetch<DecisionStatus>("/v2/decision/status"),
        apiFetch<Record<string, unknown>>("/v2/decision/dataset"),
      ]);
      setStatus(st);
      setDatasetInfo(ds);
      setModeSel(st.laya_spec.mode || "shadow");
      setCheckpointSel(st.laya_spec.checkpoint || "multilingual");
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const layaMetrics = status?.shadow?.laya?.metrics;

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>DECISION ENGINE STATUS & EXPERIMENTAL LAYA CONTROL</h3>
        {status && (
          <>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
              <span className={`pill ${status.enabled ? "warn" : "ok"}`}>
                LAYA: {status.enabled ? "ENABLED (EXPERIMENTAL)" : "DISABLED (HEURISTIC/LLM BASELINE)"}
              </span>
              <span className="pill">ACTIVE MODE: {status.mode.toUpperCase()}</span>
              <span className="pill">CHECKPOINT: {status.laya_spec.checkpoint} ({status.laya_spec.device})</span>
              <span className={`pill ${layaMetrics?.circuit_open ? "danger" : "ok"}`}>
                CIRCUIT: {layaMetrics?.circuit_open ? "OPEN" : "CLOSED"}
              </span>
            </div>
            <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>
              Service: <code>{status.laya_spec.base_url}</code> ({status.laya_spec.service_mode}) | Timeout: {status.laya_spec.timeout_ms}ms | Last Latency: {layaMetrics?.last_latency_ms ?? 0}ms | Errors: {layaMetrics?.failures ?? 0} | Shadow Runs: {status.shadow.total_shadow_runs} (Agree: {status.shadow.agreements}, Disagree: {status.shadow.disagreements})
            </div>
            <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>
              Active Policy: <code>{status.active_policy.policy_id}</code> (threshold={status.active_policy.confidence_threshold}) | Corrections Logged: {status.corrections_count}
            </div>
          </>
        )}
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <select value={modeSel} onChange={(e) => setModeSel(e.target.value)}>
            <option value="disabled">disabled</option>
            <option value="shadow">shadow</option>
            <option value="laya_primary">laya_primary</option>
            <option value="laya_with_heuristic_fallback">laya_with_heuristic_fallback</option>
          </select>
          <select value={checkpointSel} onChange={(e) => setCheckpointSel(e.target.value)}>
            <option value="multilingual">multilingual (mmBERT-base 322M)</option>
            <option value="english">english (ModernBERT-large)</option>
            <option value="typed-decisions">typed-decisions</option>
            <option value="vegaml">vegaml (vega-U 0.8B conformal)</option>
          </select>
          <button
            className="ok"
            onClick={async () => {
              const enable = modeSel !== "disabled";
              await apiFetch("/v2/decision/mode", {
                method: "POST",
                body: { enabled: enable, mode: modeSel, checkpoint: checkpointSel },
              });
              onNotify(`Decision mode updated: ${enable ? modeSel : "disabled"}`);
              refresh();
            }}
          >
            Apply Mode
          </button>
          <button
            className="danger"
            onClick={async () => {
              await apiFetch("/v2/decision/mode", { method: "POST", body: { enabled: false, mode: "disabled" } });
              onNotify("Laya disabled; reverted safely to Heuristic/LLM baseline.");
              refresh();
            }}
          >
            Disable Laya
          </button>
        </div>
      </div>

      <div className="card">
        <h3>TEST DECISION ROUTE & INSPECT SECURITY BOUNDARY</h3>
        <div className="row" style={{ marginBottom: 8 }}>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Enter English, Malayalam, Manglish, or sensitive prompt…" />
          <button
            onClick={async () => {
              if (!query.trim()) return;
              const r = await apiFetch<Record<string, unknown>>("/v2/decision/route", { method: "POST", body: { query: query.trim() } });
              setRouteResult(r);
              refresh();
            }}
          >
            Route Prompt
          </button>
        </div>
        {routeResult && <pre className="diff">{JSON.stringify(routeResult, null, 2)}</pre>}
      </div>

      <div className="card">
        <h3>EVALUATION DATASET & BENCHMARK RUNNER (HELD-OUT TEST SPLIT)</h3>
        <div className="row" style={{ gap: 8, marginBottom: 8 }}>
          <button
            className="ghost"
            onClick={async () => {
              const r = await apiFetch<Record<string, unknown>>("/v2/decision/benchmark", {
                method: "POST",
                body: { split: "test", include_laya: Boolean(status?.enabled) },
              });
              setBenchmarkResult(r);
              onNotify("Held-out test split benchmark complete.");
            }}
          >
            Run Held-Out Benchmark
          </button>
          <button
            className="ghost"
            onClick={async () => {
              const r = await apiFetch<Record<string, unknown>>("/v2/decision/calibration/prepare", {
                method: "POST",
                body: { candidate_threshold: 0.72, checkpoint: checkpointSel, target_mode: modeSel },
              });
              onNotify("Prepared calibration candidate from train+val (test untouched). Check Approvals tab before activation.");
              setBenchmarkResult(r);
            }}
          >
            Prepare Calibration Candidate
          </button>
          <button
            className="warn"
            onClick={async () => {
              if (!window.confirm("Roll back active Decision Engine calibration policy to previous snapshot?")) return;
              try {
                await apiFetch("/v2/decision/calibration/rollback", { method: "POST" });
                onNotify("Decision policy rolled back cleanly.");
                refresh();
              } catch (e) {
                onNotify((e as Error).message);
              }
            }}
          >
            Rollback Policy
          </button>
        </div>
        {datasetInfo && <pre className="diff">{JSON.stringify({ dataset: datasetInfo.counts, leakage_free: datasetInfo.leakage_free, version: datasetInfo.version }, null, 2)}</pre>}
        {benchmarkResult && <pre className="diff" style={{ marginTop: 8 }}>{JSON.stringify(benchmarkResult, null, 2)}</pre>}
      </div>
    </div>
  );
};

