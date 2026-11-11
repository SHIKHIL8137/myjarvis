# 04 — Backend & API Architecture Audit

> **Verified Metrics**: Inspected directly via FastAPI OpenAPI schema extraction (`jarvis.api.create_app().openapi()["paths"]`), revealing **81 distinct API paths** and **90 HTTP operations** across 3 modules (`api.py`, `api_workspace.py`, `api_learning.py`).

---

## 1. Complete API Route Inventory

### 1.1 Core, Chat, System, Models & Notes (`jarvis/api.py` — 32 paths, 36 operations)

| Method & Path | Handler Role | Request Body Schema | Explicit `response_model` | Async / Sync Issues |
| :--- | :--- | :--- | :--- | :--- |
| `GET /api/health` | Liveness + Ollama version/models + mode | — | None | Calls async `brain.health()` (clean) |
| `GET /api/status` | Full system, voice, stt, translation, learning, GPU/RAM/audio telemetry | — | None | Runs `sysinfo.gpu`/`audio` in `to_thread`, but `store.all()`, `memory_store.list_all()`, `knowledge.freshness()`, `feedback.summary()` run synchronously on event loop |
| `POST /api/chat` | Synchronous turn execution | `ChatIn` | None | Correct |
| `POST /api/chat/stream` | SSE token/tool/meta/done streaming turn | `ChatIn` | None | Yields from `Assistant.stream_turn()` |
| `GET /api/history` | Active RAM history window | — | None | Sync file irrelevant; lightweight |
| `DELETE /api/history` | Clear conversation history | — | None | Mutates JSONL on event loop |
| `GET /api/models` | List configured, installed Ollama & gated cloud models | — | None | Clean |
| `POST /api/models/switch` | Switch active LLM / unload previous Ollama model | `SwitchIn` | None | Clean |
| `GET /api/config` | Read sanitized `Settings` + redacted API key presence flags | — | None | Properly redacts secrets (`has_*_key`) |
| `PUT /api/config` | Persist overrides to `data/settings.json` & hot-apply | `Settings` | None | Writes settings synchronously |
| `GET /api/drill/connections` | Audit events (`since`), active `VoiceLoop` streams/threads, process count | — | None | Diagnostic endpoint |
| `GET /api/events` | Global SSE push stream (`EventBus` pub/sub) | — | None | Per-process `asyncio.Queue(64)` |
| `POST /api/voice/hear` | Upload WAV bytes (`max 20 MB`) → Whisper STT + optional translation | Raw bytes | None | Resample/transcribe in `to_thread` |
| `POST /api/voice/correct` | Save user transcript correction preserving `original_transcript` | `CorrectIn` | None | Clean |
| `POST /api/voice/translate` | Translate transcript record or arbitrary text locally | `TranslateIn` | None | Clean |
| `GET /api/voice/transcripts` | Recent in-memory transcript records (`maxlen=50`) | Query `limit` | None | Non-persistent across restarts |
| `POST /api/tts` | Synthesize text to `audio/wav` bytes via Piper | `SpeakIn` | None | Runs in `to_thread` |
| `POST /api/speak` | Enqueue speech on `Speaker` | `SpeakIn` | None | Clean |
| `POST /api/speak/stop` | Immediately stop `Speaker` playback | — | None | Clean |
| `POST /api/voice/mic` | Toggle `VoiceLoop` on/off | `MicIn` | None | Clean |
| `POST /api/voice/session` | Control `continuous`/`ptt`/`wake_word` start/stop/pause/resume | `VoiceSessionIn` | None | Clean |
| `POST /api/voice/interrupt` | Cancel active LLM generation & flush TTS | — | None | Clean |
| `POST /api/voice/ptt` | Push-to-talk start/stop | `MicIn` | None | Clean |
| `GET /api/audio/devices` | Enumerate PortAudio input/output devices + AEC backends | — | None | Clean (`to_thread` recommended for PortAudio query) |
| `GET /api/internet` | Internet mode, search status, redacted key presence, external audit log | — | None | Properly sanitizes keys |
| `POST /api/internet/mode` | Toggle `local` vs `online` mode & persist override | `InternetModeIn` | None | Mutates global `audit` + `settings` |
| `POST /api/internet/key` | Store provider API key (`tavily`, `brave`, `serper`, `openai`, `anthropic`) | `ProviderKeyIn` | None | Stored plaintext in `data/secrets.json` |
| `DELETE /api/internet/key/{provider}` | Delete stored provider API key | Path param | None | Clean |
| `POST /api/internet/search` | Direct web search through `WebSearchService` | `SearchIn` | None | Clean |
| `POST /api/memory` | Legacy note-only creation endpoint | `MemoryIn` | None | **Redundant**: overlaps `/api/memory/items` & only creates `notes/*.md` |
| `GET /api/memory/search` | Search Markdown notes (note keyword search) | Query `q`, `k` | None | Naming mismatch (`/memory/search` searches `NoteStore`, not `PersonalMemoryStore`) |
| `GET /api/notes` | List all scanned Markdown notes | — | None | Uses volatile list-index `id` |
| `GET/PUT/DELETE /api/notes/{note_id}` | Note CRUD + soft delete to `.trash/` | `NoteIn` (PUT) | None | **Fragile ID**: `note_id: int` is list position in alphabetically sorted `rglob`; concurrent file add shifts IDs |
| `GET /api/graph` | Return note nodes + `[[wikilink]]`/title-mention edges | — | None | Computes O(N^2) pairwise overlap on every refresh |

