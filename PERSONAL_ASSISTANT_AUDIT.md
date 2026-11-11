# Personal Assistant Audit (`jarvis/` + `viewer/`)

Audited against live repository state on `2026-11-11` (`Ryzen 5 5600H`, `GTX 1650 4 GB`, `15.3 GB RAM`, `Windows 11`, `Python 3.14.6`, `Ollama 0.40.16`).

---

## 1. Existing Functionality

| Area | Modules | Current Capabilities |
|---|---|---|
| Entry & API | `server.py`, `jarvis/api.py` | FastAPI loopback-only server (`127.0.0.1:4700`), Host/Origin checks, `X-Jarvis: 1` CSRF header, CSP headers, SSE event stream (`/api/events`), chat/history/models/notes/graph/voice/internet/config endpoints. |
| LLM & Prompting | `jarvis/brain.py`, `jarvis/persona.py` | Native Ollama `/api/chat` client (`127.0.0.1:11434`), `<think>` removal + reasoning drop, KV-cache-friendly static `PERSONA` / `ONLINE_PERSONA`, live model listing/switching/unloading, tool-calling parser (`tool_calls`). |
| Intent & Commands | `jarvis/commands.py`, `jarvis/assistant.py` | Regex router run before LLM (`REMEMBER`, `STOP`, `SWITCH`, `WHICH`, `COUNT`, `SHOW`, `FIND`, `WEB_EXPLICIT`) + `stream_turn` orchestration. |
| Memory & Knowledge | `jarvis/notes.py`, `jarvis/retrieval.py`, `jarvis/history.py` | Folder-based Markdown `NoteStore` (`notes/`, soft-delete to `notes/.trash/`, `[[wikilinks]]` graph), TF-IDF-style keyword search (`retrieval.search`), grow-then-halve `data/history.jsonl` conversation window (`MAX_CHARS = 2000`). |
| Voice & Speech | `jarvis/voice.py`, `jarvis/stt.py`, `jarvis/tts.py`, `jarvis/translator.py`, `jarvis/wakeword.py`, `jarvis/aec.py` | Continuous/Wake-Word/PTT state machine (`VoiceState`), Safe Half-Duplex mic suspension + optional AEC-gated full-duplex (`NLMSEchoCanceller`), `faster-whisper` multilingual CPU STT + polyphase resampling, separated Whisper/Qwen translation + `TranscriptStore`, `StreamingSpeechBuffer` → Piper ONNX synthesis → Bluetooth-resilient `Speaker`. |
| Security & Web | `jarvis/audit.py`, `jarvis/netsec.py`, `jarvis/search.py` | Process-wide `sys.addaudithook` outbound blocker, scoped `allow_outbound` contexts, SSRF/IP-pinned HTTPS client, manual redirect re-validation, `DuckDuckGo`/`Tavily`/`Brave`/`Serper`/`SearXNG` search providers + page fetch (`robots.txt`), external audit JSONL log, `data/secrets.json` API key isolation. |
| Frontend & Telemetry | `viewer/*`, `jarvis/sysinfo.py` | 3D knowledge galaxy, HUD reactor + mode buttons (`HOLO`, `FOCUS`, `REFLEX`, `INTERNET`), command bar (`c-vmode`, `c-pause`, `c-mic`, `c-ptt`, `c-voice`, `c-halt`), transcript correction card, System settings/monitor drawer, Win32 CPU/RAM/GPU metrics. |

---

## 2. Missing Functionality (Relative to Target Requirements)

1. **Centralized Intent Router & Orchestrator (Phase 1)**:
   - No structured intent classification separating deterministic commands, model-assisted tool plans, clarification questions, verification steps, and execution receipts.
   - Missing deterministic/voice routes for `pause listening`, `resume listening`, `switch voice`, `check system status`, filesystem/file/app operations, memory queries (`"What did we discuss about Redis last week?"`), and all learning/training commands.
