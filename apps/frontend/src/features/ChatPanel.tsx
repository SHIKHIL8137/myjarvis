import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, streamChat } from "../api/client";
import { Markdown } from "../components/Markdown";
import { graph } from "../graphStore";
import { onEvent, type LiveEvent } from "../live";
import { fmtTime, type ShellProps } from "../shell";

interface Msg {
  id: number;
  role: "user" | "assistant" | "error";
  text: string;
  sent?: string; // exact payload sent to the backend (for Retry)
  attachment?: string;
  streaming?: boolean;
  stopped?: boolean;
  tools?: string[]; // real tool calls reported by the backend
  sources?: string[]; // notes retrieved for this turn
  memories?: number;
  knowledge?: string[];
  web?: Array<{ title?: string; url: string }>;
  stats?: string;
}

const MAX_MESSAGE = 4000; // backend limit (ChatIn.message)
const SPEAK_KEY = "jarvis.speak";
let seq = 0;

export const ChatPanel: React.FC<ShellProps & { ask?: string; onAskConsumed?: () => void; composerRef?: React.RefObject<HTMLTextAreaElement> }> = ({
  onNotify, onNavigate, ask, onAskConsumed, composerRef,
}) => {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [speak, setSpeak] = useState(() => localStorage.getItem(SPEAK_KEY) === "1");
  const [activity, setActivity] = useState("idle");
  const [voice, setVoice] = useState("");
  const [attach, setAttach] = useState<{ name: string; text: string } | null>(null);
  const [attachPath, setAttachPath] = useState<string | null>(null);
  const [histQ, setHistQ] = useState("");
  const [histHits, setHistHits] = useState<Array<{ role: string; content: string; t?: number }> | null>(null);
  const abort = useRef<AbortController | null>(null);
  const pending = useRef(""); // tokens not painted yet (one paint per animation frame)
  const raf = useRef(0);
  const voiceTurn = useRef(false);
  const logRef = useRef<HTMLDivElement>(null);
  const scrolled = useRef(false);

  const patchLast = (fn: (m: Msg) => Msg) => setMsgs((all) => {
    const i = all.length - 1;
    if (i < 0 || all[i].role === "user") return all;
    const next = all.slice();
    next[i] = fn(all[i]);
    return next;
  });
  const paint = () => {
    raf.current = 0;
    const add = pending.current;
    pending.current = "";
    if (add) patchLast((m) => ({ ...m, text: m.text + add }));
  };

  /** One handler for typed turns (HTTP stream) and voice turns (same events over SSE). */
  const handle = useCallback((ev: LiveEvent) => {
    if (ev.type === "token") {
      pending.current += ev.text;
      if (!raf.current) raf.current = requestAnimationFrame(paint);
      return;
    }
    cancelAnimationFrame(raf.current);
    paint();
    if (ev.type === "meta") patchLast((m) => ({ ...m, sources: ev.sources, memories: ev.memory_ids?.length, knowledge: (ev.knowledge_sources || []).map((k: { title: string }) => k.title) }));
    else if (ev.type === "tool") patchLast((m) => ({ ...m, tools: [...(m.tools || []), `${ev.name}${ev.query ? `: ${ev.query}` : ""}`] }));
    else if (ev.type === "reset_answer") patchLast((m) => ({ ...m, text: "" }));
    else if (ev.type === "interrupted") patchLast((m) => ({ ...m, streaming: false, stopped: true }));
    else if (ev.type === "error") patchLast((m) => ({ ...m, role: "error", text: ev.message, streaming: false }));
    else if (ev.type === "done") {
      patchLast((m) => ({
        ...m,
        text: ev.answer,
        streaming: false,
        web: (ev.web_sources || []).filter((s: { url?: string }) => typeof s.url === "string" && s.url.startsWith("https://")),
        stats: [ev.command ? `command: ${ev.command}` : ev.model, `${ev.seconds}s`, ev.tps ? `${ev.tps} tok/s` : "", ev.truncated ? "cut off" : ""].filter(Boolean).join(" · "),
      }));
    }
  }, []);

  useEffect(() => {
    apiFetch<{ messages: Array<{ role: string; content: string }> }>("/history")
      .then((h) => setMsgs((cur) => (cur.length ? cur : h.messages.slice(-40).map((x) => ({ id: ++seq, role: x.role === "user" ? "user" : "assistant", text: x.content })))))
      .catch((e) => onNotify(`Could not load conversation history: ${(e as Error).message}`));
    return onEvent((ev) => {
      if (ev.type === "activity") setActivity(ev.state);
      else if (ev.type === "voice_state" || ev.type === "hello") setVoice(ev.audio_state || "");
      else if (ev.type === "user") { // a spoken turn started on the server: mirror it here
        voiceTurn.current = true;
        setMsgs((m) => [...m, { id: ++seq, role: "user", text: ev.text }, { id: ++seq, role: "assistant", text: "", streaming: true }]);
      } else if (voiceTurn.current && ["meta", "tool", "reset_answer", "token", "done", "error", "interrupted"].includes(ev.type)) {
        handle(ev);
        if (["done", "error", "interrupted"].includes(ev.type)) voiceTurn.current = false;
      }
    });
  }, [handle, onNotify]);

  useEffect(() => {
    const el = logRef.current;
    if (!el || !msgs.length) return;
    // first paint (history just loaded) jumps to the newest message; afterwards follow only if already near the bottom
    if (!scrolled.current || el.scrollHeight - el.scrollTop - el.clientHeight < 260) el.scrollTop = el.scrollHeight;
    scrolled.current = true;
  }, [msgs]);

  const send = useCallback(async (text: string, resend?: Msg) => {
    const q = text.trim();
    if (!q || abort.current) return;
    const sent = resend?.sent ?? (attach ? `${q}\n\n[Attached file: ${attach.name}]\n${attach.text}`.slice(0, MAX_MESSAGE) : q);
    setMsgs((m) => [...m, { id: ++seq, role: "user", text: q, sent, attachment: resend?.attachment ?? attach?.name }, { id: ++seq, role: "assistant", text: "", streaming: true }]);
    setAttach(null);
    setBusy(true);
    abort.current = new AbortController();
    try {
      await streamChat(sent, speak, handle, abort.current.signal);
    } catch (e) {
      if ((e as Error).name === "AbortError") handle({ type: "interrupted" });
      else handle({ type: "error", message: `Request failed: ${(e as Error).message}` });
    } finally {
      abort.current = null;
      setBusy(false);
      patchLast((m) => (m.streaming ? { ...m, streaming: false, stopped: true } : m)); // stream closed without a final event
    }
    setTimeout(() => logRef.current?.scrollTo({ top: logRef.current.scrollHeight }), 0);
  }, [attach, handle, speak]);

  useEffect(() => {
    if (ask) {
      send(ask);
      onAskConsumed?.();
    }
  }, [ask]); // eslint-disable-line react-hooks/exhaustive-deps -- fire once per incoming request

  const stop = async () => {
    abort.current?.abort();
    await apiFetch("/voice/interrupt", { method: "POST" }).catch(() => {});
    await apiFetch("/speak/stop", { method: "POST" }).catch(() => {});
  };
  const call = (fn: () => Promise<unknown>, okMsg?: string) => async () => {
    try {
      await fn();
      if (okMsg) onNotify(okMsg);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };
  const lastUser = [...msgs].reverse().find((m) => m.role === "user");
  const openNote = (title: string) => {
    const n = [...graph.nodes.values()].find((x) => x.type === "note" && x.label.toLowerCase() === title.toLowerCase());
    if (n) onNavigate?.("graph", n.id);
    else onNotify(`No note called “${title}”.`);
  };

  return (
    <div className="chat">
      <div className="chat-head">
        <div className="row">
          <strong>Conversation</strong>
          <span className={`status ${activity === "idle" ? "" : "info"}`} title="Assistant activity reported by the backend">
            <i className="dot" /> {activity}
          </span>
          {voice && voice !== "IDLE" && <span className="status info" title="Voice loop state"><i className="dot" /> voice {voice.toLowerCase()}</span>}
        </div>
        <form className="row" onSubmit={async (e) => {
          e.preventDefault();
          if (!histQ.trim()) return setHistHits(null);
          try {
            setHistHits((await apiFetch<{ results: Array<{ role: string; content: string; t?: number }> }>(`/history/search?q=${encodeURIComponent(histQ)}`)).results);
          } catch (err) {
            onNotify((err as Error).message);
          }
        }}>
          <input type="search" value={histQ} onChange={(e) => { setHistQ(e.target.value); if (!e.target.value) setHistHits(null); }} placeholder="Search history…" aria-label="Search conversation history" />
          <button className="ghost" type="submit">Search</button>
          <button className="ghost" type="button" title="Clear the stored conversation and start fresh"
            onClick={call(async () => { await apiFetch("/history", { method: "DELETE" }); setMsgs([]); }, "New conversation started.")}>
            New conversation
          </button>
        </form>
      </div>

      {histHits && (
        <div className="hist-hits" aria-label="History search results">
          <div className="row between"><span className="eyebrow">{histHits.length} past message{histHits.length === 1 ? "" : "s"}</span><button className="ghost" onClick={() => setHistHits(null)}>Close</button></div>
          {!histHits.length && <p className="empty">Nothing in the stored history matches.</p>}
          {histHits.map((h, i) => <div key={i} className="hist-hit"><span className="mono dim">{h.role} · {fmtTime(h.t)}</span><div>{h.content.slice(0, 300)}</div></div>)}
        </div>
      )}

      <div className="chat-log" ref={logRef} aria-live="polite">
        {!msgs.length && (
          <div className="empty-state">
            <h2>Ask Jarvis anything</h2>
            <p className="dim">Answers stream from your local model. Notes, memories and knowledge used for a reply are listed under it and light up in the Knowledge Graph.</p>
          </div>
        )}
        {msgs.slice(-200).map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === "user" ? <div className="bubble">{m.text}{m.attachment && <span className="chip static">attached: {m.attachment}</span>}</div> : (
              <div className="bubble">
                {m.tools?.map((t, i) => <div key={i} className="tool-line mono">tool · {t}</div>)}
                {m.role === "error" ? <p className="err-text">{m.text}</p> : m.text ? <Markdown text={m.text} onNote={openNote} /> : m.streaming ? <span className="typing" aria-label="Jarvis is replying"><i /><i /><i /></span> : <em className="dim">(no reply)</em>}
                {(m.sources?.length || m.memories || m.knowledge?.length || m.web?.length) ? (
                  <div className="row wrap msg-src">
                    {m.sources?.map((s) => <button key={s} className="chip" onClick={() => openNote(s)} title="Open this note in the graph">note: {s}</button>)}
                    {m.memories ? <span className="chip static">{m.memories} memor{m.memories === 1 ? "y" : "ies"}</span> : null}
                    {m.knowledge?.map((k) => <span key={k} className="chip static">knowledge: {k}</span>)}
                    {m.web?.map((s) => <a key={s.url} className="chip" href={s.url} target="_blank" rel="noopener noreferrer">{s.title || new URL(s.url).hostname}</a>)}
                  </div>
                ) : null}
                {!m.streaming && (
                  <div className="row msg-foot">
                    <span className="mono dim">{m.stopped ? "stopped" : m.stats}</span>
                    {m.text && m.role !== "error" && <button className="ghost" onClick={call(() => navigator.clipboard.writeText(m.text), "Copied.")}>Copy</button>}
                    {m.text && m.role !== "error" && <button className="ghost" title="Read aloud with the local voice" onClick={call(() => apiFetch("/speak", { method: "POST", body: { text: m.text.slice(0, 2000) } }))}>Speak</button>}
                    {m.id === msgs[msgs.length - 1].id && lastUser && !busy && <button className="ghost" onClick={() => send(lastUser.text, lastUser)}>Retry</button>}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {attachPath !== null && (
        <form className="row attach-row" onSubmit={async (e) => {
          e.preventDefault();
          try {
            const r = await apiFetch<{ content?: string; name?: string }>("/v2/documents/parse", { method: "POST", body: { path: attachPath } });
            setAttach({ name: r.name || attachPath.split(/[\\/]/).pop() || attachPath, text: r.content || "" });
            setAttachPath(null);
          } catch (err) {
            onNotify((err as Error).message);
          }
        }}>
          <input autoFocus value={attachPath} onChange={(e) => setAttachPath(e.target.value)} placeholder="Path of a local PDF, DOCX, PPTX, XLSX or text file inside an allowed folder" aria-label="File path to attach" />
          <button type="submit">Attach</button>
          <button type="button" className="ghost" onClick={() => setAttachPath(null)}>Cancel</button>
        </form>
      )}

      <form className="composer" onSubmit={(e) => { e.preventDefault(); const v = input; setInput(""); send(v); }}>
        {attach && <span className="chip">attached: {attach.name} <button type="button" className="ghost icon" aria-label="Remove attachment" onClick={() => setAttach(null)}>✕</button></span>}
        <textarea
          ref={composerRef}
          rows={1}
          value={input}
          maxLength={MAX_MESSAGE}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}
          placeholder="Message Jarvis…  (Enter to send, Shift+Enter for a new line)"
          aria-label="Message"
        />
        <div className="row">
          <button type="button" className="ghost" title="Attach a local document (parsed by the document engine)" onClick={() => setAttachPath(attachPath === null ? "" : null)}>Attach</button>
          <button type="button" className="ghost" title="Push to talk: record one utterance on the Jarvis microphone" onClick={call(() => apiFetch("/voice/ptt", { method: "POST" }))}>Talk</button>
          <button type="button" className="ghost" aria-pressed={speak} title="Speak replies aloud" onClick={() => { localStorage.setItem(SPEAK_KEY, speak ? "0" : "1"); setSpeak(!speak); }}>
            Voice replies {speak ? "on" : "off"}
          </button>
          {busy || activity === "speaking" ? <button type="button" className="danger" onClick={stop}>Stop</button> : <button type="submit" disabled={!input.trim()}>Send</button>}
        </div>
      </form>
    </div>
  );
};
