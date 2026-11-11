import React, { useCallback, useEffect, useState } from "react";
import { apiFetch, streamChat } from "./api/client";
import { GalaxyCanvas, type GraphLink, type GraphNode } from "./components/GalaxyCanvas";
import { AssistantPanel } from "./features/AssistantPanel";
import { DecisionPanel } from "./features/DecisionPanel";
import { GatewayPanel } from "./features/GatewayPanel";
import { LearningPanel } from "./features/LearningPanel";
import { MemoryPanel } from "./features/MemoryPanel";
import { RecoveryPanel } from "./features/RecoveryPanel";
import { SkillsPanel } from "./features/SkillsPanel";
import { WorkforcePanel } from "./features/WorkforcePanel";

type Tab = "chat" | "assistant" | "decision" | "workforce" | "skills" | "memory" | "gateway" | "learning" | "recovery";

export const App: React.FC = () => {
  const [tab, setTab] = useState<Tab>("chat");
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [graphTitle, setGraphTitle] = useState("Personal Notes Galaxy");
  const [healthMode, setHealthMode] = useState("local");
  const [voiceState, setVoiceState] = useState("idle");
  const [micActive, setMicActive] = useState(false);
  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant"; text: string }>>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState("");

  const notify = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast((cur) => (cur === msg ? "" : cur)), 4500);
  }, []);

  const loadNotesGraph = useCallback(async () => {
    try {
      const g = await apiFetch<{ nodes: GraphNode[]; links: GraphLink[] }>("/graph");
      setGraphTitle("Personal Notes Galaxy");
      setNodes(g.nodes || []);
      setLinks(g.links || []);
    } catch {
      // ignore initial offline load errors
    }
  }, []);

  useEffect(() => {
    loadNotesGraph();
    apiFetch<{ mode?: string }>("/health").then((h) => h.mode && setHealthMode(h.mode)).catch(() => {});
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        if (ev.type === "voice_state" || ev.type === "hello") {
          setVoiceState(ev.audio_state || ev.state || "idle");
          setMicActive(ev.state === "listening" || ev.voice === "listening");
        } else if (ev.type === "graph_changed") {
          loadNotesGraph();
        } else if (ev.type === "reminder_due" && ev.reminder?.title) {
          notify(`Reminder: ${ev.reminder.title}`);
        }
      } catch {
        // ignore keepalive
      }
    };
    return () => es.close();
  }, [loadNotesGraph, notify]);

  const sendMessage = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: q }, { role: "assistant", text: "" }]);
    try {
      await streamChat(q, false, (ev) => {
        if (ev.type === "token" && typeof ev.text === "string") {
          setMessages((m) => {
            const next = [...m];
            next[next.length - 1] = { role: "assistant", text: next[next.length - 1].text + ev.text };
            return next;
          });
        } else if (ev.type === "done" && typeof ev.answer === "string") {
          setMessages((m) => {
            const next = [...m];
            next[next.length - 1] = { role: "assistant", text: ev.answer as string };
            return next;
          });
        } else if (ev.type === "error" && typeof ev.message === "string") {
          notify(ev.message);
        }
      });
    } catch (e) {
      notify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app-shell">
      <header className="hud-bar">
        <div className="brand">
          <span>J.A.R.V.I.S. OS</span>
          <span className={`pill ${healthMode === "local" ? "ok" : "warn"}`}>{healthMode}</span>
        </div>

        <nav className="nav-tabs">
          {(["chat", "assistant", "decision", "workforce", "skills", "memory", "gateway", "learning", "recovery"] as Tab[]).map((t) => (
            <button key={t} className={`tab-btn ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
              {t.toUpperCase()}
            </button>
          ))}
        </nav>

        <div className="hud-status">
          <span className="pill">VOICE: {voiceState}</span>
          <button
            className={micActive ? "danger" : ""}
            onClick={async () => {
              await apiFetch("/voice/session", { method: "POST", body: { action: micActive ? "stop" : "start", mode: "continuous" } });
              setMicActive(!micActive);
            }}
          >
            {micActive ? "Stop Mic" : "Start Mic"}
          </button>
        </div>
      </header>

      <main className="main-grid">
        <section className="galaxy-pane">
          <div className="galaxy-overlay">
            <span className="dim">{graphTitle} ({nodes.length} nodes)</span>
            {graphTitle !== "Personal Notes Galaxy" && (
              <button className="ghost" onClick={loadNotesGraph}>Notes Galaxy</button>
            )}
          </div>
          <GalaxyCanvas nodes={nodes} links={links} onSelectNode={(n) => notify(`Node: ${n.label}`)} />
        </section>

        <section className="workspace-pane">
          {tab === "chat" && (
            <>
              <div className="scroll-body">
                {!messages.length && (
                  <div className="card">
                    <h3>LOCAL-FIRST AI OPERATING SYSTEM</h3>
                    <p className="dim">Ask Jarvis naturally, switch to ASSISTANT for documents/reminders, WORKFORCE for Graphify + OpenCode + Worktrees, MEMORY for the 5-way knowledge store, or RECOVERY for encrypted backups.</p>
                  </div>
                )}
                {messages.map((m, idx) => (
                  <div key={idx} className={`chat-msg ${m.role}`}>{m.text || "…"}</div>
                ))}
              </div>
              <div className="composer">
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && sendMessage()}
                  placeholder="Command or question… (/status, remember that..., generate report)"
                />
                <button disabled={busy} onClick={sendMessage}>{busy ? "…" : "Send"}</button>
              </div>
            </>
          )}

          {tab === "assistant" && <AssistantPanel onNotify={notify} />}
          {tab === "decision" && <DecisionPanel onNotify={notify} />}
          {tab === "workforce" && (
            <WorkforcePanel
              onNotify={notify}
              onShowProjectGraph={(name, gNodes, gLinks) => {
                setGraphTitle(`Architecture: ${name}`);
                setNodes(gNodes);
                setLinks(gLinks);
              }}
            />
          )}
          {tab === "skills" && <SkillsPanel onNotify={notify} />}
          {tab === "memory" && <MemoryPanel onNotify={notify} />}
          {tab === "gateway" && <GatewayPanel onNotify={notify} />}
          {tab === "learning" && <LearningPanel onNotify={notify} />}
          {tab === "recovery" && <RecoveryPanel onNotify={notify} />}
        </section>
      </main>

      {toast && (
        <div style={{ position: "fixed", bottom: 16, right: 16, zIndex: 50 }} className="card">
          {toast}
        </div>
      )}
    </div>
  );
};