2. **Complete Multi-Category Personal Memory (Phase 2)**:
   - No separation of explicit personal memories, user preferences, project memories, temporary task context, and approved feedback examples.
   - No searchable historical conversation retrieval beyond the live `items[-200:]` window in `History`.
   - No memory update/delete-by-query, duplicate detection, JSON/Markdown export, snapshot backup/restore, confirmation for inferred memories, or credential/secret scrubbing on save.
3. **Local Filesystem Search & Document Reader (Phase 10)**:
   - Zero local filesystem search (`filename`, `folder`, `extension`, `content` grep), project inspection, document reader (`.md`, `.txt`, `.py`, `.json`, `.csv`, etc.), or safe write/move/delete inside allowlisted roots.
4. **Permission-Gated Computer & App Actions (Phase 10 & Phase 14)**:
   - No permission model (`READ`, `OPEN`, `MODIFY`, `DESTRUCTIVE`, `SYSTEM`), no confirmation flow for risky actions, no allowlisted application launcher or approved command execution, and no tracked multi-step `TaskRecord` lifecycle (steps, status, cancellation, summary, verification).
5. **Complete Model & Voice Switching (Phase 11)**:
   - Brain manager only handles Ollama models — no optional explicit cloud adapters (`OpenAI`, `Anthropic` gated off in Local Mode), capability metadata, or health endpoints.
   - Voice manager has no runtime voice switching voice command (`"Switch voice to ..."`), voice metadata/health check, or speaking rate voice control.
6. **Online vs Local Knowledge Attribution (Phase 13)**:
   - Replies do not separate `Personal Knowledge`, `Indexed Documentation`, `Online Research`, and `Model Reasoning` metadata cleanly.

---

## 3. Broken or Incomplete Implementations Found

1. **`VoiceLoop.push_to_talk()` mutates configured voice mode (`jarvis/voice.py:342-352`)**:
   - Calling `push_to_talk()` while stopped invokes `self.enable(mode="ptt") -> self.set_mode("ptt")`, permanently overwriting `self.s.audio.voice_mode = "ptt"` and `self.s.wake.enabled = False` even when the user wanted a one-shot PTT capture while staying in `continuous` or `wake_word` mode. (Verified live: after `preflight.py` toggled mic/PTT, `/api/status` showed `"mode": "ptt"` instead of `"continuous"`.)
2. **`History` drops searchable history beyond 200 lines (`jarvis/history.py:16`)**:
   - Reads only `splitlines()[-200:]` into RAM and provides no keyword search method across `data/history.jsonl`.
3. **`commands.SWITCH` collides with voice switching (`jarvis/commands.py:10,61-75`)**:
   - `"switch to ..."` assumes an Ollama LLM model name, giving no route for Piper voices (`"switch voice to alan"`).
4. **Legacy `_voice_turn` fallback (`jarvis/assistant.py:304-325`)**:
   - Parallel standalone code path duplicates `_run_voice_utterance` without cancellation or half-duplex lifecycle sync.
5. **UI/Backend Drawer Parity**:
   - `viewer/index.html` lacks drawers/tabs for Tasks & Permissions, Filesystem Explorer/Search, Memory Categories, and Learning/Training Dashboard (`Phase 8`).

---

## 4. Duplicate Code

1. **`jarvis/assistant.py`**: Streaming token + `StreamingSpeechBuffer` feed/flush loop is duplicated verbatim across Local Mode (`lines 428-446`), Online Tool-Result Mode (`lines 483-501`), and Online Fallback Mode (`lines 515-533`). Extract a single helper `_stream_model_and_speech(...)`.
2. **`jarvis/tts.py`**: `clean_for_speech()` (`lines 21-35`) and `StreamingSpeechBuffer._clean_piece()` (`lines 118-127`) duplicate Markdown/URL/emoji stripping regexes. Share one compiled normalizer helper.
3. **`jarvis/sysinfo.py` & `preflight.py`**: Win32 `GlobalMemoryStatusEx` (`class MS(ctypes.Structure)`) is duplicated identically in both files (`sysinfo.py:40-48`, `preflight.py:38-46`).

---

## 5. Security Issues & Guardrails Required

