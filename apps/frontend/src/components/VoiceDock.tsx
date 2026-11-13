import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, streamChat } from "../api/client";
import { graph } from "../graphStore";
import type {
  AssistantTaskItem,
  ChatFeedItem,
  FsPermissionRequest,
  OwnerApprovalRequest,
} from "../features/AssistantHome";
import type { LiveEvent } from "../live";
import { onEvent } from "../live";
import type { ShellProps } from "../shell";
import { Markdown } from "./Markdown";

interface ReplyCard {
  id: number;
  user: string;
  answer: string;
  streaming: boolean;
  stopped?: boolean;
  error?: string;
  tools: string[];
  sources: string[];
  memories: number;
  knowledge: string[];
  web: Array<{ title?: string; url: string }>;
  decision?: { department?: string; intent?: string; workflow?: string; agent_role?: string };
  stats?: string;
}

export interface UiDirective {
  open_drawer?: string | null;
  focus?: string;
  open?: string;
}

interface VoiceDockProps extends ShellProps {
  assistantState: string;
  stateDetail: string;
  voiceLoopState: { state: string; mic: boolean; mode?: string; error?: string | null };
  audioLevel: number;
  inAssistantView?: boolean;
  onFeedChange?: React.Dispatch<React.SetStateAction<ChatFeedItem[]>>;
  onAudioLevel?: (level: number) => void;
  onBrowserRecChange?: (active: boolean) => void;
  onUiAction?: (ui: UiDirective | null | undefined) => void;
  onHighlightNodes?: (ids: string[]) => void;
}

const SPEAK_KEY = "jarvis.speak";
let replySeq = 0;

