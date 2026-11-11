// Drawers: knowledge explorer (notes) and system (settings + live monitor).
import { $, $$, api, colorOf, el, emit, on, store, toast } from "./api.js";
import { galaxy } from "./galaxy.js";
import { mdInto } from "./md.js";

// ---------- drawer manager: one open at a time, Esc closes ----------
const DRAWERS = ["chat", "notes", "settings"];
export function openDrawer(name) {
  DRAWERS.forEach((d) => { $("#" + d).hidden = d !== name; $("#v-" + d).setAttribute("aria-pressed", String(d === name)); });
  emit("drawer", name);
}
export const closeDrawers = () => openDrawer(null);
export const toggleDrawer = (name) => openDrawer($("#" + name).hidden ? name : null);
DRAWERS.forEach((d) => { $("#v-" + d).onclick = () => toggleDrawer(d); $("#" + d + " .close").onclick = closeDrawers; });
addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawers(); });

// ---------- notes ----------
let cat = null, current = null;
function renderList() {
  const f = $("#n-filter").value.toLowerCase(), groups = [...new Set(galaxy.nodes.map((n) => n.group))].sort();
  $("#n-chips").replaceChildren(...[null, ...groups].map((g) => {
    const b = el("button", null, g || "all"); b.setAttribute("aria-pressed", String(cat === g));
    b.onclick = () => { cat = g; renderList(); }; return b;
  }));
  const items = galaxy.nodes.filter((n) => (!cat || n.group === cat) && (n.label + " " + n.excerpt).toLowerCase().includes(f))
    .sort((a, b) => a.label.localeCompare(b.label));
  $("#n-list").replaceChildren(...(items.length ? items.map((n) => {
    const b = el("button", current === n.path ? "sel" : null), i = el("i"); i.style.setProperty("--c", colorOf(n.group));
    b.append(i, el("span", null, n.label)); b.onclick = () => openNote(n.path); return b;
  }) : [el("p", "dim", "No notes match.")]));
}

export async function openNote(path, selectNode = true) {
  const n = galaxy.node(path); if (!n) return;
  current = path; renderList();
  if (selectNode) galaxy.select(path);  // highlight the matching star
  const main = $("#n-main"), full = await api("/notes/" + n.id);
  const h = el("h1", null, full.title), meta = el("div", "dim", `${full.category} · ${full.path} · ${galaxy.degree(n)} connections`);
  const bar = el("div", "bar"), body = el("div");
  const edit = el("button", null, "Edit"), del = el("button", "danger", "Delete"), show = el("button", null, "Show in galaxy");
  bar.append(edit, del, show);
  mdInto(body, full.body, (title) => { const t = galaxy.byTitle(title); t ? openNote(t.path) : toast(`No note called “${title}”.`); });
  const relH = el("h2", null, "Related notes"), rel = el("div", "chipset");
  rel.append(...galaxy.neighborsOf(n).map((o) => { const b = el("button", null, o.label); b.onclick = () => openNote(o.path); return b; }));
  if (!rel.children.length) rel.append(el("span", "dim", "No connections yet."));
  main.replaceChildren(h, meta, bar, body, relH, rel);
  show.onclick = () => { closeDrawers(); galaxy.select(path); };
  edit.onclick = () => {
    const ta = el("textarea"); ta.value = `# ${full.title}\n\n${full.body}`;
    const save = el("button", null, "Save"), cancel = el("button", "ghost", "Cancel");
    bar.replaceChildren(save, cancel); body.replaceChildren(ta); ta.focus();
    cancel.onclick = () => openNote(path, false);
    save.onclick = async () => { try { await api("/notes/" + n.id, { method: "PUT", body: { text: ta.value } }); await galaxy.refresh(); openNote(path, false); } catch (e) { toast(e.message); } };
  };
  del.onclick = async () => {
    if (!confirm(`Move “${full.title}” to the trash folder?\n\nIt is not erased: the file goes to notes/.trash/.`)) return;
    try { await api("/notes/" + n.id, { method: "DELETE" }); current = null; galaxy.clear(); await galaxy.refresh(); main.replaceChildren(el("p", "dim", "Moved to trash.")); renderList(); }
    catch (e) { toast(e.message); }
  };
}
$("#n-filter").oninput = renderList;
$("#n-new").onclick = () => {
  const c = el("input"); c.value = cat || "captures"; c.maxLength = 30; c.setAttribute("aria-label", "Category");
  const ta = el("textarea"); ta.placeholder = "What should Jarvis remember?";
  const save = el("button", null, "Save to memory"), bar = el("div", "bar");
  bar.append(save);
  $("#n-main").replaceChildren(el("h1", null, "New note"), el("div", "dim", "Category (folder)"), c, el("div", "bar"), ta, bar);
  save.onclick = async () => {
    try { const r = await api("/memory", { method: "POST", body: { text: ta.value, category: c.value || "captures" } }); await galaxy.refresh([r.path]); openNote(r.path); }
    catch (e) { toast(e.message); }
  };
};
on("graph", () => { if (!$("#notes").hidden) renderList(); });
on("drawer", (d) => { if (d === "notes") renderList(); });

