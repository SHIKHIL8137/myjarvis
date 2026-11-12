import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, setConfirmHandler } from "./api/client";
import { DEFAULT_FORCES, type ForceConfig, type GalaxyHandle } from "./components/GalaxyCanvas";
import { VoiceDock, type UiDirective } from "./components/VoiceDock";
import { AssistantHome, type ChatFeedItem } from "./features/AssistantHome";
import type { SystemOverview, SystemStatus } from "./features/OverviewPanel";
import { SettingsPanel } from "./features/SettingsPanel";
import { startGraph } from "./graphStore";
import { connState, onConnState, onEvent, type ConnState } from "./live";
import type { Inspectable, View } from "./shell";

type PrimaryDest = "assistant" | "settings";

interface UiPreferences {
  theme: string;
  compact_mode: boolean;
  reduced_motion: boolean;
}

const destFromHash = (): PrimaryDest => {
  const seg = (location.hash.replace(/^#\/?/, "").split("/")[0] || "assistant").toLowerCase();
  return seg === "settings" || seg === "gateway" ? "settings" : "assistant";
};

const hashWantsGraph = (): boolean => {
  const seg = (location.hash.replace(/^#\/?/, "").split("/")[0] || "").toLowerCase();
  return seg === "graph";
};

export const App: React.FC = () => {
  const [dest, setDest] = useState<PrimaryDest>(destFromHash);
  const [showGraphStage, setShowGraphStage] = useState<boolean>(hashWantsGraph);
  const [requestedDrawer, setRequestedDrawer] = useState<string | null>(null);

  useEffect(() => {
    const seg = (location.hash.replace(/^#\/?/, "").split("/")[0] || "").toLowerCase();
    if (!location.hash || seg === "workspace") {
      history.replaceState(null, "", "#/assistant");
    }
  }, []);
  const [feed, setFeed] = useState<ChatFeedItem[]>([]);
  const [inspect, setInspect] = useState<Inspectable | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [forces, setForces] = useState<ForceConfig>(DEFAULT_FORCES);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [overview, setOverview] = useState<SystemOverview | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [live, setLive] = useState<ConnState>(connState);
  const [uiPrefs, setUiPrefs] = useState<UiPreferences>({
    theme: "cyan-hud",
    compact_mode: false,
    reduced_motion: false,
  });

  const [assistantState, setAssistantState] = useState("Idle");
  const [stateDetail, setStateDetail] = useState("");
  const [audioLevel, setAudioLevel] = useState(0);
  const [voice, setVoice] = useState<{ state: string; mic: boolean; mode?: string; error?: string | null }>({
    state: "idle",
    mic: false,
  });

  const [toasts, setToasts] = useState<Array<{ id: number; msg: string }>>([]);
  const [confirmMsg, setConfirmMsg] = useState("");

  const canvasRef = useRef<GalaxyHandle>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const confirmResolve = useRef<(ok: boolean) => void>();
  const browserRecRef = useRef(false);
  const hostMicRef = useRef(false);

  const notify = useCallback((msg: string) => {
    const id = Date.now() + Math.random();
    setToasts((all) => [...all.slice(-3), { id, msg }]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 5000);
  }, []);

  const navigate = useCallback((v: View, arg?: string) => {
    if (v === "settings" || v === "gateway" || v === "skills") {
      setDest("settings");
      location.hash = "/settings";
      return;
    }
    setDest("assistant");
    location.hash = "/assistant";
    if (v === "workforce") {
      setRequestedDrawer("agents");
      return;
    }
    if (v === "memory" || v === "learning") {
      setRequestedDrawer("memory");
      return;
    }
    if (v === "projects") {
      setRequestedDrawer("aliases");
      return;
    }
    if (v === "recovery") {
      setRequestedDrawer("actions");
      return;
    }
    if (v === "graph" || (arg && (arg.startsWith("project:") || arg.startsWith("memory:") || arg.includes(":")))) {
      setShowGraphStage(true);
      if (arg) {
        setInspect({ kind: "node", id: arg });
        setTimeout(() => canvasRef.current?.focus(arg), 80);
      }
    }
  }, []);

  const doInspect = useCallback((item: Inspectable | null) => {
    setInspect(item);
  }, []);

  const applyUiDirective = useCallback((ui?: UiDirective | null) => {
    if (!ui) return;
    if (ui.focus === "graph") {
      setDest("assistant");
      location.hash = "/assistant";
      setShowGraphStage(true);
      return;
    }
    if ("open_drawer" in ui && ui.open_drawer) {
      if (ui.open_drawer === "explorer") {
        setDest("assistant");
        location.hash = "/assistant";
        setShowGraphStage(true);
      } else if (ui.open_drawer === "settings") {
        setDest("settings");
        location.hash = "/settings";
      } else {
        setDest("assistant");
        location.hash = "/assistant";
        setRequestedDrawer(ui.open_drawer);
      }
    }
  }, []);

  const handleBrowserRecChange = useCallback((active: boolean) => {
    browserRecRef.current = active;
    if (active) {
      setAssistantState("Listening");
      setStateDetail("browser_mic");
    } else {
      setAssistantState(hostMicRef.current ? "Listening" : "Idle");
      setStateDetail("");
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    const [st, ov, sCfg] = await Promise.allSettled([
      apiFetch<SystemStatus>("/status"),
      apiFetch<SystemOverview>("/v2/system/overview"),
      apiFetch<{ preferences?: UiPreferences }>("/v2/settings"),
    ]);
    setBackendDown(st.status === "rejected" && ov.status === "rejected");
    const stVal = st.status === "fulfilled" ? st.value : null;
    if (stVal?.voice) {
      const micOn = Boolean(stVal.voice.mic_active);
      hostMicRef.current = micOn;
      setVoice({
        state: stVal.voice.audio_state || stVal.voice.state || "idle",
        mic: micOn,
        mode: stVal.voice.mode,
        error: stVal.voice.error || null,
      });
      if (!micOn && !browserRecRef.current) {
        setAssistantState((cur) => (cur === "Listening" ? "Idle" : cur));
      }
    }
    if (sCfg.status === "fulfilled" && sCfg.value?.preferences) {
      setUiPrefs((prev) => ({ ...prev, ...sCfg.value.preferences }));
    }
    setStatus(stVal);
    setOverview(ov.status === "fulfilled" ? ov.value : null);
  }, []);

  useEffect(() => {
    const onHash = () => {
      setDest(destFromHash());
      if (hashWantsGraph()) setShowGraphStage(true);
    };
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setSearchQuery(searchInput), 180);
    return () => clearTimeout(t);
  }, [searchInput]);

  useEffect(() => {
    setConfirmHandler(
      (message) =>
        new Promise<boolean>((resolve) => {
          confirmResolve.current = resolve;
          setConfirmMsg(message);
          dialog.current?.showModal();
        })
    );
    const stopGraph = startGraph();
    const offConn = onConnState(() => setLive(connState()));
    let timer = 0;
    const soon = () => {
      clearTimeout(timer);
      timer = window.setTimeout(refreshStatus, 400);
    };
    const offEv = onEvent((ev) => {
      if (ev.type === "voice_state" || ev.type === "hello") {
        const loop = ev.state ?? ev.voice ?? "off";
        const micActive = Boolean(
          ev.mic_active ??
            (loop !== "off" &&
              !ev.paused &&
              !ev.mic_suspended &&
              !["IDLE", "ERROR", "RESUMING", "TTS_SPEAKING", undefined].includes(ev.audio_state))
        );
        hostMicRef.current = micActive;
        setVoice({
          state: ev.audio_state || loop,
          mic: micActive,
          mode: ev.mode || ev.voice_mode,
          error: ev.error || null,
        });
        if (ev.error) {
          setAssistantState("Error");
          setStateDetail(ev.error);
        } else if (ev.assistant_state) {
          const next = ev.assistant_state === "Listening" && !micActive && !browserRecRef.current ? "Idle" : ev.assistant_state;
          setAssistantState(next);
        }
      } else if (ev.type === "voice_error") {
        hostMicRef.current = false;
        const msg = ev.error || ev.message || "Microphone error";
        setVoice((v) => ({ ...v, mic: false, state: "ERROR", error: msg }));
        setAssistantState("Error");
        setStateDetail(msg);
        notify(msg);
      } else if (ev.type === "assistant_state" || ev.event_type === "assistant.state.changed") {
        const raw = ev.state || "";
        const safe = raw === "Listening" && !hostMicRef.current && !browserRecRef.current ? "Idle" : raw;
        if (safe) setAssistantState(safe);
        setStateDetail(ev.detail || "");
      } else if (ev.type === "ui_action" && ev.ui) {
        applyUiDirective(ev.ui as UiDirective);
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
      if (
        [
          "hello",
          "agent_run_update",
          "model",
          "voice_changed",
          "internet",
          "graph_event",
          "config_updated",
          "filesystem_permissions_changed",
          "task_updated",
        ].includes(ev.type)
      ) {
        soon();
      }
    });
    refreshStatus();
    const poll = setInterval(() => !document.hidden && refreshStatus(), 30000);
    const onReject = (e: PromiseRejectionEvent) => {
      notify(e.reason?.message || String(e.reason));
      e.preventDefault();
    };
    addEventListener("unhandledrejection", onReject);
    return () => {
      stopGraph();
      offConn();
      offEv();
      clearInterval(poll);
      clearTimeout(timer);
      removeEventListener("unhandledrejection", onReject);
    };
  }, [notify, refreshStatus, applyUiDirective]);

  const closeConfirm = (ok: boolean) => {
    dialog.current?.close();
    confirmResolve.current?.(ok);
    confirmResolve.current = undefined;
  };

  const shell = { onNotify: notify, onInspect: doInspect, onNavigate: navigate };
  const pendingCount = overview?.pending_approvals?.length || 0;
  const hudClasses = [
    "os-hud",
    uiPrefs.compact_mode ? "compact-mode" : "",
    uiPrefs.reduced_motion ? "reduced-motion" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={hudClasses} data-theme={uiPrefs.theme || "cyan-hud"}>
      <a className="skip" href="#workspace">
        Skip to content
      </a>

      {/* Strict 2-Destination OS Header: JARVIS ASSISTANT & SETTINGS */}
      <header className="hud-topbar">
        <div className="row" style={{ gap: 10 }}>
          <span className="logo" aria-hidden="true">
            J
          </span>
          <div className="brand-title">
            <span>J.A.R.V.I.S</span>
            <small className="mono dim">CENTRAL AI MANAGER</small>
          </div>
          <span className={`pill ${backendDown ? "danger" : status ? "ok" : ""}`}>
            {backendDown ? "Offline" : status ? `${status.mode} · ${status.brain.model}` : "Connecting"}
          </span>
        </div>

        <nav className="hud-nav" aria-label="OS Destinations">
          <button
            className={dest === "assistant" ? "active" : "ghost"}
            aria-current={dest === "assistant" ? "page" : undefined}
            onClick={() => navigate("assistant")}
          >
            JARVIS ASSISTANT
            {pendingCount > 0 && <span className="pill warn">{pendingCount}</span>}
          </button>
          <button
            className={dest === "settings" ? "active" : "ghost"}
            aria-current={dest === "settings" ? "page" : undefined}
            onClick={() => navigate("settings")}
          >
            SETTINGS
          </button>
        </nav>

        <div className="row" style={{ gap: 8 }}>
          <span className={`status ${live === "online" ? "ok" : live === "offline" ? "danger" : "warn"}`}>
            <i className="dot" /> {live}
          </span>
        </div>
      </header>

      <main className="workspace" id="workspace">
        {dest === "assistant" ? (
          <AssistantHome
            {...shell}
            assistantState={assistantState}
            stateDetail={stateDetail}
            audioLevel={audioLevel}
            voiceLoopState={voice}
            status={status}
            overview={overview}
            backendDown={backendDown}
            live={live}
            feed={feed}
            setFeed={setFeed}
            showGraphStage={showGraphStage}
            onToggleGraphStage={setShowGraphStage}
            inspect={inspect}
            forces={forces}
            onChangeForces={setForces}
            canvasRef={canvasRef}
            searchInput={searchInput}
            searchQuery={searchQuery}
            onSearchInput={setSearchInput}
            requestedDrawer={requestedDrawer}
            onClearRequestedDrawer={() => setRequestedDrawer(null)}
          />
        ) : (
          <SettingsPanel {...shell} />
        )}
      </main>

      <VoiceDock
        assistantState={assistantState}
        stateDetail={stateDetail}
        voiceLoopState={voice}
        audioLevel={audioLevel}
        onAudioLevel={setAudioLevel}
        onBrowserRecChange={handleBrowserRecChange}
        onUiAction={applyUiDirective}
        inAssistantView={dest === "assistant"}
        onFeedChange={setFeed}
        {...shell}
      />

      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast">
            <span>{t.msg}</span>
            <button
              className="ghost icon"
              aria-label="Dismiss notification"
              onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      <dialog
        ref={dialog}
        className="confirm"
        onCancel={(e) => {
          e.preventDefault();
          closeConfirm(false);
        }}
        aria-labelledby="confirm-title"
      >
        <h2 id="confirm-title">Please confirm</h2>
        <p>{confirmMsg}</p>
        <div className="row end">
          <button className="ghost" autoFocus onClick={() => closeConfirm(false)}>
            Cancel
          </button>
          <button className="danger" onClick={() => closeConfirm(true)}>
            Confirm
          </button>
        </div>
      </dialog>
    </div>
  );
};
