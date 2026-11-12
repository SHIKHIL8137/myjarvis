import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { BrainExplorer } from "../components/BrainExplorer";
import type { ForceConfig, GalaxyHandle } from "../components/GalaxyCanvas";
import { HologramCore, type CoreMode } from "../components/HologramCore";
import { Inspector } from "../components/Inspector";
import { Markdown } from "../components/Markdown";
import { graph, useGraphVersion } from "../graphStore";
import { onEvent, type ConnState, type LiveEvent } from "../live";
import { fmtTime, type Inspectable, type ShellProps } from "../shell";
import { GraphView } from "./GraphView";
import type { SystemOverview, SystemStatus } from "./OverviewPanel";

export interface FsPermissionRequest {
  request_id: string;
  task_id?: string | null;
  requested_path: string;
  canonical_path: string;
  requested_operations: string[];
  not_requested_operations: string[];
  sensitive_flags: string[];
  reason: string;
  access_duration?: string;
  status: string;
  permission_id?: string | null;
}

export interface OwnerApprovalRequest {
  token: string;
  action_type: string;
  target_resource: string;
  preview_diff?: string;
  risk_tier?: string;
  status?: string;
}

export interface AssistantTaskItem {
  task_id: string;
  user_request: string;
  source: string;
  intent: string;
  status: string;
  plan: string[];
  selected_capabilities: string[];
  selected_agents: string[];
  tools_used: string[];
  permissions_requested: string[];
  current_step: string;
  result: string;
  generated_files: string[];
  error_info: string;
  created_at: number;
  updated_at: number;
  completed_at?: number | null;
}

export interface ChatFeedItem {
  id: string;
  role: "user" | "assistant";
  text: string;
  streaming?: boolean;
  stopped?: boolean;
  error?: string;
  command?: string | null;
  decision?: { department?: string; intent?: string; workflow?: string; agent_role?: string };
  tools?: string[];
  sources?: string[];
  memories?: number;
  knowledge?: string[];
  web?: Array<{ title?: string; url: string }>;
  paths?: string[];
  permissionRequest?: FsPermissionRequest;
  approvalRequest?: OwnerApprovalRequest;
  task?: AssistantTaskItem | null;
  stats?: string;
  resolvedForRequestId?: string;
  resolvedForToken?: string;
}

interface AssistantHomeProps extends ShellProps {
  assistantState: string;
  stateDetail: string;
  audioLevel: number;
  voiceLoopState: { state: string; mic: boolean; mode?: string; error?: string | null };
  status: SystemStatus | null;
  overview: SystemOverview | null;
  backendDown: boolean;
  live: ConnState;
  feed: ChatFeedItem[];
  setFeed: React.Dispatch<React.SetStateAction<ChatFeedItem[]>>;
  showGraphStage: boolean;
  onToggleGraphStage: (next: boolean) => void;
  inspect: Inspectable | null;
  forces: ForceConfig;
  onChangeForces: (next: ForceConfig) => void;
  canvasRef: React.RefObject<GalaxyHandle>;
  searchInput: string;
  searchQuery: string;
  onSearchInput: (v: string) => void;
}

const READ_OP_SET = new Set(["list_directory", "read_file", "search_files", "read_metadata"]);

function taskTone(status: string): "ok" | "warn" | "danger" | "info" | "" {
  if (status === "completed") return "ok";
  if (status === "running" || status === "planning" || status === "queued") return "info";
  if (status === "awaiting_permission" || status === "awaiting_approval" || status === "paused") return "warn";
  if (status === "failed" || status === "cancelled") return "danger";
  return "";
}

