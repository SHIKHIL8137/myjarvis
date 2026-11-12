import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { MemoryPanel } from "../features/MemoryPanel";
import { fmtTime, type ShellProps } from "../shell";

export type OpsTab =
  | "tasks"
  | "agents"
  | "research"
  | "routines"
  | "actions"
  | "history"
  | "aliases"
  | "memory"
  | "desktop"
  | "system";

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

interface TaskItem {
  task_id: string;
  user_request?: string;
  goal?: string;
  status: string;
  canonical_status?: string;
  step_count?: number;
  budget_step_limit?: number;
  pause_reason?: string;
  partial_report?: string;
  created_at?: number;
  meta?: Record<string, any>;
}

interface AgentProfileItem {
  name: string;
  display_name?: string;
  description?: string;
  scope?: string;
  privilege_tier?: string;
  max_delegation_depth?: number;
  allowed_tools?: string[];
  denied_tools?: string[];
  model_role_hint?: string;
}

interface ResearchReportItem {
  task_id: string;
  question: string;
  status?: string;
  canonical_status?: string;
  mode?: string;
  summary?: string;
  verified_claim_count?: number;
  disputed_claim_count?: number;
  unverified_claim_count?: number;
  source_count?: number;
  freshness?: Record<string, any>;
  limitations?: string[];
  created_at?: number;
  completed_at?: number;
}

interface ResearchReportDetail {
  task_id: string;
  question: string;
  status?: string;
  canonical_status?: string;
  mode?: string;
  summary?: string;
  markdown?: string;
  claims?: Array<{
    claim_id?: string;
    text: string;
    source_id?: string;
    start_offset?: number;
    end_offset?: number;
    provenance?: string;
    status?: string;
  }>;
  sources?: Array<{
    id?: string;
    path_or_url?: string;
    title?: string;
    trust_tier?: string;
    content_hash?: string;
  }>;
  contradictions?: Array<{
    topic?: string;
    claim_a?: string;
    claim_b?: string;
    explanation?: string;
  }>;
  freshness?: Record<string, any>;
  verification?: Record<string, any>;
  limitations?: string[];
  warnings?: string[];
}

interface RoutineItem {
  id: string;
  title: string;
  description?: string;
  trigger_type: string;
  trigger_spec?: Record<string, any>;
  action_type?: string;
  action_payload?: Record<string, any>;
  enabled: boolean;
  late_policy?: string;
  grace_period_s?: number;
  last_run_at?: number | null;
  next_run_at?: number | null;
  run_count?: number;
}

interface RoutineRunItem {
  id: string;
  routine_id: string;
  status: string;
  started_at: number;
  completed_at?: number | null;
  error?: string | null;
  idempotency_key?: string;
}

interface UndoEntryItem {
  id: string;
  action_type: string;
  target_path: string;
  status: string;
  reversible: boolean;
  before_sha256?: string;
  after_sha256?: string;
  created_at: number;
  expires_at?: number | null;
  reverted_at?: number | null;
}

interface ActionExecutionItem {
  id: string;
  action_type: string;
  target: string;
  permission_level: string;
  status: string;
  verified: boolean;
  summary?: string;
  checks?: string[];
  executed_at: number;
}

interface OperationsDrawerProps extends ShellProps {
  tab: OpsTab;
  onChangeTab: (t: OpsTab) => void;
  onClose: () => void;
  onRunPrompt?: (prompt: string) => void;
}

const TABS: { id: OpsTab; label: string }[] = [
  { id: "tasks", label: "Tasks" },
  { id: "agents", label: "Agents" },
  { id: "research", label: "Research" },
  { id: "routines", label: "Routines" },
  { id: "actions", label: "Actions & Undo" },
  { id: "history", label: "History" },
  { id: "aliases", label: "Aliases" },
  { id: "memory", label: "Memory" },
  { id: "desktop", label: "Desktop" },
  { id: "system", label: "Sysinfo" },
];