### 1.2 Workspace, Personal Memory, Filesystem & Actions (`jarvis/api_workspace.py` — 18 paths, 21 operations)

- `GET /api/voices`, `POST /api/voices/switch`, `POST /api/voices/rate`
- `GET/POST /api/memory/items`, `PUT/DELETE /api/memory/items/{rec_id}`, `POST /api/memory/items/{rec_id}/confirm`
- `POST /api/memory/backup`, `GET /api/memory/export`, `POST /api/memory/restore`
- `GET /api/history/search`
- `GET /api/files/roots`, `GET /api/files/search`, `GET /api/files/read`, `GET /api/files/inspect`, `POST /api/files/write`
- `GET /api/actions/pending`, `POST /api/actions/execute`, `POST /api/actions/{token}/confirm`, `POST /api/actions/{token}/reject`
- `GET /api/tasks`, `POST /api/tasks/{task_id}/cancel`

### 1.3 Automatic Learning, Datasets, Training & Registry (`jarvis/api_learning.py` — 25 paths, 27 operations... *Note exact OpenAPI count across the 3 routers sums to 81 paths / 90 ops total.*)

- **Knowledge**: `GET /api/learning/overview`, `GET/POST /api/learning/knowledge/sources`, `POST /api/learning/knowledge/ingest`, `POST /api/learning/knowledge/sync`, `POST /api/learning/knowledge/{source_id}/rollback`, `GET /api/learning/knowledge/search`
- **Feedback**: `GET/POST /api/learning/feedback`, `POST /api/learning/feedback/{id}/approve|reject`, `DELETE /api/learning/feedback/{id}`
- **Datasets**: `GET /api/learning/datasets`, `GET /api/learning/datasets/discover`, `POST /api/learning/datasets/import-local|download|prepare-feedback|process`
- **Training**: `GET/POST /api/learning/training/jobs`, `POST /api/learning/training/jobs/{job_id}/approve|start|pause|resume|cancel`
- **Eval & Registry**: `POST /api/learning/evaluate`, `GET /api/learning/evaluations`, `GET /api/learning/registry`, `POST /api/learning/registry/candidate`, `POST /api/learning/registry/{version_id}/promote`, `POST /api/learning/registry/rollback`

