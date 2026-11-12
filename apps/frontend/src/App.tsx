import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, setConfirmHandler } from "./api/client";
import { BrainExplorer } from "./components/BrainExplorer";
import { DEFAULT_FORCES, type ForceConfig, type GalaxyHandle } from "./components/GalaxyCanvas";
import { Inspector } from "./components/Inspector";
import { JarvisCorePanel } from "./components/JarvisCorePanel";
import { VoiceDock } from "./components/VoiceDock";
import { startGraph } from "./graphStore";
import { connState, onConnState, onEvent } from "./live";
import type { SystemOverview, SystemStatus } from "./features/OverviewPanel";
import type { Inspectable, View } from "./shell";

const Overview = lazy(() => import("./features/OverviewPanel").then((m) => ({ default: m.OverviewPanel })));
const Chat = lazy(() => import("./features/ChatPanel").then((m) => ({ default: m.ChatPanel })));
const Assistant = lazy(() => import("./features/AssistantPanel").then((m) => ({ default: m.AssistantPanel })));
const Decision = lazy(() => import("./features/DecisionPanel").then((m) => ({ default: m.DecisionPanel })));
const Workforce = lazy(() => import("./features/WorkforcePanel").then((m) => ({ default: m.WorkforcePanel })));
const Skills = lazy(() => import("./features/SkillsPanel").then((m) => ({ default: m.SkillsPanel })));
const Memory = lazy(() => import("./features/MemoryPanel").then((m) => ({ default: m.MemoryPanel })));
const Graph = lazy(() => import("./features/GraphView").then((m) => ({ default: m.GraphView })));
const Gateway = lazy(() => import("./features/GatewayPanel").then((m) => ({ default: m.GatewayPanel })));
const Learning = lazy(() => import("./features/LearningPanel").then((m) => ({ default: m.LearningPanel })));
const Recovery = lazy(() => import("./features/RecoveryPanel").then((m) => ({ default: m.RecoveryPanel })));
const Settings = lazy(() => import("./features/SettingsPanel").then((m) => ({ default: m.SettingsPanel })));

