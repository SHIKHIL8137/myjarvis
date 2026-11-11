// Jarvis command centre: knowledge rail, HUD reactor, command bar, conversation, transcript & online status.
import { $, $$, api, chatStream, colorOf, el, emit, listen, on, store, toast } from "./api.js";
import { galaxy } from "./galaxy.js";
import { mdInto } from "./md.js";
import { closeDrawers, openDrawer, openNote } from "./panels.js";
import "./learning.js";

const S = {
  voice: "off",
  audioState: "IDLE",
  voiceMode: "continuous",
  paused: false,
  micSuspended: false,
  duplex: { effective: "half_duplex", barge_in_available: false, aec_verified: false, fallback: false },
  ttsStatus: { speaking: false, queued: 0, playing_chunk: 0, chunks_queued: 0, chunks_played: 0 },
  activity: "idle",
  speaking: false,
  online: false,
  webOnline: false,
  provider: "duckduckgo",
  wake: true,
  generating: false,
  heard: "",
  voiceError: null,
  lastTranscriptId: null,
};
const formalState = () => {
  if (S.audioState === "ERROR") return "ERROR";
  if (S.paused || S.audioState === "PAUSED") return "PAUSED";
  if (S.speaking || S.audioState === "SPEAKING") return "SPEAKING";
  if (S.audioState === "GENERATING" || S.activity === "thinking" || S.generating) return "GENERATING";
  if (S.audioState && S.audioState !== "IDLE") return S.audioState;
  return "IDLE";
};

// ================= HUD =================
const bars = $("#bars"), NS = "http://www.w3.org/2000/svg", BARS = 44;
for (let i = 0; i < BARS; i++) {
  const g = document.createElementNS(NS, "g"), r = document.createElementNS(NS, "rect");
  g.setAttribute("transform", `rotate(${(360 / BARS) * i} 100 100)`);
  r.setAttribute("x", "99.1"); r.setAttribute("y", "34"); r.setAttribute("width", "1.8"); r.setAttribute("height", "13");
  r.style.setProperty("--d", `${-(i * 37 % 11) / 10}s`); r.style.setProperty("--m", (0.7 + ((i * 53) % 10) / 9).toFixed(2));
  r.dataset.k = (0.5 + ((i * 29) % 10) / 10).toFixed(2);
  g.append(r); bars.append(g);
}
const setLevel = (v) => $$("#bars rect").forEach((r) => r.style.setProperty("--s", (0.25 + v * 2.4 * r.dataset.k).toFixed(2)));

function renderInternet(web) {
  if (!web) return;
  S.webOnline = Boolean(web.enabled);
  S.provider = web.provider || "duckduckgo";
  const hm = $("#h-mode"), so = $("#st-offline"), sot = $("#st-offline-t"), mw = $("#m-web");
  hm.classList.toggle("ok", !S.webOnline);
  hm.classList.toggle("web", S.webOnline);
  hm.querySelector("b").textContent = S.webOnline ? "ONLINE ASSISTED" : "LOCAL";
  so.classList.toggle("ok", !S.webOnline);
  so.classList.toggle("web", S.webOnline);
  sot.textContent = S.webOnline ? `online (${S.provider})` : "local mode";
  mw.setAttribute("aria-pressed", String(S.webOnline));
  mw.querySelector("b").textContent = S.webOnline ? "on" : "off";
}

function renderTranscript(rec) {
  if (!rec || !rec.original_transcript) return;
  S.lastTranscriptId = rec.id;
  const box = $("#transcript-box");
  box.hidden = false;
  const prob = rec.language_probability ? ` · ${Math.round(rec.language_probability * 100)}%` : "";
  $("#tr-lang").textContent = `lang: ${rec.detected_language || "?"}${prob}`;
  $("#tr-conf").textContent = `conf: ${Math.round((rec.confidence || 0) * 100)}%`;
  $("#tr-model").textContent = `${rec.model || "whisper"} · ${rec.seconds ?? 0}s`;
  $("#tr-orig").textContent = rec.original_transcript;
  $("#tr-corr").value = rec.corrected_transcript || rec.original_transcript;
  if (rec.target_language && rec.target_language !== "none") {
    $("#tr-quick-target").value = rec.target_language;
  }
  const hasTr = Boolean(rec.translated_text && rec.target_language && rec.target_language !== "none");
  $("#tr-trans-row").hidden = !hasTr;
  if (hasTr) {
    $("#tr-trans-lbl").textContent = `Translation (${rec.target_language}${rec.translation_engine ? " · " + rec.translation_engine : ""}):`;
    $("#tr-trans").textContent = rec.translated_text;
  }
}

