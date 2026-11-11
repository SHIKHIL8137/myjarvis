/* Typed Jarvis REST + SSE Client enforcing X-Jarvis CSRF protection header cleanly. */

export async function apiFetch<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown; rawBody?: BodyInit } = {}
): Promise<T> {
  const method = (opts.method || "GET").toUpperCase();
  const headers: Record<string, string> = {};
  if (method !== "GET" && method !== "HEAD") {
    headers["X-Jarvis"] = "1";
  }
  let body: BodyInit | undefined = opts.rawBody;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const url = path.startsWith("/api") ? path : `/api${path}`;
  const res = await fetch(url, { method, headers, body });
  const text = await res.text();
  let data: Record<string, unknown> = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg =
      (typeof data.error === "string" && data.error) ||
      (typeof data.detail === "string" && data.detail) ||
      `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data as T;
}

export async function streamChat(
  message: string,
  speak: boolean,
  onEvent: (ev: Record<string, unknown>) => void
): Promise<void> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jarvis": "1" },
    body: JSON.stringify({ message, speak }),
  });
  if (!res.ok || !res.body) {
    throw new Error(`Stream error HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const ln of block.split("\n")) {
        if (ln.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(ln.slice(6)));
          } catch {
            // ignore keepalives
          }
        }
      }
    }
  }
}

