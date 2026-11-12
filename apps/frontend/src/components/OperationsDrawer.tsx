import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { MemoryPanel } from "../features/MemoryPanel";
import { fmtTime, type ShellProps } from "../shell";

export type OpsTab = "history" | "aliases" | "memory" | "desktop" | "system";

interface CommandHistoryItem {
  id: string;
  raw_text: string;
  task_category: string;
  intent: string;
  status: string;
  task_id?: string | null;
  replayable: boolean;
  replay_count: number;
  result_summary?: string;
  error_info?: string;
  created_at: number;
}

interface AliasItem {
  alias: string;
  target_path: string;
  description?: string;
  updated_at?: number;
}

interface ActionSpecItem {
  action_id: string;
  description: string;
  level: string;
  risk_tier: string;
  required_permissions: string[];
  timeout_s: number;
  confirm_required: boolean;
}

interface PendingActionItem {
  token: string;
  action: string;
  level: string;
  description: string;
  status: string;
}

interface SysHealthData {
  cpu_pct?: number | null;
  cpu?: { pct?: number | null; cores_logical?: number; temp_c?: number | null; temp_supported?: boolean; unavailable_reason?: string };
  ram?: { total_gb: number; free_gb: number; used_gb: number; used_pct: number };
  gpu?: { name: string; total_gb: number; used_gb: number; free_gb: number; used_pct: number; util_pct: number; temp_c?: number | null } | null;
  disks?: Array<{ mount: string; total_gb: number; used_gb: number; free_gb: number; used_pct: number; health: string }>;
  battery?: { present: boolean; ac_online?: boolean | null; charge_pct?: number | null; charging?: boolean | null; unavailable_reason?: string | null };
  network?: { hostname: string; addresses: string[]; has_local_interface: boolean };
  processes?: Array<{ name: string; pid: number; mem_mb: number }>;
  alerts?: Array<{ id: string; severity: string; metric: string; message: string }>;
  jarvis?: { os_state?: string; mode?: string; active_brain?: string; active_provider?: string; queued_tasks?: number; failed_tasks?: number };
}

interface OperationsDrawerProps extends ShellProps {
  tab: OpsTab;
  onChangeTab: (t: OpsTab) => void;
  onClose: () => void;
  onRunPrompt?: (prompt: string) => void;
}

