// Jarvis command centre: knowledge rail, HUD reactor, command bar, conversation. No external requests.
import { $, $$, api, chatStream, colorOf, el, emit, listen, on, store, toast } from "./api.js";
import { galaxy } from "./galaxy.js";
import { mdInto } from "./md.js";
import { closeDrawers, openDrawer, openNote } from "./panels.js";

const S = { voice: "off", activity: "idle", speaking: false, online: false, wake: true, generating: false, heard: "" };
const state = () => (S.speaking ? "speaking" : S.activity === "thinking" ? "thinking" : S.voice);
const LABEL = { off: "standby", listening: "listening", recording: "hearing you", transcribing: "transcribing", thinking: "thinking", speaking: "speaking" };

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

function render() {
  const st = state();
  $("#hud").className = st + (S.backend ? "" : " down");
  if (st !== "recording") setLevel(0);
  $("#h-state").textContent = LABEL[st] || st; $("#c-state").textContent = S.heard && st === "thinking" ? `heard: “${S.heard}”` : LABEL[st] || st;
  $("#c-state").classList.toggle("busy", st !== "off" && st !== "listening");
  const micOn = S.voice !== "off";
  $("#c-mic").setAttribute("aria-pressed", String(micOn)); $("#h-mic").textContent = micOn ? S.voice : "off";
  const w = $("#c-wake"); w.classList.toggle("on", micOn && S.wake);
  w.textContent = !micOn ? "wake word off" : S.wake ? "listening for “hey jarvis”" : "microphone on · push to talk";
  $("#c-halt").hidden = !(S.generating || S.speaking);
}

async function refreshStatus() {
  try {
    const s = await api("/status");
    S.backend = true; S.online = s.ollama.up; S.voice = s.voice.state; S.wake = s.voice.wake_enabled; S.speaking = s.voice.speaking;
    $("#st-backend").classList.add("ok"); $("#st-ollama").classList.toggle("ok", s.ollama.up); $("#st-offline").classList.toggle("ok", s.offline_enforced);
    $("#h-online").classList.toggle("ok", s.ollama.up); $("#h-online b").textContent = s.ollama.up ? "ONLINE" : "OLLAMA DOWN";
    $("#h-model b").textContent = s.brain.model.toUpperCase();
    $("#h-voice").textContent = s.tts.enabled ? s.tts.voice : "muted in settings";
    const rx = $("#m-reflex"); rx.setAttribute("aria-pressed", String(!s.brain.thinking)); rx.querySelector("b").textContent = s.brain.thinking ? "deep" : "fast";
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
      else if (ev.type === "token") { text += ev.text; if (!raf) raf = requestAnimationFrame(paint); }
      else if (ev.type === "error") { if (m && !text) m.remove(); add("err", ev.message); bubble(ev.message, "err"); hideBubble(); }
      else if (ev.type === "done") {
        cancelAnimationFrame(raf); text = ev.answer; paint();
        const info = el("small"), copy = el("button", null, "copy");
        info.append(el("span", null, [ev.command ? "command" : ev.model, ev.seconds + "s", ev.tps ? ev.tps + " tok/s" : "", ev.truncated ? "cut off" : ""].filter(Boolean).join(" · ")), copy);
        copy.onclick = async () => { try { await navigator.clipboard.writeText(text); copy.textContent = "copied"; } catch { toast("Clipboard is not available."); } };
        if (meta.sources?.length) { const s = el("button", null, "from: " + meta.sources.join(", ")); s.onclick = () => galaxy.focus(ev.paths); info.append(s); }
        m.append(info); hideBubble();
        if (ev.ui?.open === "notes") openDrawer("notes");
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
$("#c-halt").onclick = async () => { abort?.abort(); try { await api("/speak/stop", { method: "POST" }); } catch {} };
$("#c-ptt").onclick = async () => { try { await api("/voice/ptt", { method: "POST" }); } catch (e) { toast(e.message); } };
$("#c-mic").onclick = async () => {
  try { const r = await api("/voice/mic", { method: "POST", body: { enabled: S.voice === "off" } }); if (r.error) toast(r.error); S.voice = r.state; render(); }
  catch (e) { toast(e.message); }
};
const voiceBtn = $("#c-voice"); voiceBtn.setAttribute("aria-pressed", String(store.get("speak", true)));
voiceBtn.onclick = () => { const v = voiceBtn.getAttribute("aria-pressed") !== "true"; voiceBtn.setAttribute("aria-pressed", String(v)); voiceBtn.textContent = v ? "🔊" : "🔇"; store.set("speak", v); };
voiceBtn.textContent = store.get("speak", true) ? "🔊" : "🔇";
$("#ch-clear").onclick = async () => { try { await api("/history", { method: "DELETE" }); log.replaceChildren(); add("sys", "Conversation cleared."); } catch (e) { toast(e.message); } };
addEventListener("keydown", (e) => { if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $("#input").focus(); } });

// ================= live events from the backend =================
let voiceTurn = null;
listen(async (ev) => {
  switch (ev.type) {
    case "hello": S.voice = ev.voice; S.activity = ev.activity; break;
    case "voice_state": S.voice = ev.state; break;
    case "activity": S.activity = ev.state; break;
    case "speaking": S.speaking = ev.on; break;
    case "level": if (S.voice === "recording") setLevel(ev.v); return;
    case "transcript": S.heard = ev.text; if (!ev.text) { bubble("I didn't catch that."); hideBubble(3000); } break;
    case "user": add("user", ev.text); voiceTurn = turn(); break;
    case "meta": case "token": case "done": if (voiceTurn) await voiceTurn.event(ev); if (ev.type === "done") voiceTurn = null; break;
    case "error": if (voiceTurn) { await voiceTurn.event(ev); voiceTurn = null; } else toast(ev.message); break;
    case "graph_changed": await galaxy.refresh(ev.new || []); break;
    case "model": refreshStatus(); break;
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
    add("sys", `Hello, sir. ${galaxy.nodes.length} notes indexed.`);  // the count comes from the live graph
  } catch (e) { toast("Backend not reachable: " + e.message); }
  await refreshStatus();
})();