const MODULES: Array<[View, string]> = [
  ["graph", "Knowledge Graph"],
  ["chat", "Chat"],
  ["overview", "System Overview"],
  ["assistant", "Docs & Reminders"],
  ["workforce", "Workforce"],
  ["projects", "Projects"],
  ["skills", "Skills"],
  ["memory", "Memory"],
  ["decision", "Decision"],
  ["gateway", "Gateway"],
  ["learning", "Learning"],
  ["recovery", "Recovery"],
  ["settings", "Settings"],
];
const VALID_VIEWS = new Set(MODULES.map(([v]) => v));
const viewFromHash = (): View => {
  const v = location.hash.replace(/^#\/?/, "") as View;
  return VALID_VIEWS.has(v) ? v : "graph";
};

export const App: React.FC = () => {
  const [view, setView] = useState<View>(viewFromHash);
  const [leftOpen, setLeftOpen] = useState(() => innerWidth >= 960 && localStorage.getItem("jarvis.left") !== "0");
  const [rightOpen, setRightOpen] = useState(() => innerWidth >= 1080 && localStorage.getItem("jarvis.right") !== "0");
  const [inspect, setInspect] = useState<Inspectable | null>(null);
  const [inspDrawer, setInspDrawer] = useState(false);
  const [toasts, setToasts] = useState<Array<{ id: number; msg: string }>>([]);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [overview, setOverview] = useState<SystemOverview | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [live, setLive] = useState(connState());
  const [voice, setVoice] = useState<{ state: string; mic: boolean; mode?: string }>({ state: "idle", mic: false });
  const [assistantState, setAssistantState] = useState("Idle");
  const [stateDetail, setStateDetail] = useState("");
  const [audioLevel, setAudioLevel] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [forces, setForces] = useState<ForceConfig>(DEFAULT_FORCES);
  const [chatAsk, setChatAsk] = useState<string | undefined>();
  const [chatVisited, setChatVisited] = useState(view === "chat");
  const [navArg, setNavArg] = useState<string | undefined>();
  const [confirmMsg, setConfirmMsg] = useState("");

  const dialog = useRef<HTMLDialogElement>(null);
  const confirmResolve = useRef<(ok: boolean) => void>();
  const composer = useRef<HTMLTextAreaElement>(null);
  const canvasRef = useRef<GalaxyHandle>(null);
  const toastId = useRef(0);

  const notify = useCallback((msg: string) => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-3), { id, msg }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  const navigate = useCallback((v: View, arg?: string) => {
    if (v === "chat" && arg) setChatAsk(arg);
    if (v === "graph" && arg) {
      setInspect({ kind: "node", id: arg });
      setLeftOpen(true);
      setTimeout(() => canvasRef.current?.focus(arg), 80);
    }
    setNavArg(v === "workforce" || v === "memory" ? arg : undefined);
    location.hash = `/${v}`;
  }, []);

  const doInspect = useCallback((item: Inspectable | null) => {
    setInspect(item);
    if (item && view === "graph") setLeftOpen(true);
    if (item && view !== "graph") setInspDrawer(true);
  }, [view]);

  const refreshStatus = useCallback(async () => {
    const [st, ov] = await Promise.allSettled([apiFetch<SystemStatus>("/status"), apiFetch<SystemOverview>("/v2/system/overview")]);
    setBackendDown(st.status === "rejected" && ov.status === "rejected");
    setStatus(st.status === "fulfilled" ? st.value : null);
    setOverview(ov.status === "fulfilled" ? ov.value : null);
  }, []);

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    const t = setTimeout(() => setSearchQuery(searchInput), 180);
    return () => clearTimeout(t);
  }, [searchInput]);
  useEffect(() => { if (view === "chat") setChatVisited(true); }, [view]);
  useEffect(() => { localStorage.setItem("jarvis.left", leftOpen ? "1" : "0"); }, [leftOpen]);
  useEffect(() => { localStorage.setItem("jarvis.right", rightOpen ? "1" : "0"); }, [rightOpen]);

  useEffect(() => {
    setConfirmHandler((message) => new Promise<boolean>((resolve) => {
      confirmResolve.current = resolve;
      setConfirmMsg(message);
      dialog.current?.showModal();
    }));
    const stopGraph = startGraph();
    const offConn = onConnState(() => setLive(connState()));
    let timer = 0;
    const soon = () => { clearTimeout(timer); timer = window.setTimeout(refreshStatus, 400); };
    const offEv = onEvent((ev) => {
      if (ev.type === "voice_state" || ev.type === "hello") {
        const loop = ev.state ?? ev.voice ?? "off";
        setVoice({ state: ev.audio_state || loop, mic: loop !== "off" && !["IDLE", "ERROR", undefined].includes(ev.audio_state), mode: ev.mode || ev.voice_mode });
        if (ev.assistant_state) setAssistantState(ev.assistant_state);
      } else if (ev.type === "assistant_state" || ev.event_type === "assistant.state.changed") {
        if (ev.state) setAssistantState(ev.state);
        setStateDetail(ev.detail || "");
      } else if (ev.type === "level" && typeof ev.rms === "number") {
        setAudioLevel(Math.min(1, ev.rms * 3));
      } else if (ev.type === "speaking") {
        if (ev.on) setAssistantState("Speaking");
      } else if (ev.type === "reminder_due" && ev.reminder?.title) {
        notify(`Reminder: ${ev.reminder.title}`);
      } else if (ev.type === "error" && ev.message) {
        setAssistantState("Error");
        setStateDetail(ev.message);
        notify(ev.message);
      }
      if (["hello", "agent_run_update", "model", "voice_changed", "internet", "graph_event"].includes(ev.type)) soon();
    });
    refreshStatus();
    const poll = setInterval(() => !document.hidden && refreshStatus(), 30000);
    const onReject = (e: PromiseRejectionEvent) => { notify(e.reason?.message || String(e.reason)); e.preventDefault(); };
    addEventListener("unhandledrejection", onReject);
    return () => {
      stopGraph();
      offConn();
      offEv();
      clearInterval(poll);
      clearTimeout(timer);
      removeEventListener("unhandledrejection", onReject);
    };
  }, [notify, refreshStatus]);

  const closeConfirm = (ok: boolean) => {
    dialog.current?.close();
    confirmResolve.current?.(ok);
    confirmResolve.current = undefined;
  };
  const shell = { onNotify: notify, onInspect: doInspect, onNavigate: navigate };
  const selectedId = inspect?.kind === "node" ? inspect.id : null;
  const pendingCount = overview?.pending_approvals.length || 0;

  return (
    <div className="os-hud">
      <a className="skip" href="#workspace">Skip to content</a>

      {/* Minimal Top HUD Command Strip */}
      <header className="hud-topbar">
        <div className="row" style={{ gap: 10 }}>
          <button className="ghost icon mobile-only" aria-label="Toggle Brain Explorer" onClick={() => setLeftOpen((o) => !o)}>☰</button>
          <span className="logo" aria-hidden="true">J</span>
          <div className="brand-title">
            <span>J.A.R.V.I.S</span>
            <small className="mono dim">AI OPERATING SYSTEM</small>
          </div>
          <span className={`pill ${backendDown ? "danger" : status ? "ok" : ""}`}>
            {backendDown ? "Offline" : status ? `${status.mode} · ${status.brain.model}` : "Connecting"}
          </span>
        </div>

        <nav className="hud-nav" aria-label="OS Workspaces">
          {MODULES.map(([v, label]) => (
            <button
              key={v}
              className={view === v ? "active" : "ghost"}
              aria-current={view === v ? "page" : undefined}
              onClick={() => navigate(v)}
            >
              {label}
              {v === "workforce" && pendingCount > 0 && <b className="badge">{pendingCount}</b>}
            </button>
          ))}
        </nav>

        <div className="row" style={{ gap: 6 }}>
          {view !== "graph" && (
            <button className="ghost" onClick={() => setInspDrawer((o) => !o)}>Inspector</button>
          )}
          <button className="ghost icon mobile-only" aria-label="Toggle Jarvis Core" onClick={() => setRightOpen((o) => !o)}>◎</button>
        </div>
      </header>

      {/* Main 3-Column HUD Cockpit */}
      <div className="hud-stage">
        {view === "graph" && (
          <BrainExplorer
            open={leftOpen}
            onToggleOpen={() => setLeftOpen((o) => !o)}
            selectedId={selectedId}
            inspect={inspect}
            searchInput={searchInput}
            onSearchInput={setSearchInput}
            forces={forces}
            onChangeForces={setForces}
            conn={live}
            modeLabel={status?.mode || "offline"}
            onFocusNode={(id) => canvasRef.current?.focus(id)}
            onUnpinNode={(id) => canvasRef.current?.unpin(id)}
            {...shell}
          />
        )}

        <main id="workspace" className="hud-center" tabIndex={-1}>
          <Suspense fallback={<p className="empty pad">Loading module…</p>}>
            {view === "graph" && (
              <Graph
                {...shell}
                selectedId={selectedId}
                query={searchQuery}
                forces={forces}
                onChangeForces={setForces}
                canvasRef={canvasRef}
              />
            )}
            {view === "overview" && <Overview {...shell} overview={overview} status={status} />}
            {chatVisited && (
              <div className="keep" hidden={view !== "chat"}>
                <Chat {...shell} ask={chatAsk} onAskConsumed={() => setChatAsk(undefined)} composerRef={composer} />
              </div>
            )}
            {view === "assistant" && <Assistant onNotify={notify} />}
            {view === "decision" && <Decision onNotify={notify} />}
            {view === "workforce" && <Workforce {...shell} section="workforce" initialProject={navArg} />}
            {view === "projects" && <Workforce {...shell} section="projects" />}
            {view === "skills" && <Skills onNotify={notify} />}
            {view === "memory" && <Memory {...shell} />}
            {view === "gateway" && <Gateway onNotify={notify} />}
            {view === "learning" && <Learning onNotify={notify} />}
            {view === "recovery" && <Recovery onNotify={notify} />}
            {view === "settings" && <Settings {...shell} />}
          </Suspense>
        </main>

        {view === "graph" && (
          <JarvisCorePanel
            open={rightOpen}
            onToggleOpen={() => setRightOpen((o) => !o)}
            assistantState={assistantState}
            stateDetail={stateDetail}
            audioLevel={audioLevel}
            status={status}
            overview={overview}
            backendDown={backendDown}
            live={live}
            {...shell}
          />
        )}

        {view !== "graph" && inspDrawer && (
          <Inspector item={inspect} onClose={() => setInspDrawer(false)} {...shell} />
        )}
      </div>

      {/* Persistent Voice Command Dock across all OS workspaces */}
      <VoiceDock
        assistantState={assistantState}
        stateDetail={stateDetail}
        voiceLoopState={voice}
        audioLevel={audioLevel}
        onAudioLevel={setAudioLevel}
        {...shell}
      />

      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast">
            <span>{t.msg}</span>
            <button className="ghost icon" aria-label="Dismiss notification" onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}>✕</button>
          </div>
        ))}
      </div>

      <dialog ref={dialog} className="confirm" onCancel={(e) => { e.preventDefault(); closeConfirm(false); }} aria-labelledby="confirm-title">
        <h2 id="confirm-title">Please confirm</h2>
        <p>{confirmMsg}</p>
        <div className="row end">
          <button className="ghost" autoFocus onClick={() => closeConfirm(false)}>Cancel</button>
          <button className="danger" onClick={() => closeConfirm(true)}>Confirm</button>
        </div>
      </dialog>
    </div>
  );
};