// ---------- settings ----------
const accent = (v) => { document.documentElement.dataset.accent = v; store.set("accent", v); };
const motion = (v) => { v ? (document.documentElement.dataset.motion = "reduce") : delete document.documentElement.dataset.motion; store.set("motion", v); };
accent(store.get("accent", "cyan")); motion(store.get("motion", false));
$("#s-accent").onchange = (e) => accent(e.target.value);
$("#s-motion").onchange = (e) => motion(e.target.checked);

let keyMap = {};
function updateProviderUI() {
  const p = $("#s-provider").value;
  $("#s-searxng-row").hidden = p !== "searxng";
  if (p === "duckduckgo") {
    $("#s-key-status").textContent = "API key: not required for DuckDuckGo.";
  } else {
    $("#s-key-status").textContent = `API key (${p}): ${keyMap[p] ? "configured (stored securely outside config)" : "not set"}`;
  }
}

async function loadSettings() {
  $("#s-accent").value = store.get("accent", "cyan"); $("#s-motion").checked = store.get("motion", false);
  const [c, m, d] = await Promise.all([api("/config"), api("/models"), api("/audio/devices")]);
  $("#s-model").replaceChildren(...m.models.map((x) => { const o = new Option(`${x.model}${x.thinking ? " (thinking)" : ""}${x.installed ? "" : " — not installed"}`, x.key, false, x.current); o.disabled = !x.installed; return o; }));
  const ctx = $("#s-ctx"); if (![...ctx.options].some((o) => +o.value === c.num_ctx)) ctx.add(new Option(c.num_ctx)); ctx.value = c.num_ctx;
  $("#s-tts").checked = c.tts.enabled; $("#s-speed").value = c.tts.speed; $("#s-speed-o").textContent = c.tts.speed + "×";
  $("#s-voice").replaceChildren(...(c.voices.length ? c.voices : [c.tts.voice]).map((v) => new Option(v, v, false, v === c.tts.voice)));
  $("#s-vmode").value = c.audio.voice_mode || "continuous";
  $("#s-duplex").value = c.audio.duplex_mode || "half_duplex";
  $("#s-aec").value = c.audio.echo_cancellation || "auto";
  $("#s-resume-delay").value = c.audio.mic_resume_delay_ms ?? 300;
  $("#s-wake").checked = c.wake.enabled; $("#s-thr").value = c.wake.threshold; $("#s-thr-o").textContent = c.wake.threshold;
  $("#s-wfollow").value = c.audio.wake_followup_s ?? 6;
  $("#s-sconfirm").value = c.audio.speech_confirm_ms ?? 180;
  $("#s-sil").value = c.audio.silence_ms;
  $("#s-barge").checked = c.audio.barge_in_enabled !== false;
  $("#s-barge-sens").value = c.audio.barge_in_sensitivity || "medium";
  $("#s-suppress-tts").checked = c.audio.suppress_mic_during_tts !== false;
  const opts = (k, cur) => [new Option("System default", ""), ...d.devices.filter((x) => x[k] > 0).map((x) => new Option(x.name, x.id))].map((o) => { o.selected = String(o.value) === String(cur ?? ""); return o; });
  $("#s-in").replaceChildren(...opts("in", c.audio.input_device)); $("#s-out").replaceChildren(...opts("out", c.tts.output_device));

  // Speech & translation settings
  const sttModels = c.stt_models?.length ? c.stt_models : [c.stt.model];
  $("#s-stt-model").replaceChildren(...sttModels.map((name) => new Option(name, name, false, name === c.stt.model)));
  const langs = Object.entries(c.stt_languages || { auto: "Auto-detect", en: "English", ml: "Malayalam", hi: "Hindi" });
  $("#s-stt-lang").replaceChildren(...langs.map(([code, label]) => new Option(`${label} (${code})`, code, false, code === c.stt.language)));
  $("#s-stt-quality").value = c.stt.quality || "balanced";
  $("#s-stt-hints").value = c.stt.vocab_hints || "";
  if (c.translation) {
    $("#s-tr-mode").value = c.translation.mode || "transcribe_only";
    $("#s-tr-target").value = c.translation.target_language || "none";
    $("#s-tr-autocorrect").checked = Boolean(c.translation.auto_correct);
  }

  // Internet & search provider settings
  if (c.internet) {
    $("#s-web-on").checked = Boolean(c.internet.enabled);
    $("#s-provider").value = c.internet.provider || "duckduckgo";
    $("#s-web-fetch").checked = Boolean(c.internet.allow_page_fetch);
    $("#s-web-tools").checked = c.internet.tool_calling !== false;
    $("#s-searxng-url").value = c.internet.searxng_url || "";
  }
  keyMap = c.api_keys_configured || {};
  $("#s-api-key").value = "";
  updateProviderUI();
}
$("#s-provider").onchange = updateProviderUI;
$("#s-speed").oninput = (e) => ($("#s-speed-o").textContent = e.target.value + "×");
$("#s-thr").oninput = (e) => ($("#s-thr-o").textContent = e.target.value);
$("#s-model").onchange = async (e) => {
  try { await api("/models/switch", { method: "POST", body: { model: e.target.value } }); emit("status"); loadSettings(); } catch (err) { toast(err.message); loadSettings(); }
};
$("#s-key-save").onclick = async () => {
  const p = $("#s-provider").value, k = $("#s-api-key").value.trim();
  if (p === "duckduckgo") return toast("DuckDuckGo does not require an API key.");
  if (!k) return toast("Enter an API key first.");
  try {
    await api("/internet/key", { method: "POST", body: { provider: p, api_key: k } });
    $("#s-api-key").value = "";
    keyMap[p] = true;
    updateProviderUI();
    $("#s-msg").textContent = `Saved ${p} API key securely.`;
    monitor();
  } catch (e) { toast(e.message); }
};
$("#s-key-clear").onclick = async () => {
  const p = $("#s-provider").value;
  if (p === "duckduckgo") return;
  try {
    await api(`/internet/key/${encodeURIComponent(p)}`, { method: "DELETE" });
    keyMap[p] = false;
    updateProviderUI();
    $("#s-msg").textContent = `Cleared ${p} API key.`;
    monitor();
  } catch (e) { toast(e.message); }
};
$("#s-save").onclick = async () => {
  const dev = (v) => (v === "" ? null : Number(v));
  try {
    await api("/config", { method: "PUT", body: {
      num_ctx: +$("#s-ctx").value,
      tts: { enabled: $("#s-tts").checked, voice: $("#s-voice").value, speed: +$("#s-speed").value, output_device: dev($("#s-out").value) },
      wake: { enabled: $("#s-wake").checked, threshold: +$("#s-thr").value },
      audio: {
        voice_mode: $("#s-vmode").value,
        duplex_mode: $("#s-duplex").value,
        echo_cancellation: $("#s-aec").value,
        mic_resume_delay_ms: +$("#s-resume-delay").value,
        silence_ms: +$("#s-sil").value,
        speech_confirm_ms: +$("#s-sconfirm").value,
        wake_followup_s: +$("#s-wfollow").value,
        barge_in_enabled: $("#s-barge").checked,
        barge_in_sensitivity: $("#s-barge-sens").value,
        suppress_mic_during_tts: $("#s-suppress-tts").checked,
        input_device: dev($("#s-in").value),
      },
      stt: { model: $("#s-stt-model").value, language: $("#s-stt-lang").value, quality: $("#s-stt-quality").value, vocab_hints: $("#s-stt-hints").value.trim() },
      translation: { mode: $("#s-tr-mode").value, target_language: $("#s-tr-target").value, auto_correct: $("#s-tr-autocorrect").checked },
      internet: { enabled: $("#s-web-on").checked, provider: $("#s-provider").value, allow_page_fetch: $("#s-web-fetch").checked, tool_calling: $("#s-web-tools").checked, searxng_url: $("#s-searxng-url").value.trim() } } });
    $("#s-msg").textContent = "Saved. Toggle the microphone to apply a new input device."; emit("status"); monitor();
  } catch (e) { $("#s-msg").textContent = e.message; }
};

