# JARVIS P10.18 — Real-Time Event Contract & Streaming Report

## 1. Transport Architecture
- **Primary Stream**: Loopback-guarded Server-Sent Events (`GET /api/events`) in `jarvis/api.py` over same-origin CSP (`connect-src 'self'`).
- **Session & Origin Security**: Enforced by middleware in `jarvis/api.py` (loopback `Host` header validation, same-origin `Origin` check, `X-Jarvis: 1` required on all mutating endpoints).
- **Replay Endpoint**: `GET /api/v2/graph/events?since_seq=<int>` exposes buffered missed events for programmatic inspection and deterministic test verification.

## 2. Versioned Event Envelope (`schema_version = 1`)
`EventBus.publish(ev)` (`jarvis/core/event_bus.py`) atomically assigns:
- `schema_version`: `1`
- `seq`: strictly increasing integer (`1, 2, 3, ...`)
- `event_id`: `evt-<seq>`
- `occurred_at`: UTC ISO-8601 timestamp (`datetime.now(timezone.utc).isoformat()`)
- `event_type`: canonical event name (`memory.created`, `memory.updated`, `memory.deleted`, `conversation.updated`, `conversation.deleted`, `agent.started`, `agent.progress`, `agent.completed`, `agent.failed`, `skill.executed`, `project.updated`, `assistant.state.changed`, `assistant.transcript.final`, `decision.routed`, `voice_state`, etc.)

Legacy fields (`type`, `event`, `ts`) are preserved on every packet so existing SSE listeners and tests remain backward-compatible.

## 3. Reconnect, Deduplication & Gap Recovery
1. SSE frames include `id: <seq>` so standard `EventSource` tracks the last applied sequence automatically; `apps/frontend/src/live.ts` also appends `?since_seq=<lastSeq>` when reconnecting.
2. On connection, `/api/events` reads `since_seq` / `Last-Event-ID`, queries `bus.events_since(since_seq)`, and emits a `hello` packet containing `{schema_version: 1, seq, resync_required, assistant_state, ...}`.
3. If `gap == False`, missed buffered events (`seq > since_seq`) stream immediately over SSE. If buffered history (`maxlen=600`) overflowed (`resync_required == True`), `graphStore.ts` automatically fetches a full snapshot via `GET /api/v2/graph` and runs `applySnapshot()` to converge state without duplicates or lost deletions.
4. Client-side `acceptSeq()` discards any duplicate or out-of-order `seq <= lastSeq` deltas.

## 4. Measured Live SSE End-to-End Latency (`127.0.0.1:4700`)
Measured against the live server (`preflight` session) via real HTTP POST + streaming `/api/events` listener:
- Initial `hello` frame: `seq=26`, `schema_version=1`.
- `POST /api/v2/memory/personal` -> `memory.created` received on `/api/events`: **21.39 ms** (`seq=27`, `entity_id=memory:f3fefc61a70949888eaa535fca22143f`).
- `DELETE /api/v2/memory/personal/{id}?hard=true` -> `memory.deleted` received on `/api/events`: **16.25 ms** (`seq=28`).