export const OperationsDrawer: React.FC<OperationsDrawerProps> = ({
  tab,
  onChangeTab,
  onClose,
  onNotify,
  onInspect,
  onNavigate,
  onRunPrompt,
}) => {
  const [histQuery, setHistQuery] = useState("");
  const [commands, setCommands] = useState<CommandHistoryItem[]>([]);
  const [aliases, setAliases] = useState<AliasItem[]>([]);
  const [newAlias, setNewAlias] = useState("");
  const [newAliasPath, setNewAliasPath] = useState("");
  const [newAliasDesc, setNewAliasDesc] = useState("");
  const [memCats, setMemCats] = useState<{ counts?: Record<string, number>; enabled?: Record<string, boolean> } | null>(null);
  const [actionsReg, setActionsReg] = useState<{ actions: ActionSpecItem[]; pending: PendingActionItem[]; history: Array<Record<string, any>> }>({
    actions: [],
    pending: [],
    history: [],
  });
  const [urlInput, setUrlInput] = useState("https://docs.python.org/3/");
  const [winTitleInput, setWinTitleInput] = useState("");
  const [lastDesktopOut, setLastDesktopOut] = useState<string>("");
  const [sysData, setSysData] = useState<SysHealthData | null>(null);
  const [busy, setBusy] = useState(false);

  const loadTab = useCallback(async () => {
    try {
      if (tab === "history") {
        const qs = histQuery.trim() ? `&q=${encodeURIComponent(histQuery.trim())}` : "";
        const res = await apiFetch<{ commands: CommandHistoryItem[] }>(`/v2/commands/history?limit=25${qs}`);
        setCommands(res.commands || []);
      } else if (tab === "aliases") {
        const res = await apiFetch<{ aliases: AliasItem[] }>("/v2/filesystem/aliases");
        setAliases(res.aliases || []);
      } else if (tab === "memory") {
        const cats = await apiFetch<{ counts: Record<string, number>; enabled: Record<string, boolean> }>("/v2/memory/categories");
        setMemCats(cats);
      } else if (tab === "desktop") {
        const reg = await apiFetch<typeof actionsReg>("/v2/actions/registry?limit=12");
        setActionsReg(reg);
      } else if (tab === "system") {
        const snap = await apiFetch<SysHealthData>("/sysinfo");
        setSysData(snap);
      }
    } catch (e) {
      onNotify((e as Error).message);
    }
  }, [tab, histQuery, onNotify]);

  useEffect(() => {
    loadTab();
  }, [loadTab]);

  const toggleMemoryCategory = async (key: string, current: boolean) => {
    const fieldMap: Record<string, string> = {
      user_preferences: "enable_user_preferences",
      project_memory: "enable_project_memory",
      command_memory: "enable_command_memory",
      conversation_memory: "enable_conversation_memory",
    };
    const field = fieldMap[key];
    if (!field) return;
    try {
      await apiFetch("/v2/settings", { method: "PUT", body: { memory: { [field]: !current } } });
      onNotify(`${key} ${!current ? "enabled" : "disabled"}`);
      loadTab();
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const runDesktopAction = async (action: string, args: Record<string, unknown> = {}) => {
    setBusy(true);
    try {
      const res = await apiFetch<Record<string, any>>("/v2/actions/execute", {
        method: "POST",
        body: { action, args },
      });
      if (res.confirmation_required) {
        onNotify(`Approval required (${res.token}): ${res.description}`);
      } else {
        onNotify(`Action '${action}': ${res.status || "done"}`);
      }
      setLastDesktopOut(JSON.stringify(res, null, 2));
      loadTab();
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="contextual-tasks-panel" aria-label={`Operations Drawer: ${tab}`}>
      <div className="row between wrap" style={{ marginBottom: 8 }}>
        <div className="row wrap" style={{ gap: 6 }}>
          {(["history", "aliases", "memory", "desktop", "system"] as OpsTab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : "ghost"} onClick={() => onChangeTab(t)}>
              {t === "history" && "Command History"}
              {t === "aliases" && "Project Aliases"}
              {t === "memory" && "Memory & Toggles"}
              {t === "desktop" && "Desktop & URL"}
              {t === "system" && "System Monitor"}
            </button>
          ))}
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="ghost" onClick={loadTab}>Refresh</button>
          <button className="ghost icon" onClick={onClose} aria-label="Close operations drawer">✕</button>
        </div>
      </div>

      {tab === "history" && (
        <div>
          <div className="row" style={{ marginBottom: 8, gap: 6 }}>
            <input
              value={histQuery}
              onChange={(e) => setHistQuery(e.target.value)}
              placeholder="Filter command history…"
              style={{ flex: 1 }}
            />
            <button className="ghost" onClick={loadTab}>Search</button>
            <button className="ghost" onClick={() => onRunPrompt?.("continue my task")}>Continue Interrupted</button>
          </div>
          {commands.length === 0 ? (
            <p className="dim">No recorded commands match right now.</p>
          ) : (
            <div className="task-cards-list">
              {commands.map((c) => (
                <div key={c.id} className="task-mini-card">
                  <div className="row between wrap">
                    <div className="row wrap" style={{ gap: 6 }}>
                      <span className={`pill ${c.status === "completed" ? "ok" : c.status === "failed" ? "danger" : "warn"}`}>{c.status}</span>
                      <span className="pill">{c.task_category}</span>
                      {c.replay_count > 0 && <span className="dim mono">replayed ×{c.replay_count}</span>}
                    </div>
                    <div className="row" style={{ gap: 6 }}>
                      <span className="dim mono">{fmtTime(c.created_at)}</span>
                      {c.replayable && (
                        <button
                          className="ghost"
                          onClick={async () => {
                            try {
                              await apiFetch(`/v2/commands/history/${c.id}/replay`, { method: "POST" });
                              onNotify(`Replayed: ${c.raw_text}`);
                              loadTab();
                            } catch (e) {
                              onNotify((e as Error).message);
                            }
                          }}
                        >
                          Replay
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="task-req">{c.raw_text}</div>
                  {c.result_summary && <div className="dim" style={{ fontSize: 11.5 }}>{c.result_summary.slice(0, 180)}</div>}
                  {c.error_info && <div className="err-text" style={{ fontSize: 11 }}>{c.error_info}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "aliases" && (
        <div>
          <div className="grid-2" style={{ marginBottom: 8 }}>
            <input value={newAlias} onChange={(e) => setNewAlias(e.target.value)} placeholder="Alias (e.g. ERP_V2)" />
            <input value={newAliasPath} onChange={(e) => setNewAliasPath(e.target.value)} placeholder="Target folder (e.g. D:\Projects\ERP_V2)" />
          </div>
          <div className="row" style={{ marginBottom: 10, gap: 6 }}>
            <input
              value={newAliasDesc}
              onChange={(e) => setNewAliasDesc(e.target.value)}
              placeholder="Optional description"
              style={{ flex: 1 }}
            />
            <button
              onClick={async () => {
                if (!newAlias.trim() || !newAliasPath.trim()) return;
                try {
                  await apiFetch("/v2/filesystem/aliases", {
                    method: "POST",
                    body: { alias: newAlias.trim(), target_path: newAliasPath.trim(), description: newAliasDesc.trim() },
                  });
                  onNotify(`Saved alias ${newAlias.trim()}`);
                  setNewAlias("");
                  setNewAliasPath("");
                  setNewAliasDesc("");
                  loadTab();
                } catch (e) {
                  onNotify((e as Error).message);
                }
              }}
            >
              Save Alias
            </button>
          </div>
          {aliases.length === 0 ? (
            <p className="dim">No project aliases registered yet. Map names like ERP_V2 or Notes to local directories.</p>
          ) : (
            <div className="task-cards-list">
              {aliases.map((al) => (
                <div key={al.alias} className="task-mini-card row between wrap">
                  <div>
                    <strong>{al.alias}</strong> <span className="mono dim">→ {al.target_path}</span>
                    {al.description && <div className="dim" style={{ fontSize: 11.5 }}>{al.description}</div>}
                  </div>
                  <button
                    className="danger"
                    onClick={async () => {
                      await apiFetch(`/v2/filesystem/aliases/${encodeURIComponent(al.alias)}`, { method: "DELETE" });
                      onNotify(`Deleted alias ${al.alias}`);
                      loadTab();
                    }}
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "memory" && (
        <div>
          {memCats && (
            <div className="card" style={{ marginBottom: 10 }}>
              <div className="row between wrap" style={{ marginBottom: 6 }}>
                <strong>Adaptive Memory Categories</strong>
                <button
                  className="danger"
                  onClick={async () => {
                    const res = await apiFetch<{ cleared_count: number }>("/v2/memory/clear-conversation", { method: "POST" });
                    onNotify(`Cleared ${res.cleared_count} conversation-derived memories`);
                    loadTab();
                  }}
                >
                  Clear Conversation Memory
                </button>
              </div>
              <div className="row wrap" style={{ gap: 6 }}>
                {Object.entries(memCats.counts || {}).map(([cat, cnt]) => {
                  const enabled = memCats.enabled?.[cat] ?? true;
                  const toggleable = cat in (memCats.enabled || {});
                  return (
                    <button
                      key={cat}
                      className={enabled ? "pill ok" : "pill warn"}
                      disabled={!toggleable}
                      onClick={() => toggleable && toggleMemoryCategory(cat, enabled)}
                      title={toggleable ? "Click to toggle category in chat context" : "Always active"}
                    >
                      {cat}: {cnt} {toggleable ? (enabled ? "· On" : "· Off") : ""}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <MemoryPanel onNotify={onNotify} onInspect={onInspect} onNavigate={onNavigate} />
        </div>
      )}

      {tab === "desktop" && (
        <div>
          <div className="row wrap" style={{ gap: 6, marginBottom: 8 }}>
            <button className="ghost" disabled={busy} onClick={() => runDesktopAction("list_windows")}>List Open Windows</button>
            <button className="ghost" disabled={busy} onClick={() => runDesktopAction("get_active_window")}>Active Window</button>
            <button className="ghost" disabled={busy} onClick={() => runDesktopAction("media_control", { command: "play_pause" })}>Media Play/Pause</button>
            <button className="ghost" disabled={busy} onClick={() => runDesktopAction("take_screenshot")}>Take Screenshot</button>
          </div>
          <div className="row wrap" style={{ gap: 6, marginBottom: 8 }}>
            <input value={winTitleInput} onChange={(e) => setWinTitleInput(e.target.value)} placeholder="Window title to focus…" />
            <button className="ghost" disabled={busy || !winTitleInput.trim()} onClick={() => runDesktopAction("focus_window", { title: winTitleInput.trim() })}>
              Focus Window
            </button>
            <input value={urlInput} onChange={(e) => setUrlInput(e.target.value)} placeholder="https://..." style={{ flex: 1 }} />
            <button className="ghost" disabled={busy || !urlInput.trim()} onClick={() => runDesktopAction("open_url", { url: urlInput.trim() })}>
              Open Validated URL
            </button>
          </div>

          {actionsReg.pending.length > 0 && (
            <div className="card" style={{ marginBottom: 8 }}>
              <strong>Pending Action Approvals ({actionsReg.pending.length})</strong>
              {actionsReg.pending.map((p) => (
                <div key={p.token} className="row between wrap" style={{ marginTop: 6 }}>
                  <span className="mono">{p.action} [{p.level}] — {p.description}</span>
                  <div className="row" style={{ gap: 6 }}>
                    <button
                      className="ok"
                      onClick={async () => {
                        const r = await apiFetch<Record<string, any>>(`/v2/actions/${p.token}/confirm`, { method: "POST" });
                        setLastDesktopOut(JSON.stringify(r, null, 2));
                        loadTab();
                      }}
                    >
                      Approve
                    </button>
                    <button
                      className="danger"
                      onClick={async () => {
                        await apiFetch(`/v2/actions/${p.token}/reject`, { method: "POST" });
                        loadTab();
                      }}
                    >
                      Reject
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {lastDesktopOut && <pre className="diff" style={{ maxHeight: 160, marginBottom: 8 }}>{lastDesktopOut}</pre>}

          <div className="card">
            <strong>Action Registry ({actionsReg.actions.length} registered)</strong>
            <div className="task-cards-list" style={{ marginTop: 6 }}>
              {actionsReg.actions.map((a) => (
                <div key={a.action_id} className="row between wrap" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)", padding: "4px 0" }}>
                  <div>
                    <span className="mono"><strong>{a.action_id}</strong></span>{" "}
                    <span className={`pill ${a.risk_tier === "High" ? "danger" : a.risk_tier === "Medium" ? "warn" : "ok"}`}>{a.risk_tier}</span>
                    {a.confirm_required && <span className="pill warn" style={{ marginLeft: 4 }}>Confirm</span>}
                    <div className="dim" style={{ fontSize: 11.5 }}>{a.description}</div>
                  </div>
                  <span className="dim mono">{a.timeout_s}s</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {tab === "system" && sysData && (
        <div>
          {(sysData.alerts?.length || 0) > 0 && (
            <div className="card" style={{ borderColor: "rgba(255,168,76,0.45)", marginBottom: 8 }}>
              <strong>Active Resource Alerts</strong>
              {sysData.alerts!.map((al) => (
                <div key={al.id} className="err-text" style={{ fontSize: 12 }}>
                  [{al.severity}] {al.message}
                </div>
              ))}
            </div>
          )}
          <div className="health-matrix" style={{ marginBottom: 8 }}>
            <div className="health-cell">
              <small className="dim">CPU ({sysData.cpu?.cores_logical ?? "?"} threads)</small>
              <span className="mono">{sysData.cpu_pct !== null && sysData.cpu_pct !== undefined ? `${sysData.cpu_pct}%` : "measuring…"}</span>
              {!sysData.cpu?.temp_supported && <small className="dim">Temp: unavailable</small>}
            </div>
            <div className="health-cell">
              <small className="dim">RAM Usage</small>
              <span className="mono">{sysData.ram ? `${sysData.ram.used_gb}/${sysData.ram.total_gb} GB (${sysData.ram.used_pct}%)` : "—"}</span>
            </div>
            <div className="health-cell">
              <small className="dim">GPU / VRAM</small>
              <span className="mono">
                {sysData.gpu ? `${sysData.gpu.name} · ${sysData.gpu.used_gb}/${sysData.gpu.total_gb} GB${sysData.gpu.temp_c ? ` · ${sysData.gpu.temp_c}°C` : ""}` : "No NVIDIA telemetry"}
              </span>
            </div>
            <div className="health-cell">
              <small className="dim">Battery</small>
              <span className="mono">
                {sysData.battery?.present ? `${sysData.battery.charge_pct}% (${sysData.battery.ac_online ? "AC" : "Battery"})` : "Desktop / No battery"}
              </span>
            </div>
          </div>
          {sysData.disks && sysData.disks.length > 0 && (
            <div className="card" style={{ marginBottom: 8 }}>
              <strong>Storage Drives</strong>
              <div className="row wrap" style={{ gap: 8, marginTop: 4 }}>
                {sysData.disks.map((d) => (
                  <span key={d.mount} className={`chip static ${d.health === "low_space" ? "warn" : ""}`}>
                    {d.mount} {d.free_gb} GB free / {d.total_gb} GB ({d.used_pct}%)
                  </span>
                ))}
              </div>
            </div>
          )}
          {sysData.processes && sysData.processes.length > 0 && (
            <div className="card">
              <strong>Top Local Processes</strong>
              <div className="stack-list" style={{ marginTop: 4 }}>
                {sysData.processes.slice(0, 6).map((p) => (
                  <div key={`${p.pid}-${p.name}`} className="line">
                    <span className="mono">{p.name} (PID {p.pid})</span>
                    <span className="mono dim">{p.mem_mb} MB</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