---

## 2. Backend Design Findings & Remediation Specs

| Issue | Observed Location | Technical Impact | Target Fix |
| :--- | :--- | :--- | :--- |
| **1. Closure-Captured State instead of FastAPI `Depends`** | `api.py` passes `app.state.a` directly to `create_workspace_router(a)` and `create_learning_router(a)`. | If `app.state.a` is swapped (e.g., testing or runtime reset), sub-routers keep calling the stale `Assistant` instance. Also prevents granular per-service dependency injection (`MemoryRepo`, `ModelGateway`, `TaskRunner`). | Create `backend/api/deps.py` providing `get_settings()`, `get_db()`, `get_gateway()`, `get_memory_service()`, `get_agent_runtime()` via `Request.app.state` + `Depends()`. |
| **2. Missing OpenAPI `response_model` & Inconsistent Error Contracts** | Almost none of the 90 operations declare Pydantic `response_model`. `/api/actions/execute` wraps errors into `HTTP 403`; `api_learning.py` raises `ValueError`/`KeyError`/`FileNotFoundError` expecting generic FastAPI handlers; `intent.py` returns HTTP `200 {"ok": False}`. | Generated frontend TypeScript clients cannot derive strict response types; callers must inspect mixed error shapes (`detail` vs `error` vs `ok: false`). | Introduce typed `schemas/*.py` Pydantic v2 DTOs for every endpoint and a canonical `ErrorEnvelope(code, message, details, request_id)` exception handler mapping domain errors uniformly. |
| **3. Side-Effect inside Read Endpoint** | `api_learning.py` `GET /api/learning/overview` calls `Assistant.learning_overview()`, which invokes `self.training_scheduler.evaluate_triggers(...)` — writing `scheduler_state.json` and appending `training_proposed` to `audit.jsonl` on every 5-second UI drawer poll! | Causes log bloat (`audit.jsonl` & disk writes) whenever Learning drawer stays open (`learning.js` polls every `5000ms`). | Separate read-only `scheduler.get_state()` from periodic/event-driven `scheduler.evaluate_triggers()`. |
| **4. Blocking Sync IO on Async Event Loop** | `api_workspace.py` (`files/search`, `files/read`, `memory/backup`), `api_learning.py` (`datasets/process`, `knowledge/ingest`), and `Assistant._gather_context` execute `rglob`, `sqlite3`, JSONL rewrites directly on main event loop. | Large repository searches or dataset processing freeze concurrent SSE streams and voice status updates. | Migrate stores to `asyncpg`/async repositories and wrap filesystem/subprocess operations in bounded worker pools (`anyio.to_thread.run_sync`). |
| **5. Volatile Integer Primary Keys for Notes** | `NoteStore.refresh()` assigns `id = 0..N-1` from sorted filenames (`jarvis/notes.py:60-66`). | Deleting or adding a note alphabetically renumbers all subsequent `/api/notes/{note_id}` URLs and 3D graph node IDs. | Assign persistent UUID/ULID primary keys in PostgreSQL (and frontmatter/slug mapping for exported `.md` files). |
| **6. Unbounded `tts_cache` & Raw Bytes Upload without Streaming** | `data/tts_cache/` currently holds `207 WAVs` (`61.06 MB`) with no eviction; `/api/voice/hear` awaits `await req.body()` in memory up to 20 MB. | Disk fills silently over months. | Add LRU cache cap (`max_cache_mb`) in `tts` service and multipart/streaming upload checks. |
| **7. Version & Compatibility Strategy** | Existing UI (`viewer/`) & 13 test modules call `/api/*` directly. | Breaking existing `/api/*` paths immediately breaks regression tests and legacy UI fallback. | Mount new modular domain routers at `/api/v2/*` (or refactored clean `/api/*` where schemas are backward-compatible), keeping `/api/*` compatibility shims until `P8`/`P9` cutover. |
