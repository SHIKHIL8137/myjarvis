import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export const RecoveryPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [manifests, setManifests] = useState<Array<{ id: string; tier: string; bundle_path: string; size_bytes: number; verified_restore: boolean }>>([]);
  const [usage, setUsage] = useState<Record<string, unknown>>({});
  const [pass, setPass] = useState("");
  const [modeOnline, setModeOnline] = useState(false);

  const refresh = async () => {
    try {
      const [m, u, net] = await Promise.all([
        apiFetch<{ manifests: typeof manifests }>("/v2/recovery/manifests"),
        apiFetch<Record<string, unknown>>("/v2/gateway/usage"),
        apiFetch<{ online_enabled: boolean }>("/internet"),
      ]);
      setManifests(m.manifests || []);
      setUsage(u);
      setModeOnline(Boolean(net.online_enabled));
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
        <h3>NETWORK SECURITY & MODEL GATEWAY GOVERNANCE</h3>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
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
        <pre className="diff">{JSON.stringify(usage, null, 2)}</pre>
      </div>

      <div className="card">
        <h3>3-2-1 ENCRYPTED DISASTER RECOVERY</h3>
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
                  const r = await apiFetch<{ verified_files: number }>("/v2/recovery/verify", {
                    method: "POST",
                    body: { bundle_path: b.bundle_path, passphrase: pass },
                  });
                  onNotify(`Verified ${r.verified_files} encrypted files!`);
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
      </div>
    </div>
  );
};