export const OperationsDrawer: React.FC<OperationsDrawerProps> = ({
  tab,
  onChangeTab,
  onClose,
  onNotify,
  onInspect,
  onNavigate,
  onRunPrompt,
}) => {
  // Shared & Tab-specific states
  const [busy, setBusy] = useState(false);

  // History & Aliases
  const [histQuery, setHistQuery] = useState("");
  const [commands, setCommands] = useState<CommandHistoryItem[]>([]);
  const [aliases, setAliases] = useState<AliasItem[]>([]);
  const [newAlias, setNewAlias] = useState("");
  const [newAliasPath, setNewAliasPath] = useState("");
  const [newAliasDesc, setNewAliasDesc] = useState("");

  // Memory & Desktop
  const [memCats, setMemCats] = useState<{ counts?: Record<string, number>; enabled?: Record<string, boolean> } | null>(null);
  const [actionsReg, setActionsReg] = useState<{ actions: ActionSpecItem[]; pending: PendingActionItem[]; history: Array<Record<string, any>> }>({
    actions: [],
    pending: [],
    history: [],
  });
  const [urlInput, setUrlInput] = useState("https://docs.python.org/3/");
  const [winTitleInput, setWinTitleInput] = useState("");
  const [lastDesktopOut, setLastDesktopOut] = useState<string>("");

  // System
  const [sysData, setSysData] = useState<SysHealthData | null>(null);

  // Tasks (P1)
  const [tasksList, setTasksList] = useState<TaskItem[]>([]);
  const [taskFilter, setTaskFilter] = useState<string>("all");
  const [steerTexts, setSteerTexts] = useState<Record<string, string>>({});

  // Agents (P3)
  const [profiles, setProfiles] = useState<AgentProfileItem[]>([]);
  const [agentDiagnostics, setAgentDiagnostics] = useState<string[]>([]);

  // Research (P2)
  const [researchReports, setResearchReports] = useState<ResearchReportItem[]>([]);
  const [selectedResearchId, setSelectedResearchId] = useState<string | null>(null);
  const [researchDetail, setResearchDetail] = useState<ResearchReportDetail | null>(null);

  // Routines (P4)
  const [routinesList, setRoutinesList] = useState<RoutineItem[]>([]);
  const [routineRuns, setRoutineRuns] = useState<RoutineRunItem[]>([]);
  const [quietHours, setQuietHours] = useState<Record<string, any>>({});
  const [newRoutineTitle, setNewRoutineTitle] = useState("");
  const [newRoutineTrigger, setNewRoutineTrigger] = useState("every_n_minutes");
  const [newRoutineMins, setNewRoutineMins] = useState("30");
  const [newRoutineAction, setNewRoutineAction] = useState("notification");

  // Actions & Undo (P5)
  const [undoEntries, setUndoEntries] = useState<UndoEntryItem[]>([]);
  const [executions, setExecutions] = useState<ActionExecutionItem[]>([]);

  const loadTab = useCallback(async () => {
    try {
      if (tab === "tasks") {
        const res = await apiFetch<{ tasks: TaskItem[] }>("/v2/tasks?limit=40");
        setTasksList(res.tasks || []);
      } else if (tab === "agents") {
        const res = await apiFetch<{ profiles: AgentProfileItem[]; diagnostics: string[] }>("/v2/agents");
        setProfiles(res.profiles || []);
        setAgentDiagnostics(res.diagnostics || []);
      } else if (tab === "research") {
        const res = await apiFetch<{ reports: ResearchReportItem[] }>("/v2/research?limit=30");
        setResearchReports(res.reports || []);
      } else if (tab === "routines") {
        const res = await apiFetch<{ routines: RoutineItem[]; recent_runs: RoutineRunItem[]; quiet_hours: Record<string, any> }>("/v2/routines");
        setRoutinesList(res.routines || []);
        setRoutineRuns(res.recent_runs || []);
        setQuietHours(res.quiet_hours || {});
      } else if (tab === "actions") {
        const [uRes, eRes, regRes] = await Promise.all([
          apiFetch<{ entries: UndoEntryItem[] }>("/v2/undo?limit=30"),
          apiFetch<{ executions: ActionExecutionItem[] }>("/v2/actions/executions?limit=30"),
          apiFetch<typeof actionsReg>("/v2/actions/registry?limit=12"),
        ]);
        setUndoEntries(uRes.entries || []);
        setExecutions(eRes.executions || []);
        setActionsReg(regRes);
      } else if (tab === "history") {
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

  useEffect(() => {
    if (tab === "research" && selectedResearchId) {
      apiFetch<{ report: ResearchReportDetail }>(`/v2/research/${selectedResearchId}`)
        .then((res) => setResearchDetail(res.report))
        .catch((e) => onNotify((e as Error).message));
    } else {
      setResearchDetail(null);
    }
  }, [tab, selectedResearchId, onNotify]);

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
    <div className="contextual-tasks-panel" aria-label={`Operations Center: ${tab}`}>
      <div className="row between wrap" style={{ marginBottom: 8 }}>
        <div className="row wrap" style={{ gap: 6 }}>
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "active" : "ghost"} onClick={() => onChangeTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="ghost" onClick={loadTab}>Refresh</button>
          <button className="ghost icon" onClick={onClose} aria-label="Close operations drawer">✕</button>
        </div>
      </div>

      {/* ---------------- P1 TASKS TAB ---------------- */}
      {tab === "tasks" && (
        <div>
          <div className="row between wrap" style={{ marginBottom: 8, gap: 6 }}>
            <div className="row wrap" style={{ gap: 4 }}>
              {["all", "active", "scheduled", "paused", "done", "failed"].map((f) => (
                <button
                  key={f}
                  className={taskFilter === f ? "pill active" : "pill ghost"}
                  onClick={() => setTaskFilter(f)}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>
            <span className="dim mono">{tasksList.length} tasks recorded</span>
          </div>

          <div className="task-cards-list">
            {tasksList
              .filter((t) => {
                if (taskFilter === "all") return true;
                const st = (t.canonical_status || t.status || "").toLowerCase();
                if (taskFilter === "active") return ["running", "planning", "queued", "started"].includes(st);
                if (taskFilter === "scheduled") return st === "scheduled";
                if (taskFilter === "paused") return st === "paused" || st === "waiting" || st === "waiting_quota";
                if (taskFilter === "done") return st === "completed" || st === "done";
                if (taskFilter === "failed") return st === "failed" || st === "cancelled" || st === "budget_exhausted";
                return true;
              })
              .map((t) => {
                const st = (t.canonical_status || t.status || "").toLowerCase();
                const isPaused = st === "paused" || st === "waiting" || st === "waiting_quota";
                const isRunning = st === "running" || st === "planning" || st === "queued";
                return (
                  <div key={t.task_id} className="task-mini-card">
                    <div className="row between wrap">
                      <div className="row wrap" style={{ gap: 6 }}>
                        <span className={`pill ${st === "completed" || st === "done" ? "ok" : st === "failed" ? "danger" : isPaused ? "warn" : "info"}`}>
                          {st.toUpperCase()}
                        </span>
                        <span className="mono dim" style={{ fontSize: 11 }}>{t.task_id.slice(0, 10)}…</span>
                        {t.budget_step_limit && (
                          <span className="pill mono" style={{ fontSize: 11 }}>
                            Step {t.step_count || 0}/{t.budget_step_limit}
                          </span>
                        )}
                      </div>
                      <div className="row wrap" style={{ gap: 4 }}>
                        {isRunning && (
                          <button
                            className="warn"
                            onClick={async () => {
                              try {
                                await apiFetch(`/v2/tasks/${t.task_id}/pause`, { method: "POST", body: { reason: "User paused via Operations Center" } });
                                onNotify(`Paused task ${t.task_id.slice(0, 8)}`);
                                loadTab();
                              } catch (e) {
                                onNotify((e as Error).message);
                              }
                            }}
                          >
                            Pause
                          </button>
                        )}
                        {isPaused && (
                          <button
                            className="ok"
                            onClick={async () => {
                              try {
                                await apiFetch(`/v2/tasks/${t.task_id}/resume`, { method: "POST" });
                                onNotify(`Resumed task ${t.task_id.slice(0, 8)}`);
                                loadTab();
                              } catch (e) {
                                onNotify((e as Error).message);
                              }
                            }}
                          >
                            Resume
                          </button>
                        )}
                        {(isRunning || isPaused) && (
                          <button
                            className="danger"
                            onClick={async () => {
                              try {
                                await apiFetch(`/v2/tasks/${t.task_id}/cancel`, { method: "POST" });
                                onNotify(`Cancelled task ${t.task_id.slice(0, 8)}`);
                                loadTab();
                              } catch (e) {
                                onNotify((e as Error).message);
                              }
                            }}
                          >
                            Cancel
                          </button>
                        )}
                      </div>
                    </div>

                    <div className="task-req" style={{ marginTop: 4 }}>{t.user_request || t.goal || "Background task"}</div>

                    {t.pause_reason && (
                      <div className="warn-text" style={{ fontSize: 11.5, marginTop: 2 }}>
                        Paused: {t.pause_reason}
                      </div>
                    )}

                    {t.partial_report && (
                      <div className="dim" style={{ fontSize: 11.5, marginTop: 4, background: "rgba(255,255,255,0.03)", padding: 6, borderRadius: 4 }}>
                        <strong>Partial Report:</strong> {t.partial_report.slice(0, 240)}
                      </div>
                    )}

                    {/* Live steering input for active/paused tasks */}
                    {(isRunning || isPaused) && (
                      <div className="row" style={{ marginTop: 6, gap: 4 }}>
                        <input
                          placeholder="Steer task (e.g. prioritize backend tests)…"
                          value={steerTexts[t.task_id] || ""}
                          onChange={(e) => setSteerTexts({ ...steerTexts, [t.task_id]: e.target.value })}
                          style={{ flex: 1, fontSize: 12 }}
                        />
                        <button
                          className="ghost"
                          disabled={!(steerTexts[t.task_id] || "").trim()}
                          onClick={async () => {
                            const note = steerTexts[t.task_id]?.trim();
                            if (!note) return;
                            try {
                              await apiFetch(`/v2/tasks/${t.task_id}/steer`, { method: "POST", body: { note } });
                              onNotify(`Steer instruction added to ${t.task_id.slice(0, 8)}`);
                              setSteerTexts({ ...steerTexts, [t.task_id]: "" });
                              loadTab();
                            } catch (e) {
                              onNotify((e as Error).message);
                            }
                          }}
                        >
                          Steer
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
          </div>
        </div>
      )}

      {/* ---------------- P3 AGENTS TAB ---------------- */}
      {tab === "agents" && (
        <div>
          <div className="row between wrap" style={{ marginBottom: 8 }}>
            <div>
              <strong>Workforce Agent Profiles</strong>
              <div className="dim" style={{ fontSize: 11.5 }}>
                Role-based agent definitions with deterministic tool filtering & bounded delegation tiers.
              </div>
            </div>
            <button
              className="ghost"
              onClick={async () => {
                try {
                  const res = await apiFetch<{ diagnostics: string[] }>("/v2/agents/reload", { method: "POST" });
                  onNotify(`Reloaded agent profiles. ${res.diagnostics.length} diagnostics.`);
                  loadTab();
                } catch (e) {
                  onNotify((e as Error).message);
                }
              }}
            >
              Reload Profiles
            </button>
          </div>

          {agentDiagnostics.length > 0 && (
            <div className="card" style={{ borderColor: "rgba(255,168,76,0.45)", marginBottom: 8 }}>
              <strong>Agent Profile Loader Diagnostics</strong>
              {agentDiagnostics.map((d, i) => (
                <div key={i} className="warn-text" style={{ fontSize: 11.5 }}>{d}</div>
              ))}
            </div>
          )}

          <div className="task-cards-list">
            {profiles.map((p) => (
              <div key={p.name} className="task-mini-card">
                <div className="row between wrap">
                  <div className="row wrap" style={{ gap: 6 }}>
                    <strong>{p.display_name || p.name}</strong>
                    <span className="pill ok mono">{p.privilege_tier || "EXECUTE"}</span>
                    <span className="pill mono">{p.scope || "builtin"}</span>
                    <span className="dim mono" style={{ fontSize: 11 }}>Max Depth: {p.max_delegation_depth ?? 3}</span>
                  </div>
                  {p.model_role_hint && <span className="dim mono">{p.model_role_hint}</span>}
                </div>
                <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>{p.description}</div>
                {p.allowed_tools && p.allowed_tools.length > 0 && (
                  <div className="row wrap" style={{ gap: 4, marginTop: 6 }}>
                    <span className="dim mono" style={{ fontSize: 11 }}>Allowed tools:</span>
                    {p.allowed_tools.map((tl) => (
                      <span key={tl} className="pill" style={{ fontSize: 10 }}>{tl}</span>
                    ))}
                  </div>
                )}
                {p.denied_tools && p.denied_tools.length > 0 && (
                  <div className="row wrap" style={{ gap: 4, marginTop: 4 }}>
                    <span className="dim mono" style={{ fontSize: 11 }}>Denied tools:</span>
                    {p.denied_tools.map((tl) => (
                      <span key={tl} className="pill warn" style={{ fontSize: 10 }}>{tl}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---------------- P2 RESEARCH TAB ---------------- */}
      {tab === "research" && (
        <div>
          {!selectedResearchId ? (
            <div>
              <div className="row between wrap" style={{ marginBottom: 8 }}>
                <strong>Evidence-Based Deep Research Reports</strong>
                <span className="dim mono">{researchReports.length} reports</span>
              </div>
              <div className="task-cards-list">
                {researchReports.map((r) => (
                  <div
                    key={r.task_id}
                    className="task-mini-card"
                    style={{ cursor: "pointer" }}
                    onClick={() => setSelectedResearchId(r.task_id)}
                  >
                    <div className="row between wrap">
                      <div className="row wrap" style={{ gap: 6 }}>
                        <span className={`pill ${r.canonical_status === "completed" ? "ok" : "warn"}`}>
                          {(r.canonical_status || r.status || "REPORT").toUpperCase()}
                        </span>
                        <span className="pill mono">{r.mode || "local_kb_only"}</span>
                        <span className="dim mono">{fmtTime(r.created_at)}</span>
                      </div>
                      <div className="row wrap" style={{ gap: 6 }}>
                        <span className="pill ok mono" title="Verified claims">{r.verified_claim_count || 0} verified</span>
                        {(r.disputed_claim_count || 0) > 0 && (
                          <span className="pill danger mono">{r.disputed_claim_count} disputed</span>
                        )}
                        <span className="pill mono">{r.source_count || 0} sources</span>
                      </div>
                    </div>
                    <div className="task-req" style={{ marginTop: 4 }}>{r.question}</div>
                    {r.summary && <div className="dim" style={{ fontSize: 11.5, marginTop: 2 }}>{r.summary.slice(0, 180)}…</div>}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div>
              <div className="row between wrap" style={{ marginBottom: 8 }}>
                <button className="ghost" onClick={() => setSelectedResearchId(null)}>← Back to Reports</button>
                <span className="dim mono">{researchDetail?.task_id}</span>
              </div>

              {researchDetail && (
                <div>
                  <div className="card" style={{ marginBottom: 8 }}>
                    <strong>{researchDetail.question}</strong>
                    <div className="row wrap" style={{ gap: 6, marginTop: 4 }}>
                      <span className="pill ok mono">{researchDetail.verification?.verified_claim_count || 0} Claims Verified</span>
                      <span className="pill mono">{researchDetail.sources?.length || 0} Sources Checked</span>
                      <span className="pill mono">Mode: {researchDetail.mode}</span>
                    </div>

                    {researchDetail.summary && (
                      <div style={{ marginTop: 8, fontSize: 12, lineHeight: 1.4 }}>
                        {researchDetail.summary}
                      </div>
                    )}
                  </div>

                  {researchDetail.contradictions && researchDetail.contradictions.length > 0 && (
                    <div className="card" style={{ borderColor: "rgba(255,77,77,0.5)", marginBottom: 8 }}>
                      <strong className="err-text">Cross-Source Contradictions Detected ({researchDetail.contradictions.length})</strong>
                      {researchDetail.contradictions.map((c, idx) => (
                        <div key={idx} style={{ marginTop: 4, fontSize: 11.5 }}>
                          <span className="pill danger" style={{ marginRight: 4 }}>{c.topic || "Conflict"}</span>
                          <span>{c.explanation || `${c.claim_a} VS ${c.claim_b}`}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {researchDetail.freshness && (
                    <div className="card" style={{ marginBottom: 8 }}>
                      <strong>Freshness & Scanning Limitations</strong>
                      <div className="row wrap" style={{ gap: 8, marginTop: 4 }}>
                        <span className="pill mono">Scanned paths: {researchDetail.freshness.scanned_paths_count ?? "—"}</span>
                        <span className="pill mono">Latest Source: {fmtTime(researchDetail.freshness.latest_source_timestamp)}</span>
                      </div>
                      {researchDetail.limitations && researchDetail.limitations.length > 0 && (
                        <ul style={{ margin: "6px 0 0 16px", padding: 0, fontSize: 11 }}>
                          {researchDetail.limitations.map((lim, i) => (
                            <li key={i} className="dim">{lim}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  <div className="card">
                    <strong>Evidence Claims & Verbatim Quote Offset Spans</strong>
                    <div className="task-cards-list" style={{ marginTop: 6 }}>
                      {(researchDetail.claims || []).map((cl, i) => (
                        <div key={i} className="task-mini-card">
                          <div className="row between wrap">
                            <span className={`pill ${cl.status === "verified" ? "ok" : cl.status === "disputed" ? "danger" : "warn"}`}>
                              {(cl.status || "claim").toUpperCase()}
                            </span>
                            <div className="row" style={{ gap: 6 }}>
                              <span className="pill mono" style={{ fontSize: 10 }}>Provenance: {cl.provenance || "claim"}</span>
                              {cl.start_offset !== undefined && cl.end_offset !== undefined && (
                                <span className="mono dim" style={{ fontSize: 10 }}>
                                  Offset [{cl.start_offset}:{cl.end_offset}]
                                </span>
                              )}
                            </div>
                          </div>
                          <div style={{ fontSize: 12, marginTop: 4 }}>"{cl.text}"</div>
                          {cl.source_id && <div className="dim mono" style={{ fontSize: 10.5, marginTop: 2 }}>Source: {cl.source_id}</div>}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ---------------- P4 ROUTINES TAB ---------------- */}
      {tab === "routines" && (
        <div>
          {quietHours.enabled && (
            <div className="card" style={{ borderColor: "rgba(100,181,246,0.4)", marginBottom: 8 }}>
              <div className="row between wrap">
                <strong>Quiet Hours Active Policy</strong>
                <span className="pill info mono">
                  {quietHours.start_time || "22:00"} – {quietHours.end_time || "07:00"} ({quietHours.timezone || "local"})
                </span>
              </div>
              <div className="dim" style={{ fontSize: 11.5, marginTop: 2 }}>
                Auditory speech & non-critical notifications are suppressed during quiet hours.
              </div>
            </div>
          )}

          {/* Create routine form */}
          <div className="card" style={{ marginBottom: 8 }}>
            <strong>Create Scheduled Routine</strong>
            <div className="grid-2" style={{ marginTop: 6, marginBottom: 6 }}>
              <input
                placeholder="Routine title (e.g. Daily Workspace Backup)"
                value={newRoutineTitle}
                onChange={(e) => setNewRoutineTitle(e.target.value)}
              />
              <div className="row" style={{ gap: 6 }}>
                <select value={newRoutineTrigger} onChange={(e) => setNewRoutineTrigger(e.target.value)} style={{ flex: 1 }}>
                  <option value="every_n_minutes">Every N Minutes</option>
                  <option value="cron">Cron Expression</option>
                  <option value="startup">On System Startup</option>
                  <option value="once">Once</option>
                </select>
                {newRoutineTrigger === "every_n_minutes" ? (
                  <input
                    placeholder="Mins"
                    type="number"
                    style={{ width: 70 }}
                    value={newRoutineMins}
                    onChange={(e) => setNewRoutineMins(e.target.value)}
                  />
                ) : (
                  <input
                    placeholder="0 9 * * *"
                    style={{ width: 100 }}
                    value={newRoutineMins}
                    onChange={(e) => setNewRoutineMins(e.target.value)}
                  />
                )}
              </div>
            </div>
            <div className="row between wrap">
              <div className="row wrap" style={{ gap: 6 }}>
                <select value={newRoutineAction} onChange={(e) => setNewRoutineAction(e.target.value)}>
                  <option value="notification">Notification</option>
                  <option value="chat_command">Chat Command</option>
                  <option value="system_health_check">System Health Check</option>
                </select>
              </div>
              <button
                disabled={!newRoutineTitle.trim()}
                onClick={async () => {
                  try {
                    const spec =
                      newRoutineTrigger === "every_n_minutes"
                        ? { interval_seconds: parseInt(newRoutineMins, 10) * 60 }
                        : newRoutineTrigger === "cron"
                        ? { cron_expression: newRoutineMins }
                        : {};
                    await apiFetch("/v2/routines", {
                      method: "POST",
                      body: {
                        title: newRoutineTitle.trim(),
                        trigger_type: newRoutineTrigger,
                        trigger_spec: spec,
                        action_type: newRoutineAction,
                        action_payload: { message: newRoutineTitle.trim() },
                        late_policy: "run_once_if_within_grace",
                      },
                    });
                    onNotify(`Created routine '${newRoutineTitle.trim()}'`);
                    setNewRoutineTitle("");
                    loadTab();
                  } catch (e) {
                    onNotify((e as Error).message);
                  }
                }}
              >
                Add Routine
              </button>
            </div>
          </div>

          <div className="task-cards-list">
            {routinesList.map((r) => (
              <div key={r.id} className="task-mini-card">
                <div className="row between wrap">
                  <div className="row wrap" style={{ gap: 6 }}>
                    <span className={`pill ${r.enabled ? "ok" : "warn"}`}>{r.enabled ? "ENABLED" : "PAUSED"}</span>
                    <strong>{r.title}</strong>
                    <span className="pill mono">{r.trigger_type}</span>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button
                      className="ghost"
                      onClick={async () => {
                        try {
                          await apiFetch(`/v2/routines/${r.id}/run-now`, { method: "POST" });
                          onNotify(`Triggered routine: ${r.title}`);
                          loadTab();
                        } catch (e) {
                          onNotify((e as Error).message);
                        }
                      }}
                    >
                      Run Now
                    </button>
                    <button
                      className={r.enabled ? "danger" : "ok"}
                      onClick={async () => {
                        try {
                          await apiFetch(`/v2/routines/${r.id}/toggle`, { method: "POST", body: { enabled: !r.enabled } });
                          onNotify(`Routine ${!r.enabled ? "enabled" : "disabled"}`);
                          loadTab();
                        } catch (e) {
                          onNotify((e as Error).message);
                        }
                      }}
                    >
                      {r.enabled ? "Disable" : "Enable"}
                    </button>
                  </div>
                </div>

                <div className="row between wrap" style={{ marginTop: 4, fontSize: 11 }}>
                  <span className="dim">Next run: <span className="mono">{fmtTime(r.next_run_at)}</span></span>
                  <span className="dim">Last run: <span className="mono">{fmtTime(r.last_run_at)}</span></span>
                  <span className="dim mono">Runs: {r.run_count || 0}</span>
                </div>
              </div>
            ))}
          </div>

          {routineRuns.length > 0 && (
            <div className="card" style={{ marginTop: 10 }}>
              <strong>Recent Routine Execution Journal</strong>
              <div className="task-cards-list" style={{ marginTop: 6 }}>
                {routineRuns.slice(0, 8).map((rn) => (
                  <div key={rn.id} className="row between wrap" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)", padding: "4px 0" }}>
                    <div className="row wrap" style={{ gap: 6 }}>
                      <span className={`pill ${rn.status === "completed" ? "ok" : "danger"}`}>{rn.status}</span>
                      <span className="mono dim" style={{ fontSize: 11 }}>{fmtTime(rn.started_at)}</span>
                      {rn.idempotency_key && <span className="dim mono" style={{ fontSize: 10 }}>{rn.idempotency_key}</span>}
                    </div>
                    {rn.error && <span className="err-text" style={{ fontSize: 11 }}>{rn.error}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ---------------- P5 ACTIONS & UNDO TAB ---------------- */}
      {tab === "actions" && (
        <div>
          {/* Undo Journal Section */}
          <div className="card" style={{ marginBottom: 8 }}>
            <div className="row between wrap">
              <strong>Undo Journal (Reversible Snapshots)</strong>
              <span className="dim mono">{undoEntries.length} entries</span>
            </div>
            <div className="dim" style={{ fontSize: 11.5, marginTop: 2 }}>
              Revert actions using pre-execution byte snapshots with strict external modification conflict detection.
            </div>

            <div className="task-cards-list" style={{ marginTop: 6 }}>
              {undoEntries.length === 0 ? (
                <p className="dim" style={{ fontSize: 12 }}>No reversible action snapshots recorded yet.</p>
              ) : (
                undoEntries.map((u) => {
                  const isAvail = u.status === "available";
                  return (
                    <div key={u.id} className="task-mini-card">
                      <div className="row between wrap">
                        <div className="row wrap" style={{ gap: 6 }}>
                          <span className={`pill ${isAvail ? "ok" : u.status === "reverted" ? "info" : "danger"}`}>
                            {u.status.toUpperCase()}
                          </span>
                          <strong className="mono">{u.action_type}</strong>
                          <span className="dim mono" style={{ fontSize: 11 }}>{fmtTime(u.created_at)}</span>
                        </div>
                        {isAvail && (
                          <button
                            className="danger"
                            onClick={async () => {
                              try {
                                await apiFetch(`/v2/undo/${u.id}`, { method: "POST" });
                                onNotify(`Successfully reverted action on: ${u.target_path}`);
                                loadTab();
                              } catch (e) {
                                onNotify(`Undo failed: ${(e as Error).message}`);
                              }
                            }}
                          >
                            Revert / Undo
                          </button>
                        )}
                      </div>
                      <div className="mono" style={{ fontSize: 11.5, marginTop: 4, wordBreak: "break-all" }}>
                        Target: {u.target_path || "—"}
                      </div>
                      {u.before_sha256 && (
                        <div className="dim mono" style={{ fontSize: 10, marginTop: 2 }}>
                          SHA256: {u.before_sha256.slice(0, 10)}… → {u.after_sha256 ? `${u.after_sha256.slice(0, 10)}…` : "none"}
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Action Execution History Section */}
          <div className="card">
            <div className="row between wrap">
              <strong>Action Execution Audit Log</strong>
              <span className="dim mono">{executions.length} logged</span>
            </div>
            <div className="task-cards-list" style={{ marginTop: 6 }}>
              {executions.map((x) => (
                <div key={x.id} className="row between wrap" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)", padding: "4px 0" }}>
                  <div>
                    <div className="row wrap" style={{ gap: 6 }}>
                      <span className={`pill ${x.status === "done" ? "ok" : x.status === "denied" ? "warn" : "danger"}`}>
                        {x.status.toUpperCase()}
                      </span>
                      <strong className="mono">{x.action_type}</strong>
                      <span className="pill mono">{x.permission_level}</span>
                      {x.verified && <span className="pill ok" style={{ fontSize: 10 }}>VERIFIED</span>}
                    </div>
                    <div className="mono dim" style={{ fontSize: 11, marginTop: 2 }}>{x.target}</div>
                    {x.summary && <div className="dim" style={{ fontSize: 11 }}>{x.summary}</div>}
                  </div>
                  <span className="dim mono" style={{ fontSize: 10 }}>{fmtTime(x.executed_at)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ---------------- HISTORY TAB ---------------- */}
      {tab === "history" && (
        <div>
          <div className="row" style={{ marginBottom: 8, gap: 6 }}>
            <input
              value={histQuery}
              onChange={(e) => setHistQuery(e.target.value)}
              placeholder="Search past commands, tasks, or file targets…"
              style={{ flex: 1 }}
            />
            <button className="ghost" onClick={loadTab}>Search</button>
          </div>
          {commands.length === 0 ? (
            <p className="dim">No matching commands found in unified command history.</p>
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

      {/* ---------------- ALIASES TAB ---------------- */}
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

      {/* ---------------- MEMORY TAB ---------------- */}
      {tab === "memory" && (
        <div>
          {memCats && (
            <div className="card" style={{ marginBottom: 8 }}>
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

      {/* ---------------- DESKTOP TAB ---------------- */}
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

      {/* ---------------- SYSTEM TAB ---------------- */}
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