1. **Path Traversal & Sensitive File Exposure (New Risk in Phase 10)**:
   - Adding filesystem search/read/open requires strict canonical root validation (`Path.resolve().is_relative_to(allowed_root)`), mandatory blocklists (`.env`, `.git`, `.venv`, `node_modules`, `data/secrets.json`, Windows system dirs, browser profiles, SSH keys), and size caps.
2. **Command / Shell Injection & Prompt Injection (New Risk in Phases 1 & 10)**:
   - The 4B LLM must **never** pass arbitrary shell strings to `subprocess(..., shell=True)`. All actions must map to allowlisted Python action schemas, validate arguments with Pydantic, enforce `PermissionLevel` (`READ < OPEN < MODIFY < DESTRUCTIVE < SYSTEM`), and require explicit user confirmation tokens for `MODIFY`/`DESTRUCTIVE`/`SYSTEM`.
3. **Credential Leakage into Memory / History (New Risk in Phase 2)**:
   - Regex + entropy secret scrubber must refuse saving patterns matching API keys (`sk-`, `hf_`, `ghp_`, `AIza`, `tvly-`, `BSA...`), passwords, bearer tokens, or PEM blocks into personal memory or feedback stores.
4. **Cloud Model Bypass Risk (Phase 11)**:
   - Optional OpenAI/Anthropic cloud providers must check `not settings.offline_enforced` / `settings.internet.enabled` and `audit.allow_outbound(...)` so Local Mode remains airtight (`drill.py` must continue passing with 0 remote connections).
5. **Existing Protections to Preserve**: Keep Host/Origin checks, `X-Jarvis: 1`, CSP (`connect-src 'self'`), `audit.sys.addaudithook`, and `netsec.py` SSRF pinning untouched cleanly.

---

## 6. Performance Problems & Hardware Constraints

- **Hardware envelope (`Ryzen 5 5600H`, `GTX 1650 4 GB`, `15.3 GB RAM`)**:
  - Live telemetry during testing showed `2.5–4.5 GB` free RAM and `3.0 / 4.0 GB` VRAM when `qwen3:4b-instruct`, Whisper `base`, and Piper are loaded concurrently.
  - Adding an external neural embedding model at runtime would risk swapping or starving Ollama VRAM/RAM; therefore memory, conversation search, and knowledge chunks should default to fast CPU lexical/BM25/TF-IDF + incremental JSONL/SQLite indexing, using Ollama `/api/embeddings` (`nomic-embed-text`, if explicitly installed/configured) only opportunistically.
- **Knowledge Graph Scaling (`jarvis/notes.py:80-100`)**:
  - `NoteStore.graph()` performs $O(N^2)$ pair + substring checks on every graph rebuild. Keep curated notes in `notes/` (`~30–500` items) for the 3D galaxy, and store large ingested documentation/datasets (`Phase 3`) in an incremental SQLite/JSONL chunk store under `data/learning/knowledge/` so the 3D WebGL graph stays responsive at 60 FPS.

---

## 7. Files Requiring Changes

- `jarvis/config.py`, `config.json`: Add structured configs for `filesystem`, `actions`, `memory`, `providers` (optional OpenAI/Anthropic), `learning` (knowledge, feedback, training, evaluation, registry policies).
- `jarvis/commands.py` & `jarvis/assistant.py`: Replace flat regex routing with centralized `IntentRouter`, task runner/cancellable `TaskRecord`, deduplicated `_stream_model_and_speech`, knowledge + feedback context injection, and source provenance attribution.
- `jarvis/history.py` & `jarvis/notes.py`: Add full-file conversation search across `history.jsonl`, structured memory categorization (`explicit`, `preference`, `project`, `temporary`), duplicate check, secret filtering, backup/export/restore, and fast incremental lookup.
- `jarvis/brain.py`: Add provider abstraction (`OllamaProvider`, optional gated `OpenAICompatibleProvider` / `AnthropicProvider`), aliases, capability/health probes, runtime Ollama model/embedding discovery.
- `jarvis/tts.py`: Deduplicate cleaner, add voice discovery metadata, runtime voice switching + health check + speaking rate setter.
- `jarvis/voice.py`: Fix one-shot `push_to_talk()` preserving configured `voice_mode`, integrate voice commands cleanly with pause/resume/stop intents.
- `jarvis/persona.py`: Structured prompt sections separating Personal Memory, Conversation History, Feedback Examples, Indexed Docs, and Online Research.
- `jarvis/api.py`: Endpoints for `/api/intents`, `/api/tasks*`, `/api/memory/*` (categories, export, backup, restore, history search), `/api/files/*` (search, read, inspect, write/move/delete/open), `/api/actions/*` (confirm/reject/history), `/api/voices*`, `/api/providers*`, `/api/learning/*`.
- `viewer/index.html`, `viewer/style.css`, `viewer/js/*`: Extend drawers/modals cleanly (preserving 3D galaxy) for Tasks & Approvals banner, Filesystem/Memory search, and Learning & Brain Dashboard.
- `preflight.py`, `drill.py`: Extend live probes cleanly cleanly per Phase 18.

