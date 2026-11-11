import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

interface Capability {
  provider: string;
  model_id: string;
  local: boolean;
  streaming: boolean;
  tool_calling: boolean;
  json_mode: boolean;
  vision: boolean;
  max_ctx: number;
  pricing_tier: string;
  breaker_state: string;
  key_configured: boolean;
  paid_authorized: boolean;
}

export const GatewayPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [caps, setCaps] = useState<Capability[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown>>({});
  const [policy, setPolicy] = useState<{ internet_mode?: string; allow_cloud?: boolean; allow_paid_models?: boolean }>({});

  const refresh = async () => {
    try {
      const res = await apiFetch<{ capabilities: Capability[]; usage: Record<string, unknown>; internet_mode: string; allow_cloud: boolean; allow_paid_models: boolean }>("/v2/gateway/capabilities");
      setCaps(res.capabilities || []);
      setUsage(res.usage || {});
      setPolicy({ internet_mode: res.internet_mode, allow_cloud: res.allow_cloud, allow_paid_models: res.allow_paid_models });
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="scroll-body">
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>MODEL GATEWAY CAPABILITY REGISTRY</h3>
          <button className="ghost" onClick={refresh}>Refresh</button>
        </div>
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="pill">{policy.internet_mode || "local"} mode</span>
          <span className={`pill ${policy.allow_cloud ? "warn" : "ok"}`}>Cloud: {policy.allow_cloud ? "ENABLED" : "DISABLED"}</span>
          <span className={`pill ${policy.allow_paid_models ? "warn" : "ok"}`}>Paid Models: {policy.allow_paid_models ? "AUTHORIZED" : "BLOCKED"}</span>
        </div>
        {caps.map((c, idx) => (
          <div key={`${c.provider}-${c.model_id}-${idx}`} style={{ padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <div>
                <strong>{c.provider}:{c.model_id}</strong>{" "}
                <span className={`pill ${c.local ? "ok" : c.pricing_tier === "paid" ? "danger" : "warn"}`}>
                  {c.local ? "LOCAL" : `CLOUD (${c.pricing_tier})`}
                </span>
              </div>
              <div className="row">
                <span className={`pill ${c.breaker_state === "CLOSED" ? "ok" : "danger"}`}>Breaker: {c.breaker_state}</span>
                {!c.local && <span className="pill">{c.key_configured ? "KEY SET" : "NO KEY"}</span>}
                {c.pricing_tier === "paid" && <span className="pill">{c.paid_authorized ? "AUTH" : "BLOCKED"}</span>}
              </div>
            </div>
            <div className="dim">
              ctx={c.max_ctx} | stream={String(c.streaming)} | tools={String(c.tool_calling)} | json={String(c.json_mode)} | vision={String(c.vision)}
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>GATEWAY TELEMETRY, FALLBACKS & HARDWARE GOVERNOR</h3>
        <pre className="diff">{JSON.stringify(usage, null, 2)}</pre>
      </div>
    </div>
  );
};