function render() {
  const st = formalState();
  const sessionOpen = !["IDLE", "ERROR"].includes(S.audioState) || S.voice !== "off";
  $("#hud").className = st.toLowerCase() + (S.backend ? "" : " down");
  if (st !== "SPEECH_DETECTED") setLevel(0);
  $("#h-state").textContent = st;
  $("#h-vmode").textContent = `${S.voiceMode} (${S.duplex?.effective === "full_duplex" ? "duplex" : "half"})`;
  if ($("#c-vmode").value !== S.voiceMode) $("#c-vmode").value = S.voiceMode;
  const stEl = $("#c-state");
  const chunkNote = st === "SPEAKING" && S.ttsStatus?.chunks_queued
    ? ` · chunk ${Math.max(1, S.ttsStatus.playing_chunk || 1)}/${S.ttsStatus.chunks_queued}${S.ttsStatus.queued > 1 ? ` (+${S.ttsStatus.queued - 1} q)` : ""}`
    : "";
  stEl.textContent = S.heard && (st === "PROCESSING" || st === "GENERATING")
    ? `${st} · “${S.heard}”`
    : `${st}${chunkNote}`;
  stEl.classList.toggle("busy", ["SPEECH_DETECTED", "PROCESSING", "GENERATING", "SPEAKING", "INTERRUPTED", "RESUMING"].includes(st));
  stEl.classList.toggle("err", st === "ERROR");
  stEl.classList.toggle("paused", st === "PAUSED");
  $("#c-mic").setAttribute("aria-pressed", String(sessionOpen && !S.paused && !S.micSuspended));
  $("#h-mic").textContent = S.paused
    ? "paused"
    : S.micSuspended
      ? "suspended (half-duplex)"
      : (sessionOpen ? st.toLowerCase() : "off");
  const pBtn = $("#c-pause");
  pBtn.hidden = !sessionOpen;
  pBtn.setAttribute("aria-pressed", String(S.paused));
  pBtn.textContent = S.paused ? "▶" : "⏸";
  pBtn.title = S.paused ? "Resume hands-free listening" : "Pause hands-free listening";
  const w = $("#c-wake");
  w.classList.toggle("on", sessionOpen && !S.paused && !S.micSuspended && !S.voiceError);
  const modeLbl = S.voiceMode.replace("_", "-");
  const halfDup = (S.duplex?.effective || "half_duplex") !== "full_duplex";
  if (S.voiceError) {
    w.textContent = `voice error · ${S.voiceError}`;
  } else if (!sessionOpen) {
    w.textContent = `${modeLbl} · ${halfDup ? "safe half-duplex" : "full-duplex AEC"} · click mic`;
  } else if (S.paused) {
    w.textContent = `${modeLbl} · paused`;
  } else if (S.micSuspended || (halfDup && (st === "PROCESSING" || st === "GENERATING" || st === "SPEAKING"))) {
    w.textContent = st === "SPEAKING"
      ? `half-duplex · mic suspended (use ■ button to stop)`
      : `half-duplex · mic suspended during turn`;
  } else if (st === "RESUMING") {
    w.textContent = "resuming microphone after playback…";
  } else if (st === "WAKE_WORD") {
    w.textContent = "listening for “hey jarvis”";
  } else if (S.voiceMode === "continuous") {
    w.textContent = S.duplex?.fallback
      ? "continuous · half-duplex fallback (AEC unavailable)"
      : `hands-free continuous · ${halfDup ? "safe half-duplex" : "verified full-duplex"}`;
  } else {
    w.textContent = `${modeLbl} · active`;
  }
  $("#c-halt").hidden = !(S.generating || S.speaking || ["PROCESSING", "GENERATING", "SPEAKING"].includes(st));
}

