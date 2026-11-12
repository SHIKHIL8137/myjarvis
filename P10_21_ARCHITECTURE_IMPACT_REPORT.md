# P10.21 — Architectural Impact Report

**Audit Date:** 2026-11-12  
**Target Architecture:** Jarvis (`FastAPI` + `SQLAlchemy`/SQLite/PostgreSQL + `LangGraph` + `ModelGateway` + `VoiceLoop`/`Whisper`/`Piper` + `React/TS/Vite/WebGL` frontend)

This report analyzes how each recommended clean-room addition fits into Jarvis's **existing** module boundaries without rewriting working subsystems or violating Local Mode / permission policies.

---

## 1. Persistent Checkpointed Background Task Engine & Boot Recovery (`P0`)

- **Current Integration Point:** `jarvis/agents/tasks.py` (`AssistantTaskService`), `jarvis/agents/checkpointer.py` (`DBBackedCheckpointSaver`), `jarvis/agents/orchestrator.py`, `jarvis/assistant.py` (`start()`).
- **Database Schema / Migration Impact:**
  - Extend `AssistantTaskRow` (via new Alembic revision `0004`) with additive columns: `attempts` (int, default `0`), `max_attempts` (int, default `3`), `not_before` (float nullable epoch), `deadline_at` (float nullable epoch), `step_budget` (int nullable), `steps_used` (int, default `0`), `steer_notes_json` (JSON list of user steering notes), `quota_retry_at` (float nullable), and `worker_kind` (`"research" | "workflow" | "workforce" | "routine"`).
  - Add a child `TaskArtifactRow` (or `artifacts_json` on `AssistantTaskRow` + disk folder `data/tasks/<task_id>/`) to persist intermediate subagent/research state independently of RAM.
- **Runtime / Execution Impact:**
  - Add a clean-room `BackgroundTaskRunner` started alongside `_watch_reminders` inside `Assistant.start()` and halted cleanly in `Assistant.stop()` (`shutdown` drains active coroutine cooperatively, leaving `"running"` tasks cleanly recoverable rather than marking `"failed"`).
  - On startup, query `AssistantTaskRow` where `status in ("queued", "running", "waiting_quota", "planning")` **but exclude `awaiting_permission` and `awaiting_approval`** (those MUST NOT auto-execute without fresh UI/owner confirmation!). Re-queue interrupted `"running"` tasks if `attempts < max_attempts`, else transition to `"failed"` cleanly.
  - Reuse `ModelGateway` and enforce per-step token/time budget wrap-up (force a toolless summary generation pass when `remaining_time <= wrap_reserve` or `steps_used >= step_budget`).
- **API & UI Impact:**
  - Extend `/api/v2/tasks` (`api_v2.py`) with `POST /api/v2/tasks/{task_id}/steer` and `not_before` / `attempts` fields on GET responses; reuse existing SSE `task_updated` events and `Tasks` contextual drawer (no new top-level navigation).
- **Resource & Latency Impact:** Negligible idle CPU (event-driven + 2–5s wake/timer tick). Crucially protects local Ollama GPU (`GTX 1650` / `ResourceGovernor` `max_local_concurrency=1`) by ensuring background tasks yield or serialize behind interactive user turns!

---

## 2. Multi-Stage Verified Deep Research Engine (`P0`)

- **Current Integration Point:** `jarvis/agents/assistant_graph.py` (`task_kind="research"` currently routes to memory-only `summarize`), `jarvis/search.py` (`WebSearchService.search`, `fetch_page`), `jarvis/departments/assistant/documents.py` (`DocumentService`), `jarvis/netsec.py`, `jarvis/audit.py`.
- **Execution Flow (Online Assisted Mode Only; Local Mode stays KB-only):**
  1. `PLAN`: Decompose question into bounded sub-queries (`<= 6`).
  2. `INVESTIGATE`: Execute `WebSearchService.search` + `fetch_page` (raising page storage limit from `2,500` chars preview to full sanitized text archived in `data/tasks/<task_id>/pages/<sha1>.txt`).
  3. `REVIEW`: Identify thin claims, missing citations, or source contradictions; run bounded follow-up pass if within budget.
  4. `WRITE` (with reserved time/token budget): Draft Markdown report citing sources `[1]..[n]`.
  5. `VERIFY / REVISE` (deterministic Python check): Normalize draft quotes + text (`NFKC` + whitespace collapse) and verify in Python whether quoted claims appear (`exact` or sliding-window `partial`) inside cached `pages/<sha1>.txt`; strip/renumber citations `[n]` and mark unsupported claims `[unverified]`/`[?]`. Optionally export PDF/DOCX via `DocumentService`.
- **Resource Impact:** Strictly runs asynchronously as a `BackgroundTaskRunner` job when deep research is requested, preventing blocking of voice/chat `stream_turn()` lock (`_turn_lock`).

---

## 3. Untrusted Content Boundaries, Memory-Web Split & Verified Outcomes (`P0`)

- **Current Integration Point:** `jarvis/persona.py` (`user_message`), `jarvis/assistant.py` (`_execute_tool`, `stream_turn`), `jarvis/skills/security.py`, `jarvis/actions.py`.
- **Architectural Changes (Zero Schema Changes):**
  - Introduce a central helper (`jarvis/security/untrusted.py`) wrapping every web search snippet, fetched webpage body, parsed untrusted document, and unverified external text in `<<<UNTRUSTED_DATA source="...">>> ... <<<END_UNTRUSTED_DATA>>>` after escaping any literal closing delimiter inside the payload.
  - Apply deterministic tool policy enforcement inside `assistant.py`: when a workflow/research runner executes untrusted web retrieval (`web_search`/`fetch_webpage`), **do not** include raw private personal memories (`MemoryStore` / `MemoryV2`) in the same autonomous tool loop unless explicit user turn context only.
  - Introduce a compact `ToolOutcome` dataclass (`status: Literal["done", "unverified", "failed", "started", "awaiting_approval"]`, `summary`, `verified_evidence`, `error_code`) consumed by `assistant.py` / `orchestrator.py` and surfaced clearly in system/developer tool prompts.

