import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

interface ManifestItem {
  id: string;
  tier: string;
  bundle_path: string;
  size_bytes: number;
  verified_restore: boolean;
  created_at?: number;
  details?: {
    tier3_copy?: string | null;
    remote_uri?: string | null;
    snapshot_counts?: Record<string, number>;
  };
}

interface DbTargetItem {
  target_id: string;
  role: string;
  driver: string;
  host: string;
  port: number;
  database: string;
  fingerprint: string;
  is_current_live?: boolean;
}

export const RecoveryPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [manifests, setManifests] = useState<ManifestItem[]>([]);
  const [steps, setSteps] = useState<string[]>([]);
  const [targets, setTargets] = useState<DbTargetItem[]>([]);
  const [pass, setPass] = useState("");
  const [lastDrill, setLastDrill] = useState<Record<string, unknown> | null>(null);
  const [modeOnline, setModeOnline] = useState(false);

  const refresh = async () => {
    try {
      const [m, net, inst, tgts] = await Promise.all([
        apiFetch<{ manifests: ManifestItem[] }>("/v2/recovery/manifests"),
        apiFetch<{ online_enabled: boolean }>("/internet"),
        apiFetch<{ steps: string[] }>("/v2/recovery/instructions"),
        apiFetch<{ targets: DbTargetItem[] }>("/v2/recovery/targets"),
      ]);
      setManifests(m.manifests || []);
      setModeOnline(Boolean(net.online_enabled));
      setSteps(inst.steps || []);
      setTargets(tgts.targets || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const latest = manifests[0];

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>NETWORK POLICY</h3>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span>Active Internet Policy: <strong>{modeOnline ? "Online Assisted Mode" : "Local Mode (Strict Offline)"}</strong></span>
          <button
            className={modeOnline ? "danger" : "ok"}
            onClick={async () => {
              await apiFetch("/internet/mode", { method: "POST", body: { enabled: !modeOnline } });
              refresh();
            }}
          >
            {modeOnline ? "Enforce Local Offline" : "Enable Online Assisted"}
          </button>
        </div>
      </div>

      <div className="card">
        <h3>3-2-1 ENCRYPTED DISASTER RECOVERY</h3>
        {latest && (
          <div className="row" style={{ flexWrap: "wrap", marginBottom: 8 }}>
            <span className="pill">Last Backup: {latest.created_at ? new Date(latest.created_at * 1000).toLocaleString() : "n/a"}</span>
            <span className={`pill ${latest.verified_restore ? "ok" : "warn"}`}>Integrity: {latest.verified_restore ? "VERIFIED" : "UNVERIFIED"}</span>
            {latest.details?.tier3_copy && <span className="pill ok">Secondary: synced</span>}
            {latest.details?.snapshot_counts && <span className="pill">Vectors: {latest.details.snapshot_counts.vectors ?? 0}</span>}
          </div>
        )}
        <div className="row" style={{ marginBottom: 8 }}>
          <input type="password" value={pass} onChange={(e) => setPass(e.target.value)} placeholder="Backup vault passphrase (min 8 chars)" />
          <button
            onClick={async () => {
              if (pass.length < 8) return onNotify("Passphrase must be at least 8 characters");
              try {
                const res = await apiFetch<{ bundle_path: string }>("/v2/recovery/backup", { method: "POST", body: { tier: "tier2_warm", passphrase: pass } });
                onNotify(`Created encrypted backup: ${res.bundle_path}`);
                refresh();
              } catch (e) {
                onNotify((e as Error).message);
              }
            }}
          >
            Create Encrypted Backup
          </button>
        </div>
        {manifests.map((b) => (
          <div key={b.id} className="row" style={{ justifyContent: "space-between", padding: "5px 0" }}>
            <div>
              <strong>[{b.tier}]</strong> {(b.size_bytes / 1024).toFixed(1)} KB{" "}
              <span className={`pill ${b.verified_restore ? "ok" : "warn"}`}>{b.verified_restore ? "VERIFIED" : "UNVERIFIED"}</span>
              <div className="dim">{b.bundle_path}</div>
            </div>
            <button
              onClick={async () => {
                if (pass.length < 8) return onNotify("Enter vault passphrase to verify restore");
                try {
                  const r = await apiFetch<Record<string, unknown>>("/v2/recovery/verify", {
                    method: "POST",
                    body: { bundle_path: b.bundle_path, passphrase: pass },
                  });
                  setLastDrill(r);
                  onNotify(`Drill OK: ${r.verified_files} files, RTO ${r.rto_seconds}s`);
                  refresh();
                } catch (e) {
                  onNotify((e as Error).message);
                }
              }}
            >
              Verify Drill
            </button>
          </div>
        ))}
        {lastDrill && <pre className="diff" style={{ marginTop: 8 }}>{JSON.stringify(lastDrill, null, 2)}</pre>}
      </div>

      <div className="card">
        <h3>DATABASE TARGET REGISTRY (PRODUCTION PROTECTION)</h3>
        {targets.map((t) => (
          <div key={`${t.target_id}-${t.fingerprint}`} className="row" style={{ justifyContent: "space-between", padding: "3px 0" }}>
            <span className="dim"><code>{t.fingerprint}</code></span>
            <div className="row">
              <span className={`pill ${t.role === "prod_pg" ? "danger" : t.role === "scratch_pg" ? "ok" : "warn"}`}>{t.role}</span>
              {t.is_current_live && <span className="pill ok">LIVE ACTIVE</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>NEW-MACHINE RECOVERY WORKFLOW</h3>
        <div className="dim" style={{ lineHeight: 1.5 }}>
          {steps.map((s, idx) => <div key={idx}>{s}</div>)}
        </div>
      </div>
    </div>
  );
};