async function refreshStatus() {
  try {
    const s = await api("/status");
    S.backend = true; S.online = s.ollama.up; S.voice = s.voice.state;
    S.audioState = s.voice.audio_state || S.audioState;
    S.voiceMode = s.voice.mode || S.voiceMode;
    S.paused = Boolean(s.voice.paused);
    S.micSuspended = Boolean(s.voice.mic_suspended);
    if (s.voice.duplex) S.duplex = s.voice.duplex;
    if (s.voice.tts_status) S.ttsStatus = s.voice.tts_status;
    S.voiceError = s.voice.error || null;
    S.wake = s.voice.wake_enabled; S.speaking = s.voice.speaking;
    $("#st-backend").classList.add("ok"); $("#st-ollama").classList.toggle("ok", s.ollama.up);
    $("#h-online").classList.toggle("ok", s.ollama.up); $("#h-online b").textContent = s.ollama.up ? "OLLAMA OK" : "OLLAMA DOWN";
    $("#h-model b").textContent = s.brain.model.toUpperCase();
    $("#h-voice").textContent = s.tts.enabled ? s.tts.voice : "muted in settings";
    if (s.stt) {
      const tr = s.translation?.mode === "translate" && s.translation?.target_language !== "none" ? ` → ${s.translation.target_language}` : "";
      $("#h-stt").textContent = `${s.stt.model} · ${s.stt.language}${tr}`;
    }
    const rx = $("#m-reflex"); rx.setAttribute("aria-pressed", String(!s.brain.thinking)); rx.querySelector("b").textContent = s.brain.thinking ? "deep" : "fast";
    renderInternet(s.internet);
  } catch {
    S.online = S.backend = false; $("#st-backend").classList.remove("ok"); $("#h-online").classList.remove("ok"); $("#h-online b").textContent = "BACKEND DOWN";
  }
  render();
}
on("status", refreshStatus);
setInterval(() => !document.hidden && refreshStatus(), 30000);

// modes: each one does something real, or says plainly that it does not exist
const mode = (id, fn) => { const b = $(id); b.onclick = async () => { const v = b.getAttribute("aria-pressed") !== "true"; if ((await fn(v)) !== false) { b.setAttribute("aria-pressed", String(v)); if (id !== "#m-reflex") b.querySelector("b").textContent = v ? "on" : "off"; } }; };
mode("#m-holo", (v) => { document.body.classList.toggle("holo", v); galaxy.holo(v); $("#d-rotate").checked = v || $("#d-rotate").checked; });
mode("#m-focus", (v) => { document.body.classList.toggle("focus", v); if (v) closeDrawers(); });
mode("#m-reflex", async (fast) => {
  try { await api("/models/switch", { method: "POST", body: { model: fast ? "local" : "local_thinking" } }); await refreshStatus(); sys(fast ? "Reflex on: fast model." : "Reflex off: thinking model (slower, reasons first)."); }
  catch (e) { toast(e.message); return false; }
});
mode("#m-web", async (enable) => {
  try {
    const st = await api("/internet/mode", { method: "POST", body: { enabled: enable } });
    renderInternet(st);
    sys(enable ? `Online Assisted Mode enabled (${st.provider}).` : "Local Mode active: all outbound internet requests blocked.");
  } catch (e) { toast(e.message); return false; }
});