// ---------- monitor: real numbers from GET /api/status, polled only while this drawer is open ----------
let timer = null;
function meter(name, value, pct, sub) {
  const m = el("div", "meter"), top = el("div", "top"); top.append(el("span", null, name), el("b", null, value)); m.append(top);
  if (pct != null) { const t = el("div", "track"), f = el("div"); f.style.width = Math.max(0, Math.min(100, pct)) + "%"; t.append(f); m.append(t); }
  if (sub) m.append(el("div", "sub", sub));
  return m;
}
async function monitor() {
  try {
    const s = await api("/status"), r = s.system.ram, g = s.system.gpu, used = r.total_gb - r.free_gb, a = s.system.audio;
    const net = s.internet || {};
    const webLine = net.enabled
      ? `${net.provider} · ${net.state}${net.last_error ? " (" + net.last_error + ")" : ""} · cache ${net.cache_entries ?? 0}`
      : "disabled (Local Mode)";
    $("#monitor").replaceChildren(
      meter("BACKEND", "online", null, `${s.notes} notes indexed · activity: ${s.activity}`),
      meter("OLLAMA", s.ollama.up ? "v" + s.ollama.version : "DOWN", null, s.ollama.up ? `loaded: ${(s.ollama.loaded || []).join(", ") || "nothing"}` : s.ollama.error),
      meter("MODEL", s.brain.model, null, `context ${s.brain.num_ctx}${s.brain.thinking ? " · thinking model" : ""}`),
      meter("CPU", s.system.cpu_pct == null ? "measuring…" : s.system.cpu_pct + "%", s.system.cpu_pct),
      meter("RAM", `${used.toFixed(1)} / ${r.total_gb} GB`, (used / r.total_gb) * 100),
      g ? meter("GPU", g.util_pct + "%", g.util_pct, g.name) : meter("GPU", "not available"),
      g ? meter("VRAM", `${g.used_gb} / ${g.total_gb} GB`, (g.used_gb / g.total_gb) * 100) : meter("VRAM", "not available"),
      meter("AUDIO", a.input && a.output ? "ready" : "missing device", null, `in: ${a.input || "none"} · out: ${a.output || "none"}`),
      meter("VOICE LOOP", s.voice.audio_state || s.voice.state, null, s.voice.error || `${s.voice.mode || "continuous"} · ${s.voice.duplex?.effective || "half_duplex"}${s.voice.duplex?.fallback ? " (AEC fallback)" : ""} · mic ${s.voice.mic_suspended ? "suspended" : "active"}`),
      meter("TTS STREAM", s.voice.tts_status?.speaking ? `chunk ${s.voice.tts_status.playing_chunk}/${s.voice.tts_status.chunks_queued}` : "idle", null, `queued ${s.voice.tts_status?.queued ?? 0} · played ${s.voice.tts_status?.chunks_played ?? 0}${s.voice.tts_status?.last_chunk_gap_ms != null ? ` · gap ${s.voice.tts_status.last_chunk_gap_ms}ms` : ""}`),
      meter("NETWORK", net.enabled ? "ONLINE ASSISTED" : "LOCAL MODE", null, net.enabled ? "approved HTTPS search + SSRF guard active" : "all non-local connections blocked"),
      meter("WEB SEARCH", net.enabled ? (net.state || "ready") : "offline", null, webLine));
  } catch (e) { $("#monitor").replaceChildren(meter("BACKEND", "unreachable", null, e.message)); }
}
on("drawer", (d) => {
  clearInterval(timer);
  if (d === "settings") { loadSettings().catch((e) => toast(e.message)); monitor(); timer = setInterval(() => !document.hidden && monitor(), 3000); }
});
