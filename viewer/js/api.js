// Centralised backend access + tiny DOM helpers + a shared event hub. Everything talks to /api on this origin only.
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const HEAD = { "Content-Type": "application/json", "X-Jarvis": "1" };

export async function api(path, { method = "GET", body, signal } = {}) {
  const r = await fetch("/api" + path, { method, headers: HEAD, body: body === undefined ? undefined : JSON.stringify(body), signal });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail?.[0]?.msg || r.statusText);
  return j;
}

/** POST /chat/stream and call onEvent for each server-sent event. Abort the signal to stop generation. */
export async function chatStream(message, speak, onEvent, signal) {
  const r = await fetch("/api/chat/stream", { method: "POST", headers: HEAD, body: JSON.stringify({ message, speak }), signal });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  const rd = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await rd.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 2);
      if (line.startsWith("data: ")) await onEvent(JSON.parse(line.slice(6)));
    }
  }
}

/** Server-sent events from the backend (voice state, voice turns, graph changes). Reconnects by itself. */
export function listen(onEvent, onConn) {
  const es = new EventSource("/api/events");
  es.onopen = () => onConn(true);
  es.onerror = () => onConn(false);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
}

// app-wide pub/sub so modules stay decoupled
const subs = {};
export const on = (name, fn) => (subs[name] ||= []).push(fn);
export const emit = (name, data) => (subs[name] || []).forEach((fn) => fn(data));

const PALETTE = { programming: "#4d8dff", technology: "#27d3ff", personal: "#ffb74d", learning: "#b388ff", captures: "#ff6b9a", general: "#9aa7b5" };
export const colorOf = (g) => PALETTE[g] || `hsl(${[...g].reduce((a, c) => a * 31 + c.charCodeAt(0), 7) % 360} 70% 62%)`;

export function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 5000);
}
export const store = {
  get: (k, d) => { try { const v = localStorage.getItem("jarvis." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("jarvis." + k, JSON.stringify(v)); } catch {} },
};