// ================= left rail =================
const hint = () => [el("div", "dim", "Click a star to focus it: only that note and its connections stay lit, and you can read it here. Double-click to explore two steps out.")];
function inspect(n) {
  const box = $("#inspector");
  if (!n) return box.replaceChildren(...hint());
  const cat = el("div", "cat", n.group); cat.style.color = colorOf(n.group);
  const open = el("button", null, "Open note"); open.onclick = () => { openDrawer("notes"); openNote(n.path, false); };
  const rel = el("div", "rel");
  rel.append(...galaxy.neighborsOf(n).map((o) => { const b = el("button", "tag", o.label); b.onclick = () => galaxy.select(o.path); return b; }));
  box.replaceChildren(el("div", "title", n.label), cat, el("p", null, n.excerpt), open, el("h2", null, `Related (${rel.children.length})`), rel);
}
on("select", inspect);
on("explore", (k) => sys(`Exploring ${k} notes within two steps.`));
on("rotate", (v) => ($("#d-rotate").checked = v));
on("graph", ({ nodes, links }) => {
  const groups = {}; nodes.forEach((n) => (groups[n.group] = (groups[n.group] || 0) + 1));
  $("#brand-sub").textContent = `${nodes.length} notes · ${links.length} connections`;
  const row = (label, count, color, fn) => { const b = el("button"), i = el("i"); i.style.setProperty("--c", color); b.append(i, el("span", null, label), el("b", null, count)); b.onclick = () => fn(b); return b; };
  $("#hubs").replaceChildren(...galaxy.hubs(8).map((n) => row(n.label, galaxy.degree(n), colorOf(n.group), () => galaxy.select(n.path))));
  $("#cats").replaceChildren(...Object.keys(groups).sort().map((g) => row(g, groups[g], colorOf(g), (b) => b.classList.toggle("off", !galaxy.toggleGroup(g)))));
  const top = galaxy.hubs(1)[0], iso = nodes.filter((n) => !galaxy.degree(n)).length;
  const stat = (k, v) => [el("span", null, k), el("b", null, v)];
  $("#stats").replaceChildren(...stat("Notes", nodes.length), ...stat("Knowledge nodes", nodes.length), ...stat("Connections", links.length),
    ...stat("Categories", Object.keys(groups).length), ...stat("Links per note", nodes.length ? ((2 * links.length) / nodes.length).toFixed(1) : "0"),
    ...stat("Unlinked notes", iso), ...stat("Most connected", top ? top.label : "—"));
});
$("#search").oninput = (e) => {
  const hits = galaxy.search(e.target.value);
  if (!e.target.value.trim()) return inspect(null);
  const rel = el("div", "rel"); rel.append(...hits.slice(0, 12).map((n) => { const b = el("button", "tag", n.label); b.onclick = () => galaxy.select(n.path); return b; }));
  $("#inspector").replaceChildren(el("div", "title", `${hits.length} match${hits.length === 1 ? "" : "es"}`), el("div", "dim", "Enter opens the best one."), rel);
  $("#search").hits = hits;
};
$("#search").onkeydown = (e) => { if (e.key === "Enter" && $("#search").hits?.length) galaxy.select($("#search").hits[0].path); };
const forces = () => galaxy.forces(+$("#f-repel").value, +$("#f-link").value);
$("#f-repel").onchange = forces; $("#f-link").onchange = forces;
$("#d-labels").onchange = (e) => galaxy.setLabels(e.target.checked);
$("#d-rotate").onchange = (e) => galaxy.rotate(e.target.checked);
$("#d-links").onchange = (e) => galaxy.setLinks(e.target.checked);
$("#left-toggle").onclick = () => { const c = document.body.classList.toggle("left-closed"); $("#left-toggle").textContent = c ? "»" : "«"; $("#left-toggle").setAttribute("aria-expanded", String(!c)); };
$("#v-fit").onclick = () => galaxy.fit(); $("#v-reset").onclick = () => galaxy.reset();
if (innerWidth < 1100) $("#left-toggle").click();

// ================= conversation =================
const log = $("#log");
const add = (cls, text) => { const m = el("div", "msg " + cls, text); log.append(m); log.scrollTop = log.scrollHeight; return m; };
const sys = (t) => { add("sys", t); bubble(t); };
const noteLink = (title) => { const t = galaxy.byTitle(title); t ? galaxy.select(t.path) : toast(`No note called “${title}”.`); };
let bubbleTimer;
function bubble(text, cls = "") {
  const r = $("#reply"); clearTimeout(bubbleTimer); r.className = "glass " + cls; r.hidden = false;
  if (text === null) r.replaceChildren(typing()); else r.textContent = text.length > 320 ? text.slice(0, 320) + "…" : text;
}
const hideBubble = (ms = 9000) => { clearTimeout(bubbleTimer); bubbleTimer = setTimeout(() => ($("#reply").hidden = true), ms); };
$("#reply").onclick = () => openDrawer("chat");
function typing() { const t = el("span", "typing"); t.append(el("i"), el("i"), el("i")); return t; }

