/** One shared SSE connection to /api/events for the whole app with sequence tracking & reconnect replay. */
export type LiveEvent = Record<string, any>; // backend event payloads are open-ended dicts
export type ConnState = "connecting" | "online" | "offline";

const subs = new Set<(ev: LiveEvent) => void>();
const stateSubs = new Set<() => void>();
let es: EventSource | null = null;
let retry = 0;
let state: ConnState = "connecting";
let lastSeq = 0;

/** Recent system events for the inspector feed. Bounded; streaming tokens and mic levels are not kept. */
export const recentEvents: Array<{ at: number; ev: LiveEvent }> = [];
const NOISY = new Set(["token", "level", "tts_status"]);

function setState(s: ConnState) {
  if (s === state) return;
  state = s;
  stateSubs.forEach((f) => f());
}

function open() {
  const url = lastSeq > 0 ? `/api/events?since_seq=${lastSeq}` : "/api/events";
  es = new EventSource(url);
  es.onopen = () => setState("online");
  es.onerror = () => {
    // CONNECTING: the browser is already retrying. CLOSED (e.g. HTTP error): it gave up, so retry ourselves.
    if (es?.readyState === EventSource.CLOSED) {
      setState("offline");
      es = null;
      retry = window.setTimeout(() => subs.size && open(), 5000);
    } else setState("connecting");
  };
  es.onmessage = (e) => {
    let ev: LiveEvent;
    try {
      ev = JSON.parse(e.data);
    } catch {
      return;
    }
    if (typeof ev.seq === "number" && ev.seq > lastSeq) lastSeq = ev.seq;
    if (!NOISY.has(ev.type)) {
      recentEvents.push({ at: Date.now(), ev });
      if (recentEvents.length > 120) recentEvents.shift();
    }
    subs.forEach((f) => f(ev));
  };
}

/** Subscribe; returns the unsubscribe function. The connection closes when the last listener leaves. */
export function onEvent(fn: (ev: LiveEvent) => void): () => void {
  subs.add(fn);
  if (!es) open();
  return () => {
    subs.delete(fn);
    if (!subs.size) {
      clearTimeout(retry);
      es?.close();
      es = null;
    }
  };
}

export const connState = (): ConnState => state;
export const currentSeq = (): number => lastSeq;
export function onConnState(fn: () => void): () => void {
  stateSubs.add(fn);
  return () => stateSubs.delete(fn);
}