---

## 4. DB-Backed Reversible Action Undo Journal (`P1`)

- **Current Integration Point:** `jarvis/filesystem_access.py`, `jarvis/actions.py`, `jarvis/assistant.py`, `jarvis/db/models.py`, `jarvis/api_v2.py`.
- **Why Jarvis Must Improve Over Mark's RAM-Only Stack:**
  - Mark's `core/undo.py` loses everything on restart (`clear()`) and runs raw closures without checking whether a file changed externally since Jarvis edited it.
- **Clean-Room Design:**
  - Add `UndoJournalRow` (`undo_id`, `action_type`, `label`, `target_path_or_key`, `before_sha256`, `after_sha256`, `rollback_payload_json` / snapshot path in `data/undo/`, `created_at`, `undone_at`, `expires_at`).
  - Support safe reversals for: `create_file` -> unlink if `after_sha256` still matches; `write_file`/`edit_file` -> restore previous snapshot (`<= 1 MB`) only if current file hash == `after_sha256` (fail safely with conflict message if modified externally!); `move_file` -> move back; settings change -> restore previous setting key; `trash` (`send2trash` optional instead of hard `unlink`).
  - Expose voice command (`"undo last action"` / `"what can I undo?"`), `POST /api/v2/undo/last`, `GET /api/v2/undo` inside the existing single-screen Assistant contextual drawer.

---

## 5. Runtime Self-Knowledge Prompt Block (`P1`)

- **Current Integration Point:** `jarvis/persona.py`, `jarvis/assistant.py` (`stream_turn`), `jarvis/voice.py`, `jarvis/api_v2.py` (`/system/overview`).
- **Architectural Impact:**
  - **KV-Cache Preservation Note:** Prepending volatile per-second telemetry (timestamps/RAM MB) to the system prompt would bust local Ollama `qwen3:4b-instruct` KV caches on every turn! Therefore, keep `PERSONA` fixed as prefix line 1 and append a **coarse, rarely-changing** capability block (OS, `mode=local|online`, `active_model`, `vision_available=true|false`, `mic_active`, `wake_ready`, `docker_available`, `db_backend=sqlite|postgresql`, active granted folder paths, active agent roles) — or place volatile deltas in a small suffix system/user metadata note so the base prompt stays KV-cache friendly.

---

## 6. Unified Routines & Quiet Hours Engine (`P1`)

- **Current Integration Point:** `jarvis/departments/assistant/reminders.py`, `jarvis/assistant.py` (`_watch_reminders`, `_watch_notes`, `_watch_knowledge`, `_watch_memory_expiry`).
- **Database Schema / Migration Impact:**
  - Extend or add `RoutineRow` (`routine_id`, `title`, `kind` in `("say", "prompt", "task", "builtin")`, `trigger_type` in `("once", "interval", "weekly", "monthly", "startup", "idle")`, `schedule_json`, `payload_json`, `quiet_eligible` bool, `enabled` bool, `last_run_at`, `next_run_at`, `last_status`) + `Settings.preferences.quiet_hours` (`"23:00-07:00"`).
- **Runtime Impact:** Consolidates `_watch_reminders` + scheduled maintenance into one scheduler honoring catch-up grace windows (`say` 2h, `prompt` 45m, `task` 6h) and `quiet_hours` (deferring/skipping `quiet_eligible=True` background check-ins while still delivering explicit user-set alarms). Reuses `Assistant._set_os_state` and SSE `/api/events` (and `Speaker` only if voice active & outside quiet hours).

---

## 7. Read-Only Gated Vision (`qwen2.5vl:3b`) & Audio Device Probing (`P2`)

- **Vision (`P2`):** Host already has `qwen2.5vl:3b` in Ollama and `vision=True` metadata on `GeminiAdapter`/`AnthropicAdapter`, **but** `Brain.stream()` and `ModelGateway` need multipart image message normalization + a gated capture/upload endpoint (`POST /api/vision/analyze` + optional desktop screenshot helper under explicit owner opt-in, honoring `ResourceGovernor` VRAM eviction when swapping between `qwen3:4b-instruct` and `qwen2.5vl:3b` on the 4 GB GTX 1650).
- **Audio Probe & Voice Loop Verification (`P1/P2`):** Add optional `test_audio_device_transport()` diagnostic in `jarvis/sysinfo.py` / `api_v2.py` (`/settings/devices`) so silent DirectSound sinks are flagged automatically without changing `VoiceLoop` core state machine. Add configurable `wake_active_timeout_s` (e.g., `120s`) before returning `LISTENING` -> `WAKE_WORD`.

---

## 8. Features Excluded From Core Architecture Impact (`Not suitable`)

- **PyQt6 HUD (`main.py`, `ui.py`)**: Incompatible with Jarvis's consolidated React/TypeScript/WebGL web OS.
- **Un-audited Dynamic Plugins & Runtime `pip install` (`plugin_loader.py`, `installer.py`)**: Superseded by `jarvis/skills/*` DB + AST security scanner.
- **Cloud Second Body (`core/cloud.py`) & Auto-UAC Firewall Remote Server (`dashboard/server.py`)**: Excluded from P0–P2 because they conflict with `sys.addaudithook` local-first security and localhost binding (`127.0.0.1:4700`).
- **Unscoped `Path.home()` Filesystem Access & Arbitrary Desktop GUI Writes**: Excluded; must never bypass `FilesystemAccessManager`.