function renderWebSources(container, sources) {
  if (!sources?.length) return;
  const wrap = el("div", "sources");
  sources.forEach((s) => {
    if (typeof s.url !== "string" || !s.url.startsWith("https://")) return;
    const a = el("a", "src-link");
    a.href = s.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    let host = s.url;
    try { host = new URL(s.url).hostname.replace(/^www\./, ""); } catch {}
    a.textContent = `${s.title || host} (${host})`;
    a.title = [s.title, s.published_date, s.snippet].filter(Boolean).join(" — ");
    wrap.append(a);
  });
  if (wrap.children.length) container.append(wrap);
}

/** One handler per turn; used for typed turns (stream) and for voice turns (server events). */
function turn() {
  let m = null, text = "", meta = {}, raf = 0;
  const paint = () => { raf = 0; if (m) { mdInto(m.body, text, noteLink); log.scrollTop = log.scrollHeight; } bubble(text.replace(/```[\s\S]*?(```|$)/g, " [code] ").replace(/[*_`#]/g, "")); };
  const ensure = () => { if (!m) { m = add("jarvis"); m.body = el("div"); m.body.append(typing()); m.append(m.body); bubble(null); } return m; };
  ensure();
  return {
    stopped() { if (m) { cancelAnimationFrame(raf); mdInto(m.body, text || "*(stopped)*", noteLink); m.append(el("small", null, "stopped")); } hideBubble(2500); },
    async event(ev) {
      if (ev.type === "meta") meta = ev;
      else if (ev.type === "tool") { bubble(`Searching web: ${ev.query || ev.name}…`); $("#c-state").textContent = `searching: ${ev.query || "web"}`; }
      else if (ev.type === "reset_answer") { cancelAnimationFrame(raf); text = ""; if (m) m.body.replaceChildren(typing()); }
      else if (ev.type === "token") { text += ev.text; if (!raf) raf = requestAnimationFrame(paint); }
      else if (ev.type === "error") { if (m && !text) m.remove(); add("err", ev.message); bubble(ev.message, "err"); hideBubble(); }
      else if (ev.type === "done") {
        cancelAnimationFrame(raf); text = ev.answer; paint();
        renderWebSources(m, ev.web_sources);
        const info = el("small"), copy = el("button", null, "copy");
        info.append(el("span", null, [ev.command ? "command" : ev.model, ev.seconds + "s", ev.tps ? ev.tps + " tok/s" : "", ev.truncated ? "cut off" : ""].filter(Boolean).join(" · ")), copy);
        copy.onclick = async () => { try { await navigator.clipboard.writeText(text); copy.textContent = "copied"; } catch { toast("Clipboard is not available."); } };
        if (meta.sources?.length) { const s = el("button", null, "from: " + meta.sources.join(", ")); s.onclick = () => galaxy.focus(ev.paths); info.append(s); }
        m.append(info); hideBubble();
        if (ev.ui?.open === "notes") openDrawer("notes");
        if (ev.ui?.open === "learning") openDrawer("learning");
        if (ev.ui?.open === "workspace") openDrawer("workspace");
        if (ev.ui?.voice) refreshStatus();
        if (ev.ui?.focus === "search") { document.body.classList.remove("left-closed", "focus"); $("#search").focus(); }
        if (ev.ui?.search) { $("#search").value = ev.ui.search; }
        if (ev.paths?.length) await galaxy.focus(ev.paths, ev.command === "remember", ev.command === "find");  // small talk has no paths: the camera stays put
      }
    },
  };
}

let abort = null;
async function send(message) {
  add("user", message); S.heard = "";
  const t = turn(); abort = new AbortController(); S.generating = true; render();
  try { await chatStream(message, $("#c-voice").getAttribute("aria-pressed") === "true", (ev) => t.event(ev), abort.signal); }
  catch (e) { if (e.name === "AbortError") t.stopped(); else { add("err", "Request failed: " + e.message); bubble("Request failed: " + e.message, "err"); hideBubble(); } }
  finally { S.generating = false; abort = null; render(); }
}
$("#cmd").onsubmit = (e) => { e.preventDefault(); const v = $("#input").value.trim(); if (!v || S.generating) return; $("#input").value = ""; send(v); };
$("#c-vmode").onchange = async (e) => {
  const m = e.target.value, prev = S.voiceMode;
  S.voiceMode = m;
  try {
    await api("/config", { method: "PUT", body: { audio: { voice_mode: m }, wake: { enabled: m === "wake_word" } } });
    const sessionOpen = !["IDLE", "ERROR"].includes(S.audioState) || S.voice !== "off";
    if (sessionOpen) {
      const r = await api("/voice/session", { method: "POST", body: { action: "mode", mode: m } });
      S.voice = r.state; S.audioState = r.audio_state || S.audioState; S.voiceMode = r.mode || m; S.paused = Boolean(r.paused);
    }
    sys(`Voice mode switched to ${m.replace("_", " ")}.`);
    render();
  } catch (err) {
    S.voiceMode = prev; $("#c-vmode").value = prev; toast(err.message);
  }
};
$("#c-halt").onclick = async () => {
  abort?.abort();
  try { await api("/voice/interrupt", { method: "POST" }); } catch {}
  try { await api("/speak/stop", { method: "POST" }); } catch {}
};
$("#c-pause").onclick = async () => {
  try {
    const action = S.paused ? "resume" : "pause";
    const r = await api("/voice/session", { method: "POST", body: { action } });
    S.voice = r.state; S.audioState = r.audio_state || (S.paused ? "LISTENING" : "PAUSED");
    S.paused = Boolean(r.paused); S.voiceMode = r.mode || S.voiceMode;
    if (r.error) S.voiceError = r.error;
    render();
  } catch (e) { toast(e.message); }
};
$("#c-ptt").onclick = async () => {
  try {
    const r = await api("/voice/ptt", { method: "POST" });
    S.voice = r.state; if (r.audio_state) S.audioState = r.audio_state; if (r.mode) S.voiceMode = r.mode;
    S.paused = false; S.voiceError = null; render();
  } catch (e) { toast(e.message); }
};
$("#c-mic").onclick = async () => {
  const sessionOpen = !["IDLE", "ERROR"].includes(S.audioState) || S.voice !== "off";
  try {
    const action = sessionOpen ? "stop" : "start";
    const r = await api("/voice/session", { method: "POST", body: { action, mode: $("#c-vmode").value } });
    if (r.error) { S.voiceError = r.error; toast(r.error); } else S.voiceError = null;
    S.voice = r.state; S.audioState = r.audio_state || (action === "start" ? "LISTENING" : "IDLE");
    S.voiceMode = r.mode || S.voiceMode; S.paused = Boolean(r.paused);
    render();
  } catch (e) { toast(e.message); }
};
const voiceBtn = $("#c-voice"); voiceBtn.setAttribute("aria-pressed", String(store.get("speak", true)));
voiceBtn.onclick = () => { const v = voiceBtn.getAttribute("aria-pressed") !== "true"; voiceBtn.setAttribute("aria-pressed", String(v)); voiceBtn.textContent = v ? "🔊" : "🔇"; store.set("speak", v); };
voiceBtn.textContent = store.get("speak", true) ? "🔊" : "🔇";
$("#ch-clear").onclick = async () => { try { await api("/history", { method: "DELETE" }); log.replaceChildren(); add("sys", "Conversation cleared."); } catch (e) { toast(e.message); } };

// Transcript correction & translation actions
$("#tr-apply").onclick = async () => {
  if (!S.lastTranscriptId) return;
  try {
    const rec = await api("/voice/correct", {
      method: "POST",
      body: { id: S.lastTranscriptId, corrected_transcript: $("#tr-corr").value, target_language: $("#tr-quick-target").value },
    });
    renderTranscript(rec);
    toast("Transcript updated (original preserved).");
  } catch (e) { toast(e.message); }
};
$("#tr-translate-btn").onclick = async () => {
  if (!S.lastTranscriptId) return;
  try {
    const rec = await api("/voice/translate", {
      method: "POST",
      body: { id: S.lastTranscriptId, target_language: $("#tr-quick-target").value },
    });
    renderTranscript(rec);
  } catch (e) { toast(e.message); }
};

addEventListener("keydown", (e) => { if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $("#input").focus(); } });

// ================= live events from the backend =================
let voiceTurn = null;
listen(async (ev) => {
  switch (ev.type) {
    case "hello":
      S.voice = ev.voice; S.audioState = ev.audio_state || "IDLE";
      S.voiceMode = ev.voice_mode || S.voiceMode; S.paused = Boolean(ev.paused);
      S.micSuspended = Boolean(ev.mic_suspended);
      if (ev.duplex) S.duplex = ev.duplex;
      if (ev.tts_status) S.ttsStatus = ev.tts_status;
      S.activity = ev.activity; if (ev.internet) renderInternet(ev.internet); break;
    case "internet": renderInternet(ev); break;
    case "voice_state":
      S.voice = ev.state;
      if (ev.audio_state) S.audioState = ev.audio_state;
      if (ev.mode) S.voiceMode = ev.mode;
      if ("paused" in ev) S.paused = Boolean(ev.paused);
      if ("mic_suspended" in ev) S.micSuspended = Boolean(ev.mic_suspended);
      if (ev.duplex_effective) S.duplex = { ...S.duplex, effective: ev.duplex_effective, barge_in_available: Boolean(ev.barge_in_available), aec_verified: Boolean(ev.aec_verified) };
      if (ev.audio_state && ev.audio_state !== "ERROR") S.voiceError = null;
      break;
    case "tts_status": S.ttsStatus = ev; S.speaking = Boolean(ev.speaking); break;
    case "activity": S.activity = ev.state; break;
    case "speaking": S.speaking = ev.on; break;
    case "level": if (S.voice === "recording" || S.audioState === "SPEECH_DETECTED") setLevel(ev.v); return;
    case "transcript": S.heard = ev.text; if (!ev.text) { bubble("I didn't catch that."); hideBubble(3000); } break;
    case "transcript_record": renderTranscript(ev); break;
    case "interrupted": if (voiceTurn) { voiceTurn.stopped(); voiceTurn = null; } break;
    case "user": add("user", ev.text); voiceTurn = turn(); break;
    case "meta": case "tool": case "reset_answer": case "token": case "done":
      if (voiceTurn) await voiceTurn.event(ev);
      if (ev.type === "done") voiceTurn = null;
      break;
    case "error":
      if (voiceTurn) { await voiceTurn.event(ev); voiceTurn = null; }
      else {
        if (/voice|microphone|audio/i.test(ev.message || "")) S.voiceError = ev.message;
        toast(ev.message);
      }
      break;
    case "graph_changed": await galaxy.refresh(ev.new || []); break;
    case "model": case "voice_changed": refreshStatus(); break;
  }
  render();
}, (up) => { if (up) refreshStatus(); else { S.backend = false; render(); $("#st-backend").classList.remove("ok"); $("#h-online").classList.remove("ok"); $("#h-online b").textContent = "BACKEND DOWN"; } });

// ================= boot =================
(async function boot() {
  inspect(null);
  galaxy.init();
  try {
    await galaxy.refresh();
    const h = await api("/history");
    h.messages.slice(-20).forEach((x) => { if (x.role === "user") add("user", x.content); else { const m = add("jarvis"); mdInto(m, x.content, noteLink); } });
    const tr = await api("/voice/transcripts?limit=1");
    if (tr.records?.length) renderTranscript(tr.records[0]);
    add("sys", `Hello, sir. ${galaxy.nodes.length} notes indexed.`);  // the count comes from the live graph
  } catch (e) { toast("Backend not reachable: " + e.message); }
  await refreshStatus();
})();