/** Encode float32 PCM samples (-1..1) at native sampleRate into standard 16-bit mono WAV bytes (no aliasing decimation; backend applies bandlimited resampling). */
function encodeWav16(samples: Float32Array, sampleRate: number): Blob {
  const pcm = samples;
  const rate = Math.max(8000, Math.round(sampleRate || 16000));
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buf);
  const str = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  str(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  str(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  let off = 44;
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([buf], { type: "audio/wav" });
}

export const VoiceDock: React.FC<VoiceDockProps> = ({
  onNotify,
  onNavigate,
  onInspect,
  assistantState,
  stateDetail,
  voiceLoopState,
  audioLevel,
  inAssistantView = true,
  onFeedChange,
  onAudioLevel,
  onBrowserRecChange,
  onUiAction,
}) => {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [speak, setSpeak] = useState(() => localStorage.getItem(SPEAK_KEY) === "1");
  const [liveTranscript, setLiveTranscript] = useState("");
  const [decisionBadge, setDecisionBadge] = useState<string>("");
  const [card, setCard] = useState<ReplyCard | null>(null);
  const [cardOpen, setCardOpen] = useState(false);
  const [browserRec, setBrowserRec] = useState(false);

  const abort = useRef<AbortController | null>(null);
  const pendingTokens = useRef("");
  const rafRef = useRef(0);
  const activeFeedMsgId = useRef<string | null>(null);
  const waveCanvas = useRef<HTMLCanvasElement | null>(null);
  const waveHistory = useRef<number[]>(new Array(44).fill(0));
  const voiceTurn = useRef(false);
  const webMicRef = useRef<{ stream: MediaStream; ctx: AudioContext; chunks: Float32Array[]; rate: number } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const flushTokens = () => {
    rafRef.current = 0;
    const chunk = pendingTokens.current;
    pendingTokens.current = "";
    if (chunk) {
      setCard((c) => (c ? { ...c, answer: c.answer + chunk } : c));
      const fid = activeFeedMsgId.current;
      if (fid && onFeedChange) {
        onFeedChange((prev) => prev.map((m) => (m.id === fid ? { ...m, text: m.text + chunk } : m)));
      }
    }
  };

  const handleTurnEvent = useCallback((ev: LiveEvent) => {
    if (ev.type === "token") {
      pendingTokens.current += ev.text;
      if (!rafRef.current) rafRef.current = requestAnimationFrame(flushTokens);
      return;
    }
    cancelAnimationFrame(rafRef.current);
    flushTokens();
    const fid = activeFeedMsgId.current;
    const updateActiveFeed = (fn: (m: ChatFeedItem) => ChatFeedItem) => {
      if (fid && onFeedChange) {
        onFeedChange((prev) => prev.map((m) => (m.id === fid ? fn(m) : m)));
      }
    };
    if (ev.type === "meta") {
      if (ev.decision) {
        const d = ev.decision;
        setDecisionBadge(`${d.department || "assistant"} · ${d.intent || "chat"} · ${d.workflow || "direct"}`);
      }
      setCard((c) =>
        c
          ? {
              ...c,
              sources: ev.sources || [],
              memories: ev.memory_ids?.length || 0,
              knowledge: (ev.knowledge_sources || []).map((k: { title: string }) => k.title),
              decision: ev.decision,
            }
          : c
      );
      updateActiveFeed((m) => ({
        ...m,
        decision: ev.decision as ChatFeedItem["decision"],
        sources: (ev.sources as string[]) || [],
        memories: ev.memory_ids?.length || 0,
        permissionRequest: (ev.permission_request as FsPermissionRequest) || m.permissionRequest,
        approvalRequest: (ev.approval as OwnerApprovalRequest) || m.approvalRequest,
        clarification: (ev.clarification as ChatFeedItem["clarification"]) || m.clarification,
        task: (ev.task as AssistantTaskItem) || m.task,
      }));
    } else if (ev.type === "tool") {
      const tline = `${ev.name}${ev.query ? `: ${ev.query}` : ""}`;
      setCard((c) => (c ? { ...c, tools: [...c.tools, tline] } : c));
      updateActiveFeed((m) => ({ ...m, tools: [...(m.tools || []), tline] }));
    } else if (ev.type === "reset_answer") {
      setCard((c) => (c ? { ...c, answer: "" } : c));
      updateActiveFeed((m) => ({ ...m, text: "" }));
    } else if (ev.type === "interrupted") {
      setCard((c) => (c ? { ...c, streaming: false, stopped: true } : c));
      updateActiveFeed((m) => ({ ...m, streaming: false, stopped: true }));
    } else if (ev.type === "error") {
      setCard((c) => (c ? { ...c, error: ev.message, streaming: false } : c));
      updateActiveFeed((m) => ({ ...m, error: String(ev.message), streaming: false }));
    } else if (ev.type === "done") {
      if (ev.ui) onUiAction?.(ev.ui as UiDirective);
      const statsStr = [ev.command ? `command: ${ev.command}` : ev.model, `${ev.seconds}s`, ev.tps ? `${ev.tps} tok/s` : ""].filter(Boolean).join(" · ");
      setCard((c) =>
        c
          ? {
              ...c,
              answer: ev.answer || c.answer,
              streaming: false,
              web: (ev.web_sources || []).filter((s: { url?: string }) => typeof s.url === "string" && s.url.startsWith("https://")),
              stats: statsStr,
            }
          : c
      );
      updateActiveFeed((m) => ({
        ...m,
        text: String(ev.answer || m.text),
        streaming: false,
        command: (ev.command as string) || m.command,
        paths: Array.isArray(ev.paths) ? (ev.paths as string[]) : m.paths,
        web: ((ev.web_sources as Array<{ title?: string; url: string }>) || []).filter(
          (s) => typeof s.url === "string" && s.url.startsWith("https://")
        ),
        permissionRequest: (ev.permission_request as FsPermissionRequest) || m.permissionRequest,
        approvalRequest: (ev.approval as OwnerApprovalRequest) || m.approvalRequest,
        clarification: (ev.clarification as ChatFeedItem["clarification"]) || m.clarification,
        task: (ev.task as AssistantTaskItem) || m.task,
        stats: statsStr,
      }));
    }
  }, [onFeedChange, onUiAction]);

  useEffect(() => {
    return onEvent((ev) => {
      if (ev.type === "decision" && ev.intent) {
        setDecisionBadge(`${ev.department || "assistant"} · ${ev.intent} · ${ev.workflow || "direct"}`);
      } else if (ev.type === "transcript_partial" || ev.event_type === "assistant.transcript.partial") {
        const txt = String(ev.text || ev.payload?.text || "").trim();
        if (txt) setLiveTranscript(`${txt} …`);
      } else if (
        ev.type === "transcript" ||
        ev.type === "transcript_record" ||
        ev.type === "transcript_final" ||
        ev.event_type === "assistant.transcript.final"
      ) {
        const txt = String(ev.text || ev.payload?.text || ev.corrected_transcript || "").trim();
        if (txt) setLiveTranscript(txt);
      } else if (ev.type === "user" && ev.text) {
        voiceTurn.current = true;
        setLiveTranscript(ev.text);
        const turnId = ++replySeq;
        const asstMsgId = `voice-a-${turnId}`;
        activeFeedMsgId.current = asstMsgId;
        onFeedChange?.((prev) => [
          ...prev,
          { id: `voice-u-${turnId}`, role: "user", text: String(ev.text) },
          { id: asstMsgId, role: "assistant", text: "", streaming: true, tools: [] },
        ]);
        setCard({
          id: turnId,
          user: ev.text,
          answer: "",
          streaming: true,
          tools: [],
          sources: [],
          memories: 0,
          knowledge: [],
          web: [],
        });
        setCardOpen(!inAssistantView);
      } else if (voiceTurn.current && ["meta", "tool", "reset_answer", "token", "done", "error", "interrupted"].includes(ev.type)) {
        handleTurnEvent(ev);
        if (["done", "error", "interrupted"].includes(ev.type)) voiceTurn.current = false;
      }
    });
  }, [handleTurnEvent]);

  // Draw lightweight real audio waveform bar canvas (flat calm line when idle / zero signal).
  useEffect(() => {
    waveHistory.current.push(audioLevel);
    if (waveHistory.current.length > 44) waveHistory.current.shift();
    const cvs = waveCanvas.current;
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    if (!ctx) return;
    const w = cvs.width;
    const h = cvs.height;
    ctx.clearRect(0, 0, w, h);
    const active = assistantState === "Speaking" || assistantState === "Speech detected" || assistantState === "Listening" || browserRec;
    ctx.fillStyle = active ? "#22d3ee" : "rgba(56, 189, 248, 0.35)";
    const bars = waveHistory.current.length;
    const step = w / bars;
    for (let i = 0; i < bars; i++) {
      const val = waveHistory.current[i];
      const barH = Math.max(2, Math.min(h - 2, val * (h - 2) + (assistantState === "Speaking" ? 3 + ((i + Math.floor(performance.now() / 90)) % 5) * 1.4 : 0)));
      ctx.fillRect(i * step + 1, (h - barH) / 2, Math.max(1.5, step - 2), barH);
    }
  }, [audioLevel, assistantState, browserRec]);

  // Global "/" shortcut focuses command bar cleanly unless typing
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
      if (e.key === "/" && !typing) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);

  const sendCommand = async (raw: string) => {
    const q = raw.trim();
    if (!q || abort.current) return;
    setInput("");
    setBusy(true);
    setLiveTranscript(q);
    const turnId = ++replySeq;
    const asstId = `chat-a-${turnId}`;
    activeFeedMsgId.current = asstId;
    onFeedChange?.((prev) => [
      ...prev,
      { id: `chat-u-${turnId}`, role: "user", text: q },
      { id: asstId, role: "assistant", text: "", streaming: true, tools: [] },
    ]);
    setCard({
      id: turnId,
      user: q,
      answer: "",
      streaming: true,
      tools: [],
      sources: [],
      memories: 0,
      knowledge: [],
      web: [],
    });
    setCardOpen(!inAssistantView);
    abort.current = new AbortController();
    try {
      await streamChat(q, speak, handleTurnEvent, abort.current.signal);
    } catch (e) {
      if ((e as Error).name === "AbortError") handleTurnEvent({ type: "interrupted" });
      else handleTurnEvent({ type: "error", message: (e as Error).message });
    } finally {
      abort.current = null;
      setBusy(false);
      setCard((c) => (c && c.streaming ? { ...c, streaming: false, stopped: true } : c));
      onFeedChange?.((prev) => prev.map((m) => (m.id === asstId && m.streaming ? { ...m, streaming: false } : m)));
    }
  };

  const stopAll = async () => {
    abort.current?.abort();
    setBusy(false);
    await apiFetch("/voice/interrupt", { method: "POST" }).catch(() => {});
    await apiFetch("/speak/stop", { method: "POST" }).catch(() => {});
  };

  const recoverMic = async () => {
    try {
      const r = await apiFetch<{ error?: string; state?: string }>("/voice/session", {
        method: "POST",
        body: { action: "recover" },
      });
      if (r.error) onNotify(r.error);
      else onNotify(`Microphone recovered (${r.state || "ready"})`);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const toggleHostMic = async () => {
    try {
      const r = await apiFetch<{ error?: string }>("/voice/session", {
        method: "POST",
        body: { action: voiceLoopState.mic ? "stop" : "start", mode: "continuous" },
      });
      if (r.error) onNotify(r.error);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const toggleBrowserMic = async () => {
    if (browserRec && webMicRef.current) {
      const rec = webMicRef.current;
      webMicRef.current = null;
      setBrowserRec(false);
      onBrowserRecChange?.(false);
      onAudioLevel?.(0);
      rec.stream.getTracks().forEach((t) => t.stop());
      await rec.ctx.close().catch(() => {});
      const total = rec.chunks.reduce((n, c) => n + c.length, 0);
      const merged = new Float32Array(total);
      let pos = 0;
      for (const c of rec.chunks) {
        merged.set(c, pos);
        pos += c.length;
      }
      if (merged.length < rec.rate * 0.25) {
        onNotify("Voice snippet too short.");
        return;
      }
      setBusy(true);
      try {
        const wav = encodeWav16(merged, rec.rate);
        const res = await fetch(`/api/voice/command?speak=${speak ? "true" : "false"}`, {
          method: "POST",
          headers: { "X-Jarvis": "1", "Content-Type": "audio/wav" },
          body: wav,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (data.prompt) setLiveTranscript(data.prompt);
        if (data.ui) onUiAction?.(data.ui as UiDirective);
        const turnId = ++replySeq;
        const statsStr = [data.command ? `command: ${data.command}` : "", data.seconds ? `${data.seconds}s` : ""].filter(Boolean).join(" · ") || undefined;
        onFeedChange?.((prev) => [
          ...prev,
          { id: `bmic-u-${turnId}`, role: "user", text: data.prompt || "(voice utterance)" },
          {
            id: `bmic-a-${turnId}`,
            role: "assistant",
            text: data.answer || "(no spoken reply)",
            command: data.command,
            paths: data.paths || [],
            decision: data.decision,
            permissionRequest: data.permission_request,
            approvalRequest: data.approval,
            task: data.task,
            stats: statsStr,
          },
        ]);
        setCard({
          id: turnId,
          user: data.prompt || "(voice utterance)",
          answer: data.answer || "(no spoken reply)",
          streaming: false,
          tools: [],
          sources: data.paths || [],
          memories: data.memory_ids?.length || 0,
          knowledge: (data.knowledge_sources || []).map((k: { title: string }) => k.title),
          web: data.web_sources || [],
          decision: data.decision,
          stats: statsStr,
        });
        setCardOpen(!inAssistantView);
      } catch (e) {
        onNotify((e as Error).message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      onNotify("Browser microphone access unavailable; use host Mic or text dock.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(2048, 1, 1);
      const chunks: Float32Array[] = [];
      proc.onaudioprocess = (ev) => {
        const inp = ev.inputBuffer.getChannelData(0);
        chunks.push(new Float32Array(inp));
        let sum = 0;
        for (let i = 0; i < inp.length; i++) sum += inp[i] * inp[i];
        onAudioLevel?.(Math.min(1, Math.sqrt(sum / inp.length) * 4.5));
      };
      const [track] = stream.getAudioTracks();
      if (track) {
        track.onended = () => {
          if (webMicRef.current?.stream === stream) {
            webMicRef.current = null;
            setBrowserRec(false);
            onBrowserRecChange?.(false);
            onAudioLevel?.(0);
            ctx.close().catch(() => {});
            onNotify("Microphone disconnected.");
          }
        };
      }
      src.connect(proc);
      const muteGain = ctx.createGain();
      muteGain.gain.setValueAtTime(0, ctx.currentTime);
      proc.connect(muteGain);
      muteGain.connect(ctx.destination);
      webMicRef.current = { stream, ctx, chunks, rate: ctx.sampleRate };
      setBrowserRec(true);
      onBrowserRecChange?.(true);
    } catch (e) {
      onNotify(`Mic access denied: ${(e as Error).message}`);
    }
  };

  const openNote = (title: string) => {
    const n = [...graph.nodes.values()].find((x) => x.type === "note" && x.label.toLowerCase() === title.toLowerCase());
    if (n) onInspect?.({ kind: "node", id: n.id });
    else onNotify(`No note named "${title}".`);
  };

  return (
    <div className="voice-dock-wrap">
      {card && cardOpen && !inAssistantView && (
        <section className="dock-response card" aria-label="Latest Jarvis response">
          <div className="row between">
            <div className="row wrap">
              <span className="eyebrow">Command · {card.user.slice(0, 80)}</span>
              {card.decision && <span className="pill info">{card.decision.department} · {card.decision.intent}</span>}
              {card.streaming && <span className="status info"><i className="dot" /> {assistantState}</span>}
            </div>
            <div className="row">
              <button className="ghost" onClick={() => onNavigate?.("assistant")}>Open Assistant</button>
              <button className="ghost icon" aria-label="Collapse response card" onClick={() => setCardOpen(false)}>✕</button>
            </div>
          </div>
          <div className="dock-response-body">
            {card.tools.map((t, i) => <div key={i} className="tool-line mono">tool · {t}</div>)}
            {card.error ? (
              <p className="err-text">{card.error}</p>
            ) : card.answer ? (
              <Markdown text={card.answer} onNote={openNote} />
            ) : (
              <span className="typing" aria-label="Generating"><i /><i /><i /></span>
            )}
            {(card.sources.length > 0 || card.memories > 0 || card.knowledge.length > 0 || card.web.length > 0) && (
              <div className="row wrap msg-src">
                {card.sources.map((s) => <button key={s} className="chip" onClick={() => openNote(s)}>note: {s}</button>)}
                {card.memories > 0 && <span className="chip static">{card.memories} memor{card.memories === 1 ? "y" : "ies"}</span>}
                {card.knowledge.map((k) => <span key={k} className="chip static">kb: {k}</span>)}
                {card.web.map((w) => <a key={w.url} className="chip" href={w.url} target="_blank" rel="noreferrer">{w.title || w.url}</a>)}
              </div>
            )}
          </div>
          {!card.streaming && (
            <div className="row between dim mono">
              <span>{card.stopped ? "stopped" : card.stats}</span>
              <div className="row">
                {card.answer && <button className="ghost" onClick={() => navigator.clipboard.writeText(card.answer).then(() => onNotify("Copied."))}>Copy</button>}
                {card.answer && <button className="ghost" onClick={() => apiFetch("/speak", { method: "POST", body: { text: card.answer.slice(0, 2000) } })}>Speak</button>}
              </div>
            </div>
          )}
        </section>
      )}

      <div className="voice-dock" role="region" aria-label="Voice Command Dock">
        <div className="dock-telemetry row between">
          <div className="row">
            <span className={`pill ${assistantState === "Error" ? "danger" : assistantState.includes("approval") ? "warn" : assistantState !== "Idle" ? "info" : ""}`}>
              {assistantState}{stateDetail ? ` · ${stateDetail}` : ""}
            </span>
            {decisionBadge && <span className="mono dim">route: {decisionBadge}</span>}
            {liveTranscript && <span className="dock-transcript mono" title={liveTranscript}>“{liveTranscript.slice(0, 72)}{liveTranscript.length > 72 ? "…" : ""}”</span>}
          </div>
          <div className="row">
            <canvas ref={waveCanvas} width={160} height={22} className="wave-canvas" aria-hidden="true" />
            {card && !cardOpen && (
              <button className="ghost" onClick={() => setCardOpen(true)}>Last Reply</button>
            )}
          </div>
        </div>

        <form
          className="dock-form row"
          onSubmit={(e) => {
            e.preventDefault();
            sendCommand(input);
          }}
        >
          <input
            ref={inputRef}
            value={input}
            maxLength={4000}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask Jarvis... Find a file, inspect a project, remember something, or generate a report"
            aria-label="Command or ask Jarvis"
          />
          <button
            type="button"
            className={voiceLoopState.mic ? "danger" : "ghost"}
            onClick={toggleHostMic}
            title="Toggle continuous Jarvis host microphone loop"
          >
            {voiceLoopState.mic ? "Host Mic On" : "Host Mic"}
          </button>
          {(voiceLoopState.state === "ERROR" || voiceLoopState.error) && (
            <button type="button" className="warn ghost" onClick={recoverMic} title="Recover failed microphone stream">
              Recover Mic
            </button>
          )}
          <button
            type="button"
            className={browserRec ? "danger" : "ghost"}
            onClick={toggleBrowserMic}
            title="Click to record voice command in browser, click again to send"
          >
            {browserRec ? "Send Voice" : "Speak"}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => apiFetch("/voice/ptt", { method: "POST" }).catch((e) => onNotify((e as Error).message))}
            title="Trigger one-shot Push-to-Talk utterance on host mic"
          >
            PTT
          </button>
          <button
            type="button"
            className="ghost"
            aria-pressed={speak}
            onClick={() => {
              const next = !speak;
              localStorage.setItem(SPEAK_KEY, next ? "1" : "0");
              setSpeak(next);
            }}
            title="Toggle spoken TTS replies"
          >
            TTS {speak ? "On" : "Off"}
          </button>
          {busy || ["Speaking", "Executing", "Thinking", "Planning", "Transcribing"].includes(assistantState) ? (
            <button type="button" className="danger" onClick={stopAll}>Interrupt</button>
          ) : (
            <button type="submit" disabled={!input.trim()}>Send</button>
          )}
        </form>
      </div>
    </div>
  );
};