---

## 8. New Modules Genuinely Required

- `jarvis/intent.py`: Centralized intent classifier + argument extractor + clarification builder.
- `jarvis/memory_store.py`: Multi-category structured memory & searchable history/backup/secret-filter manager wrapping `history.py` & `notes.py` (without duplicating Markdown notes).
- `jarvis/filesystem.py`: Allowlisted local filesystem search, project analyzer, safe document reader/writer, excluded-dir filter.
- `jarvis/actions.py`: Permission-tiered (`READ`, `OPEN`, `MODIFY`, `DESTRUCTIVE`, `SYSTEM`) action executor, confirmation queue, app launcher, and `TaskManager`.
- `jarvis/learning/`: Learning & Brain Improvement package (detailed in `AUTOMATIC_LEARNING_AUDIT.md`).

---

## 9. Tests Needed

- `tests/test_intent_and_tasks.py`: Deterministic vs LLM routing, clarification prompts, permission denial/confirmation, multi-step `TaskRecord` lifecycle + cancellation.
- `tests/test_memory_complete.py`: All 6 categories, history search across >200 records, save/update/delete/duplicate detection, restart persistence, export/backup/restore, credential/secret rejection.
- `tests/test_filesystem_and_actions.py`: Filename/folder/extension/content search, project inspection, path traversal rejection, excluded dirs (`.git`, `.venv`, `node_modules`), safe file edits/deletes with confirmation, allowlisted app launch, permission tier enforcement.
- `tests/test_models_and_voices.py`: Voice discovery/switching/rate/health, independent model vs voice switching, optional cloud provider Local Mode hard block + mocked online call.
- `tests/test_learning_*.py`: Knowledge ingestion/versioning/rollback, feedback learning & privacy scrub, dataset validation/license/poisoning checks, hardware estimator, evaluation suite & promotion/rollback gates, dashboard/preflight/drill extensions.

---

## 10. Recommended Implementation Sequence

1. Fix `VoiceLoop.push_to_talk()` preservation + Settings schema extensions (`jarvis/config.py`, `config.json`).
2. Centralized Intent Router & Task Manager (`jarvis/intent.py`, refactor `commands.py` & `assistant.py`).
3. Multi-Category Personal Memory & History Search (`jarvis/memory_store.py`, `jarvis/history.py`).
4. Safe Filesystem Service & Document Reader (`jarvis/filesystem.py`).
5. Permission Tiers, App/Computer Actions & Confirmation Flow (`jarvis/actions.py`).
6. Model/Provider & Piper Voice Management (`jarvis/brain.py`, `jarvis/tts.py`, independent voice commands).
7. Automatic Knowledge Update Pipeline (`jarvis/learning/knowledge_pipeline.py`).
8. Feedback Learning Engine (`jarvis/learning/feedback.py`).
9. Training Subsystem: Dataset validation/cleaning -> Hardware-aware Worker -> Evaluation -> Registry & Rollback (`jarvis/learning/training/`, `evaluation.py`).
10. API Endpoints + Learning & Operations Dashboard UI (`jarvis/api.py`, `viewer/`).
11. End-to-End Tests, `preflight.py`, `drill.py`, Documentation & Final Verification.