export const AssistantHome: React.FC<AssistantHomeProps> = ({
  onNotify,
  onInspect,
  onNavigate,
  assistantState,
  stateDetail,
  audioLevel,
  voiceLoopState,
  status,
  overview,
  backendDown,
  live,
  feed,
  setFeed,
  showGraphStage,
  onToggleGraphStage,
  inspect,
  forces,
  onChangeForces,
  canvasRef,
  searchInput,
  searchQuery,
  onSearchInput,
}) => {
  const [coreMode, setCoreMode] = useState<CoreMode>("ring");
  const [tasks, setTasks] = useState<AssistantTaskItem[]>([]);
  const [tasksOpen, setTasksOpen] = useState(false);
  const [explorerOpen, setExplorerOpen] = useState(false);
  const [allowAlwaysReadOnly, setAllowAlwaysReadOnly] = useState(true);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const feedEndRef = useRef<HTMLDivElement | null>(null);
  const gVer = useGraphVersion();
  const graphEntityCount = useMemo(() => graph.nodes.size, [gVer]);

  const loadTasksAndPolicy = useCallback(async () => {
    try {
      const [tRes, sRes] = await Promise.all([
        apiFetch<{ tasks: AssistantTaskItem[] }>("/v2/tasks?limit=15"),
        apiFetch<{ filesystem?: { allow_always_read_only?: boolean } }>("/v2/settings"),
      ]);
      setTasks(tRes.tasks || []);
      if (sRes.filesystem && typeof sRes.filesystem.allow_always_read_only === "boolean") {
        setAllowAlwaysReadOnly(sRes.filesystem.allow_always_read_only);
      }
    } catch {
      // background polling handles recovery smoothly
    }
  }, []);

  useEffect(() => {
    loadTasksAndPolicy();
  }, [loadTasksAndPolicy]);

  // Hydrate conversation history + any still-pending inline permissions/approvals on initial mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [hRes, fsRes, apRes] = await Promise.all([
          apiFetch<{ messages: Array<{ role: string; content: string }> }>("/history"),
          apiFetch<{ requests: FsPermissionRequest[] }>("/v2/filesystem/requests?status=pending"),
          apiFetch<{ pending: OwnerApprovalRequest[] }>("/v2/approvals"),
        ]);
        if (cancelled) return;
        setFeed((cur) => {
          if (cur.length > 0) return cur;
          const hydrated: ChatFeedItem[] = (hRes.messages || []).slice(-30).map((m, idx) => ({
            id: `hist-${idx}-${m.role}`,
            role: m.role === "user" ? "user" : "assistant",
            text: m.content || "",
          }));
          for (const pr of fsRes.requests || []) {
            hydrated.push({
              id: `fsreq-${pr.request_id}`,
              role: "assistant",
              text: pr.reason || `Folder access permission requested for \`${pr.canonical_path}\`.`,
              command: "permission_required",
              permissionRequest: pr,
            });
          }
          for (const ap of apRes.pending || []) {
            hydrated.push({
              id: `apreq-${ap.token}`,
              role: "assistant",
              text: `Owner approval required for \`${ap.action_type}\` on \`${ap.target_resource}\`.`,
              command: "approval_required",
              approvalRequest: ap,
            });
          }
          return hydrated;
        });
      } catch {
        // leave feed empty cleanly on offline boot
      }
    })();
    return () => { cancelled = true; };
  }, [setFeed]);

  // Subscribe to SSE real-time permission, approval, task, and config events
  useEffect(() => {
    return onEvent((ev: LiveEvent) => {
      if (ev.type === "task_updated" && ev.task) {
        const t = ev.task as AssistantTaskItem;
        setTasks((prev) => {
          const rest = prev.filter((x) => x.task_id !== t.task_id);
          return [t, ...rest].slice(0, 20);
        });
      } else if (ev.type === "permission_requested" && ev.request) {
        const req = ev.request as FsPermissionRequest;
        setFeed((prev) => {
          const exists = prev.some((m) => m.permissionRequest?.request_id === req.request_id);
          if (exists) return prev;
          return [
            ...prev,
            {
              id: `perm-sse-${req.request_id}`,
              role: "assistant",
              text: req.reason || `Folder access permission requested for \`${req.canonical_path}\`.`,
              command: "permission_required",
              permissionRequest: req,
            },
          ];
        });
      } else if (ev.type === "approval_requested" && ev.approval) {
        const ap = ev.approval as OwnerApprovalRequest;
        setFeed((prev) => {
          const exists = prev.some((m) => m.approvalRequest?.token === ap.token);
          if (exists) return prev;
          return [
            ...prev,
            {
              id: `ap-sse-${ap.token}`,
              role: "assistant",
              text: `Owner approval required for \`${ap.action_type}\` on \`${ap.target_resource}\`.`,
              command: "approval_required",
              approvalRequest: ap,
            },
          ];
        });
      } else if (ev.type === "assistant_message" && ev.text) {
        const reqId = ev.request_id ? String(ev.request_id) : undefined;
        const apTok = ev.approval_token ? String(ev.approval_token) : undefined;
        setFeed((prev) => {
          const next = prev.map((m) => {
            if (reqId && m.permissionRequest?.request_id === reqId) {
              return {
                ...m,
                permissionRequest: { ...m.permissionRequest, status: ev.granted ? "approved" : "denied" },
              };
            }
            if (apTok && m.approvalRequest?.token === apTok) {
              return {
                ...m,
                approvalRequest: { ...m.approvalRequest, status: ev.granted ? "approved" : "denied" },
              };
            }
            return m;
          });
          const alreadyRendered =
            (reqId && next.some((m) => m.resolvedForRequestId === reqId)) ||
            (apTok && next.some((m) => m.resolvedForToken === apTok));
          if (alreadyRendered) return next;
          return [
            ...next,
            {
              id: `asst-msg-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
              role: "assistant",
              text: String(ev.text),
              paths: Array.isArray(ev.paths) ? (ev.paths as string[]) : [],
              task: (ev.task as AssistantTaskItem) || null,
              resolvedForRequestId: reqId,
              resolvedForToken: apTok,
            },
          ];
        });
      } else if (ev.type === "config_updated" || ev.type === "filesystem_permissions_changed") {
        loadTasksAndPolicy();
      }
    });
  }, [setFeed, loadTasksAndPolicy]);

  useEffect(() => {
    feedEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [feed.length]);

  const decideFsPermission = async (
    requestId: string,
    decision: "allow_once" | "allow_session" | "always_read_only" | "deny"
  ) => {
    setDecidingId(requestId);
    try {
      const res = await apiFetch<{
        granted: boolean;
        reply?: string;
        paths?: string[];
        task?: AssistantTaskItem | null;
        request?: FsPermissionRequest;
      }>(`/v2/filesystem/requests/${requestId}/decide`, {
        method: "POST",
        body: { decision, decided_by: "owner", execute_deferred: true },
      });
      setFeed((prev) => {
        const updated = prev.map((m) =>
          m.permissionRequest?.request_id === requestId
            ? {
                ...m,
                permissionRequest: {
                  ...(res.request || m.permissionRequest),
                  status: res.granted ? "approved" : "denied",
                },
              }
            : m
        );
        if (res.reply && !updated.some((m) => m.resolvedForRequestId === requestId)) {
          updated.push({
            id: `fs-res-${requestId}`,
            role: "assistant",
            text: res.reply,
            paths: res.paths || [],
            task: res.task || null,
            resolvedForRequestId: requestId,
          });
        }
        return updated;
      });
      if (res.task) {
        setTasks((prev) => [res.task!, ...prev.filter((t) => t.task_id !== res.task!.task_id)]);
      }
      onNotify(res.granted ? `Access granted (${decision.replace(/_/g, " ")})` : "Access denied");
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setDecidingId(null);
      loadTasksAndPolicy();
    }
  };

  const decideApproval = async (token: string, approved: boolean) => {
    setDecidingId(token);
    try {
      const res = await apiFetch<{ status?: string; reply?: string; task?: AssistantTaskItem | null }>(
        `/v2/approvals/${token}/decide`,
        {
          method: "POST",
          body: { approved, decided_by: "owner" },
        }
      );
      setFeed((prev) => {
        const updated = prev.map((m) =>
          m.approvalRequest?.token === token
            ? { ...m, approvalRequest: { ...m.approvalRequest, status: approved ? "approved" : "denied" } }
            : m
        );
        const msg = res.reply || (approved ? `Approved action \`${token}\`.` : `Denied action \`${token}\`.`);
        if (!updated.some((m) => m.resolvedForToken === token)) {
          updated.push({
            id: `ap-res-${token}`,
            role: "assistant",
            text: msg,
            task: res.task || null,
            resolvedForToken: token,
          });
        }
        return updated;
      });
      onNotify(approved ? `Approved ${token}` : `Denied ${token}`);
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setDecidingId(null);
      loadTasksAndPolicy();
    }
  };

  const cancelTask = async (taskId: string) => {
    try {
      const r = await apiFetch<{ task: AssistantTaskItem }>(`/v2/tasks/${taskId}/cancel`, { method: "POST" });
      if (r.task) {
        setTasks((prev) => [r.task, ...prev.filter((t) => t.task_id !== taskId)]);
      }
      onNotify("Task cancelled.");
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const clearConversation = async () => {
    try {
      await apiFetch("/history", { method: "DELETE" });
      setFeed([]);
      onNotify("Conversation history cleared.");
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const activeTasks = tasks.filter((t) =>
    ["queued", "planning", "awaiting_permission", "awaiting_approval", "running", "paused"].includes(t.status)
  );
  const currentModel = status?.brain?.model || "local";
  const currentProvider = status?.brain?.provider || "ollama";
  const onlineActive = (status?.mode || "").toLowerCase().includes("online");

  return (
    <div className={`assistant-home ${showGraphStage ? "with-graph" : ""}`}>
      {/* Left HUD Visual Column: Holographic Core + Expandable 3D Knowledge Graph + Real Telemetry */}
      <section className={`assistant-visual ${showGraphStage ? "graph-expanded" : ""}`} aria-label="Jarvis HUD Core and Knowledge Graph">
        <div className="assistant-visual-bar row between">
          <div className="row wrap">
            <span className="pill info">JARVIS OS · {assistantState}</span>
            <span className={`pill ${onlineActive ? "warn" : "ok"}`}>
              {onlineActive ? "Online Assisted" : "Local Mode"}
            </span>
            <span className="chip static" title="Active language model and provider">
              <b>Model</b> {currentProvider}:{currentModel}
            </span>
            <span className={`status ${voiceLoopState.error ? "danger" : voiceLoopState.mic ? "ok" : ""}`}>
              <i className="dot" /> {voiceLoopState.error ? "Mic Error" : voiceLoopState.mic ? "Mic Listening" : "Mic Idle"}
            </span>
          </div>
          <div className="row wrap">
            {!showGraphStage && (
              <div className="row core-mode-pills">
                {(["ring", "cube", "face"] as CoreMode[]).map((m) => (
                  <button key={m} className={coreMode === m ? "active" : "ghost"} onClick={() => setCoreMode(m)}>
                    {m}
                  </button>
                ))}
              </div>
            )}
            <button
              className={showGraphStage ? "active" : "ghost"}
              onClick={() => onToggleGraphStage(!showGraphStage)}
              title="Toggle P10.18 Real-Time Knowledge Graph"
            >
              {showGraphStage ? "HUD Core" : `Knowledge Graph (${graphEntityCount})`}
            </button>
            {showGraphStage && (
              <button className={explorerOpen ? "active" : "ghost"} onClick={() => setExplorerOpen((o) => !o)}>
                Explorer
              </button>
            )}
          </div>
        </div>

        {showGraphStage ? (
          <div className="assistant-graph-host">
            {explorerOpen && (
              <BrainExplorer
                open={true}
                onToggleOpen={() => setExplorerOpen(false)}
                selectedId={inspect?.kind === "node" ? inspect.id : null}
                inspect={inspect}
                searchInput={searchInput}
                onSearchInput={onSearchInput}
                forces={forces}
                onChangeForces={onChangeForces}
                conn={live}
                modeLabel={status?.mode || "offline"}
                onFocusNode={(id) => canvasRef.current?.focus(id)}
                onUnpinNode={(id) => canvasRef.current?.unpin(id)}
                onNotify={onNotify}
                onInspect={onInspect}
                onNavigate={onNavigate}
              />
            )}
            <GraphView
              onNotify={onNotify}
              onInspect={onInspect}
              onNavigate={onNavigate}
              selectedId={inspect?.kind === "node" ? inspect.id : null}
              query={searchQuery}
              forces={forces}
              onChangeForces={onChangeForces}
              canvasRef={canvasRef}
            />
          </div>
        ) : (
          <div className="assistant-orb-stage">
            <div className="hologram-frame assistant-orb-big">
              <HologramCore mode={coreMode} assistantState={assistantState} audioLevel={audioLevel} />
              <div className="hologram-hud-label">
                <span className="mono">{assistantState}</span>
                {stateDetail && <small className="dim">{stateDetail}</small>}
              </div>
            </div>

            <div className="assistant-metrics-row">
              <div className="kpi-tile">
                <span>{graphEntityCount}</span>
                <small className="dim">Graph Entities</small>
              </div>
              <div className="kpi-tile">
                <span>{overview?.memory?.personal_memories ?? 0}</span>
                <small className="dim">Memories</small>
              </div>
              <div className="kpi-tile">
                <span>{activeTasks.length}</span>
                <small className="dim">Active Tasks</small>
              </div>
            </div>

            <div className="assistant-sys-strip">
              <div className="health-matrix">
                <div className="health-cell">
                  <small className="dim">Backend</small>
                  <span className={`status ${backendDown ? "danger" : "ok"}`}>
                    <i className="dot" /> {backendDown ? "Offline" : "Connected"}
                  </span>
                </div>
                <div className="health-cell">
                  <small className="dim">Ollama</small>
                  <span className={`status ${status?.ollama?.up ? "ok" : "warn"}`}>
                    <i className="dot" /> {status?.ollama?.up ? "Ready" : "Unavailable"}
                  </span>
                </div>
                <div className="health-cell">
                  <small className="dim">Voice Engine</small>
                  <span className={`status ${status?.ready?.tts ? "ok" : "warn"}`}>
                    <i className="dot" /> {status?.ready?.tts ? "TTS Ready" : "Check Settings"}
                  </span>
                </div>
                <div className="health-cell">
                  <small className="dim">RAM Free</small>
                  <span className="mono" style={{ fontSize: 11.5 }}>
                    {status?.system?.ram ? `${status.system.ram.free_gb}/${status.system.ram.total_gb} GB` : "—"}
                  </span>
                </div>
              </div>
            </div>

            {overview && overview.pending_approvals?.length > 0 && (
              <div className="alert-card">
                <span className="pill warn">{overview.pending_approvals.length} approval awaiting decision</span>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Right / Primary Conversation, Inline Permissions & Contextual Tasks Column */}
      <section className="assistant-stream-col" aria-label="Unified Assistant Workspace">
        <div className="assistant-stream-head row between">
          <div className="row wrap">
            <strong>JARVIS ASSISTANT</strong>
            {activeTasks.length > 0 && (
              <button className="pill warn" onClick={() => setTasksOpen((o) => !o)}>
                {activeTasks.length} active task{activeTasks.length === 1 ? "" : "s"} · {tasksOpen ? "Hide" : "Inspect"}
              </button>
            )}
            {activeTasks.length === 0 && tasks.length > 0 && (
              <button className="ghost" onClick={() => setTasksOpen((o) => !o)}>
                Tasks ({tasks.length})
              </button>
            )}
          </div>
          <div className="row">
            <button className="ghost" onClick={() => onNavigate?.("settings")}>Settings</button>
            {feed.length > 0 && (
              <button className="ghost" onClick={clearConversation}>Clear</button>
            )}
          </div>
        </div>

        {/* Contextual Tasks Panel: shown on active work or user click */}
        {tasksOpen && (
          <div className="contextual-tasks-panel" aria-label="Contextual Assistant Tasks">
            <div className="row between" style={{ marginBottom: 6 }}>
              <span className="eyebrow">Central Task Manager</span>
              <button className="ghost icon" onClick={() => setTasksOpen(false)} aria-label="Close tasks panel">✕</button>
            </div>
            {tasks.length === 0 ? (
              <p className="dim">No assistant tasks recorded yet.</p>
            ) : (
              <div className="task-cards-list">
                {tasks.slice(0, 8).map((t) => (
                  <div key={t.task_id} className="task-mini-card">
                    <div className="row between wrap">
                      <span className={`pill ${taskTone(t.status)}`}>{t.status}</span>
                      <span className="mono dim">{t.intent} · {t.source} · {fmtTime(t.created_at)}</span>
                      {["queued", "planning", "awaiting_permission", "awaiting_approval", "running"].includes(t.status) && (
                        <button className="danger" onClick={() => cancelTask(t.task_id)}>Cancel</button>
                      )}
                    </div>
                    <div className="task-req">{t.user_request}</div>
                    {t.current_step && <div className="dim" style={{ fontSize: 11.5 }}>{t.current_step}</div>}
                    {t.plan?.length > 0 && (
                      <ol className="task-plan-steps">
                        {t.plan.map((st, idx) => <li key={idx}>{st}</li>)}
                      </ol>
                    )}
                    <div className="row wrap" style={{ marginTop: 4 }}>
                      {t.selected_agents?.map((a) => <span key={a} className="chip static"><b>agent</b> {a}</span>)}
                      {t.selected_capabilities?.map((c) => <span key={c} className="chip static"><b>skill</b> {c}</span>)}
                      {t.tools_used?.map((tl) => <span key={tl} className="chip static"><b>tool</b> {tl}</span>)}
                      {t.generated_files?.map((f) => <span key={f} className="chip"><b>file</b> {f}</span>)}
                    </div>
                    {t.error_info && <p className="err-text" style={{ marginTop: 4 }}>{t.error_info}</p>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Unified Conversation Stream + Inline Permission & Approval Cards */}
        <div className="chat-log assistant-feed" role="log" aria-label="Conversation stream">
          {feed.length === 0 && (
            <div className="empty-state">
              <div className="eyebrow">Voice-First Central AI OS</div>
              <h2>Speak naturally or type a command below</h2>
              <p className="dim">
                Ask Jarvis to search folders (with explicit inline permission), inspect codebases, generate reports, manage reminders or memories, or check pending tasks.
              </p>
              <div className="row wrap" style={{ justifyContent: "center" }}>
                <span className="chip static">Search my Documents folder</span>
                <span className="chip static">Check all pending tasks</span>
                <span className="chip static">Remember this: ...</span>
              </div>
            </div>
          )}

          {feed.map((m) => {
            const pr = m.permissionRequest;
            const ap = m.approvalRequest;
            const canAlwaysReadOnly =
              Boolean(pr) &&
              allowAlwaysReadOnly &&
              (pr?.requested_operations || []).length > 0 &&
              (pr?.requested_operations || []).every((op) => READ_OP_SET.has(op));

            return (
              <div key={m.id} className={`msg ${m.role} ${m.error ? "error" : ""}`}>
                <div className="bubble">
                  {m.role === "user" ? (
                    <span>{m.text}</span>
                  ) : (
                    <>
                      <div className="row wrap" style={{ marginBottom: 4 }}>
                        {m.decision && (
                          <span className="pill info">
                            {m.decision.department} · {m.decision.intent}
                          </span>
                        )}
                        {m.task && <span className={`pill ${taskTone(m.task.status)}`}>task: {m.task.status}</span>}
                        {m.streaming && <span className="status info"><i className="dot" /> {assistantState}</span>}
                      </div>

                      {(m.tools || []).map((t, i) => (
                        <div key={i} className="tool-line mono">tool · {t}</div>
                      ))}

                      {m.error ? (
                        <p className="err-text">{m.error}</p>
                      ) : m.text ? (
                        <Markdown text={m.text} />
                      ) : (
                        <span className="typing" aria-label="Generating"><i /><i /><i /></span>
                      )}

                      {/* Inline Scoped Filesystem Permission Card */}
                      {pr && (
                        <div className="inline-perm-card" data-request-id={pr.request_id}>
                          <div className="row between wrap">
                            <span className={`pill ${pr.status === "approved" ? "ok" : pr.status === "denied" ? "danger" : "warn"}`}>
                              Filesystem Permission · {pr.status}
                            </span>
                            <span className="mono dim">id: {pr.request_id.slice(0, 8)}</span>
                          </div>
                          <div className="perm-path-box mono">{pr.canonical_path}</div>
                          <div className="perm-ops-grid">
                            <div>
                              <small className="dim">Requested permissions</small>
                              <div className="row wrap">
                                {pr.requested_operations.map((op) => (
                                  <span key={op} className="pill info">{op}</span>
                                ))}
                              </div>
                            </div>
                            <div>
                              <small className="dim">NOT requested (stays blocked)</small>
                              <div className="row wrap">
                                {(pr.not_requested_operations || []).slice(0, 5).map((op) => (
                                  <span key={op} className="pill">{op}</span>
                                ))}
                              </div>
                            </div>
                          </div>
                          {pr.sensitive_flags?.length > 0 && (
                            <div className="dim" style={{ fontSize: 11.5 }}>
                              Flags: {pr.sensitive_flags.join(", ")}
                            </div>
                          )}
                          {pr.status === "pending" && (
                            <div className="row wrap perm-actions">
                              <button
                                className="ok"
                                disabled={decidingId === pr.request_id}
                                onClick={() => decideFsPermission(pr.request_id, "allow_once")}
                              >
                                Allow once
                              </button>
                              <button
                                className="active"
                                disabled={decidingId === pr.request_id}
                                onClick={() => decideFsPermission(pr.request_id, "allow_session")}
                              >
                                Allow for this session
                              </button>
                              {canAlwaysReadOnly && (
                                <button
                                  className="ghost"
                                  disabled={decidingId === pr.request_id}
                                  onClick={() => decideFsPermission(pr.request_id, "always_read_only")}
                                >
                                  Always allow (read-only)
                                </button>
                              )}
                              <button
                                className="danger"
                                disabled={decidingId === pr.request_id}
                                onClick={() => decideFsPermission(pr.request_id, "deny")}
                              >
                                Deny
                              </button>
                            </div>
                          )}
                        </div>
                      )}

                      {/* Inline Action Approval Card (Destructive / Privileged Actions) */}
                      {ap && (
                        <div className="inline-perm-card" data-approval-token={ap.token}>
                          <div className="row between wrap">
                            <span className={`pill ${ap.status === "approved" ? "ok" : ap.status === "denied" ? "danger" : "warn"}`}>
                              Owner Approval · {ap.status || "pending"}
                            </span>
                            <span className="mono dim">token: {ap.token}</span>
                          </div>
                          <div className="perm-path-box mono">{ap.target_resource}</div>
                          {ap.preview_diff && <pre className="diff">{ap.preview_diff}</pre>}
                          {(!ap.status || ap.status === "pending") && (
                            <div className="row wrap perm-actions">
                              <button
                                className="ok"
                                disabled={decidingId === ap.token}
                                onClick={() => decideApproval(ap.token, true)}
                              >
                                Approve
                              </button>
                              <button
                                className="danger"
                                disabled={decidingId === ap.token}
                                onClick={() => decideApproval(ap.token, false)}
                              >
                                Deny
                              </button>
                            </div>
                          )}
                        </div>
                      )}

                      {((m.paths?.length || 0) > 0 || (m.sources?.length || 0) > 0 || (m.memories || 0) > 0 || (m.web?.length || 0) > 0) && (
                        <div className="row wrap msg-src">
                          {(m.paths || []).slice(0, 8).map((p) => (
                            <span key={p} className="chip mono">{p}</span>
                          ))}
                          {(m.memories || 0) > 0 && <span className="chip static">{m.memories} memor{m.memories === 1 ? "y" : "ies"}</span>}
                          {(m.web || []).map((w) => (
                            <a key={w.url} className="chip" href={w.url} target="_blank" rel="noreferrer">{w.title || w.url}</a>
                          ))}
                        </div>
                      )}

                      {m.stats && !m.streaming && (
                        <div className="msg-foot row between dim mono">
                          <span>{m.stopped ? "stopped" : m.stats}</span>
                          {m.text && (
                            <button className="ghost" onClick={() => apiFetch("/speak", { method: "POST", body: { text: m.text.slice(0, 2000) } })}>
                              Speak
                            </button>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            );
          })}
          <div ref={feedEndRef} />
        </div>

        {/* Floating contextual Node Inspector if inspecting a node/detail */}
        {inspect && (
          <aside className="hud-drawer" aria-label="Contextual Inspector">
            <Inspector item={inspect} onClose={() => onInspect?.(null)} onNotify={onNotify} onNavigate={onNavigate} />
          </aside>
        )}
      </section>
    </div>
  );
};
