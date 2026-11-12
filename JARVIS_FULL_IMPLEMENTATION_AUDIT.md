# JARVIS Full Implementation Audit & Stage A–G Blueprint

**Scope**: Phase 0 codebase & external repository audit (`d:\projects\myjarvis`) prior to implementing Stages A–G.
**Target Runtime Hardware**: Windows, AMD Ryzen 5 5600H, NVIDIA GTX 1650 (4 GB VRAM), 16 GB System RAM, Local Ollama (`qwen3:4b`), Faster-Whisper + Piper TTS + Silero VAD.

---

## 1. Current Architecture Overview

Jarvis (`d:\projects\myjarvis`) is a local-first AI assistant built with:
- **Backend (`jarvis/`)**: FastAPI (`jarvis/api.py`, `jarvis/api_v2.py`, `jarvis/api_workspace.py`, `jarvis/api_learning.py`, `jarvis/api_skills.py`, `jarvis/api_decision.py`), SQLAlchemy 2 + Alembic (`jarvis/db/models.py`, `migrations/versions/0001..0006`, SQLite WAL / PostgreSQL + pgvector), Ollama native `/api/chat` client (`jarvis/brain.py`), multi-tier decision engine (`jarvis/decision/`), LangGraph / orchestrator (`jarvis/agents/orchestrator.py`), durable task queue (`jarvis/agents/tasks.py`, `task_runner.py`), 5-way vector/SQL memory (`jarvis/memory/five_way.py`), JSONL personal memory (`jarvis/memory_store.py`), JSONL conversation history (`jarvis/history.py`), scoped filesystem manager (`jarvis/filesystem_access.py`) + legacy `SafeFilesystem` (`jarvis/filesystem.py`), action executor (`jarvis/actions.py`), document engine (`jarvis/departments/assistant/documents.py`), reminders/productivity (`jarvis/departments/assistant/`), security boundaries (`jarvis/security/untrusted.py`, `pii_firewall.py`, `netsec.py`), and voice/STT/TTS pipeline (`jarvis/stt.py`, `asr_providers.py`, `audio_preprocess.py`, `translator.py`, `tts.py`, `voice.py`).
- **Frontend (`apps/frontend/src/`)**: React + TypeScript + Vite SPA routed via hash (`#/assistant`, `#/settings` in `App.tsx`) using SSE (`EventSource("/api/events")` in `live.ts`, `streamChat()` in `api/client.ts`), `AssistantHome.tsx`, `SettingsPanel.tsx`, `HologramCore.tsx`, `SpeechInspectorPanel.tsx`, `AudioVideoPanel.tsx`, `VoiceDock.tsx`, `BrainExplorer.tsx`, `GraphView.tsx`, and `Inspector.tsx`.

---

## 2. Existing Implementations (Reusable Building Blocks)

1. **Scoped Filesystem Permissions (`jarvis/filesystem_access.py`, `jarvis/db/models.py`)**:
   - `FilesystemPermissionRow` and `FilesystemPermissionRequestRow` track canonical paths, allowed operation sets (`READ_OPS`, `WRITE_OPS`, `EXECUTE_OPS`, `DELETE_OPS`, `ALL_OPS`), durations (`once`, `session`, `persistent`), sensitive path flags, and deferred action payloads.
   - Implements path canonicalization, traversal/symlink blocking, secret pattern awareness, OS folder picker (`pick_folder_dialog`), and execution helpers (`execute_list_directory`, `execute_search_files`, `execute_read_file`, `execute_read_metadata`, `execute_write_file`, `execute_delete_file`).
2. **Document Extraction & Generation (`jarvis/departments/assistant/documents.py`)**:
   - Already implements `parse_document()` and `parse_document_untrusted()` for `.pdf` (`pypdf`), `.docx` (`python-docx`), `.pptx` (`python-pptx`), `.xlsx`/`.xlsm` (`openpyxl`), and plain UTF-8 text, plus Excel formula neutralization (`_safe_cell`), `DocumentService`, and PDF/DOCX/PPTX/XLSX generation.
3. **5-Way Memory & Legacy Memory (`jarvis/memory/five_way.py`, `jarvis/memory_store.py`)**:
   - `FiveWayMemoryService` provides SQL + `VectorEntryRow` hybrid search across 5 namespaces (`personal_memory`, `project_knowledge`, `experience_memory`, `external_knowledge`, `personal_documents`), duplicate detection (`DuplicateMemoryError`), expiry/purge, archive/restore, superseding, audit logging, and PII/secret detection (`detect_secret`).
   - `PersonalMemoryStore` supports JSONL categories (`explicit`, `preference`, `project`, `temporary`, `feedback`), `infer_memory_category()`, duplicate checks, and encrypted backup/restore.
4. **Orchestration & Durable Tasks (`jarvis/agents/orchestrator.py`, `tasks.py`, `task_runner.py`, `db/models.py`)**:
   - `AssistantTaskRow` (with 0005 durable task columns: `worker_kind`, `attempts`, `max_attempts`, `checkpoint_json`, `steer_notes`, `completed_step_keys`, `step_budget`, `token_budget`, `tool_budget`, `claimed_by`, `claimed_at`), `AgentRunRow`, `AgentStepRow`, `ApprovalRequestRow`, and `BackgroundTaskRunner` with graceful shutdown & boot recovery.
5. **Action Executor (`jarvis/actions.py`) & System Telemetry (`jarvis/sysinfo.py`)**:
   - `ActionExecutor` implements `PermissionLevel` (`READ`, `OPEN`, `MODIFY`, `DESTRUCTIVE`, `SYSTEM`), 300s expiring `PendingApproval` tokens, `open_app` (`shell=False`), and `run_command` metacharacter filtering + command allowlists.
   - `jarvis/sysinfo.py` reports CPU cores/utilization, audio devices, RAM totals/percent, and basic NVIDIA GPU VRAM telemetry.
6. **Speech & Translation Pipeline (`jarvis/stt.py`, `asr_providers.py`, `audio_preprocess.py`, `translator.py`, `voice.py`)**:
   - Faster-Whisper + Silero VAD + RMS noise gate + resampling + optional Malayalam specialized providers + honest unavailable/confidence status + `VoiceTranscriptRow` persistence + manual correction + Speech Inspector panel.
7. **Frontend Permission, Approval & Task Drawer Foundations (`apps/frontend/src/features/AssistantHome.tsx`)**:
   - Inline FS permission cards (`permission_requested` -> `Allow Once` / `Allow Session` / `Always Read-Only` / `Deny` -> `POST /api/v2/filesystem/requests/{id}/decide` with `execute_deferred: true`), owner approval cards (`approval_requested`), task drawer (`GET /api/v2/tasks`), and live SSE state streaming.

---

## 3. Broken Integrations & Root Causes

### 3.1 Why Jarvis Claims "I cannot access files"
1. **System Prompt Refusal Instructions (`jarvis/persona.py` L11, L20)**:
   - `PERSONA` and `ONLINE_PERSONA` explicitly tell `qwen3:4b` that it cannot save/delete/change files directly, priming the model to refuse filesystem tasks even when read/inspection tools could be used.
2. **Zero Filesystem Tools Exposed to `Brain` (`jarvis/assistant.py` L77–107, L725–782, L1987–2029)**:
   - `_web_tools()` only registers `web_search` and `fetch_webpage`.
   - Local mode calls `_stream_llm_with_tts(..., tools=None)` with no tool-calling loop at all; online mode passes `_web_tools()` only.
   - `_execute_tool()` only dispatches web tools, never `FilesystemAccessManager` or document parsing.
3. **Rule-Locked Decision Routing (`jarvis/assistant.py` L1776, `jarvis/decision/rules.py` L9–37)**:
   - `self.orchestrator.decision.decide(text)` is called without passing `fast_llm`, keeping `DecisionEngine` locked to regex cascades (`FILESYSTEM_OPS_RE`, `DEV_KEYWORDS`).
   - Natural phrasing (e.g., `"Open ERP_V2, inspect the backend, find the database configuration and explain the database connection"`) hits `DEV_KEYWORDS` (`erp`, `backend`) and gets hijacked into `workforce_graph` (`_run_erp_dev_review()` reading raw AST of first 8 `.py` files, or `run_autonomous_dev_pipeline()` falling back to `SafeFilesystem` over `notes/` when unregistered).
4. **Fragmented Single-Shot `execute_deferred_action` (`jarvis/assistant.py` L1045–1168) & Raw Read (`filesystem_access.py` L1088–1117)**:
   - Deferred resume returns truncated raw text (e.g. `doc['content'][:300]`) directly without passing results through `Brain` for explanation/synthesis, and `FilesystemAccessManager.execute_read_file()` calls `Path.read_text(errors='replace')` on binary documents (`.pdf`, `.docx`, `.xlsx`) instead of calling `parse_document_untrusted()`.

### 3.2 Memory & History Split
- `Assistant._gather_context()` (`assistant.py` L532–564) queries legacy `PersonalMemoryStore` (`data/memory/records.jsonl`) and never queries `FiveWayMemoryService` (`orchestrator.memory`), nor injects project aliases or structured command memory.
- `POST /api/memory` bypasses `FiveWayMemoryService`, and conversation turns write to `data/history.jsonl` (`History`) separately from `ConversationSessionRow`/`ConversationMessageRow`.
- Command execution history is fragmented across `History`, `AssistantTaskRow`, and in-memory `TaskManager` (`actions.py`), with no unified natural-language replay/status/continue interface.

### 3.3 Configuration Schema Mismatch (`422` Failure) & Clamp Drift
- `jarvis/config.py` `FilesystemSpec` validates `sensitive_file_policy` as `^(always_ask|block)$` and `cloud_data_policy` as `^(local_only|redact_only|redact_and_confirm)$`, whereas `jarvis/api_v2.py` (`FsPolicyUpdateIn`) and `SettingsPanel.tsx` send `block|warn|metadata_only` and `never|redacted|approved_only`, causing `PUT /api/v2/filesystem/policy` to fail inside `apply_settings_patch`.
- `api_deps.py` validates `num_ctx` in `1024..8192` while `BrainSpec` allows up to `32768`.

### 3.4 Background Runner Scope & Cloud Tool Gap
- `AgentOrchestrator` (`orchestrator.py` L134–137) only registers `deep_research` with `BackgroundTaskRunner`; sequential multi-step workflows, checkpoints, and step-dependency execution are missing.
- `Brain.stream_events` (`brain.py` L365–368) skips `tools` when routing to `_cloud_complete`.

### 3.5 Orphaned Frontend Panels & Dropped `ui_action` Directives
- 11 frontend modules (`JarvisCorePanel.tsx`, `AssistantPanel.tsx`, `ChatPanel.tsx`, `DecisionPanel.tsx`, `GatewayPanel.tsx`, `LearningPanel.tsx`, `MemoryPanel.tsx`, `OverviewPanel.tsx`, `RecoveryPanel.tsx`, `SkillsPanel.tsx`, `WorkforcePanel.tsx`) are unimported in live `App.tsx`.
- Backend emits `ui: {"open_drawer": "memory"|"tasks"|"approvals"|"workforce"|"system"}` (`assistant.py`), but `App.tsx` `applyUiDirective` only handles `"explorer"`, `"system"`, and `focus: "graph"`.

---

## 4. Missing Capabilities Inventory

1. **Tool-Calling Local+Online Filesystem Access**: Brain-callable read-only tools (`fs_list_directory`, `fs_search_files`, `fs_read_file`, `fs_read_metadata`, `fs_get_project_structure`, `fs_find_files_by_extension`, `fs_read_document`) gated by `FilesystemAccessManager` + permission-card pause/resume + Brain synthesis of verified results.
2. **Approved Project Aliases**: User-managed named aliases (e.g., `ERP_V2 -> D:\...\ERP_V2`) bound to approved filesystem paths so NL commands resolve reliably without exposing unapproved paths.
3. **Multi-Step Checkpointed Orchestration**: Structured task classifier + sequential dependency runner passing verified step outputs to subsequent steps and `Brain`, supporting timeouts, bounded retries, owner cancellation, and permission pause/resume.
4. **Unified Command History & Continuity**: Searchable command/task execution records supporting NL queries (`repeat last command`, `show recent tasks`, `continue previous task`, `what happened to my last request`, `show failed tasks`, `cancel current task`) with fresh permission checks on replay.
5. **Connected Adaptive Memory**: Unified `FiveWayMemoryService` + structured categories (`User preferences`, `Project memory`, `Command memory`, `Conversation memory`), per-category enable/disable toggles, UI view/correct/delete/clear, secret exclusion, and deterministic retrieval/frequency-based adaptive ranking.
6. **Schema-Validated Desktop & Browser Action Registry**: Extended `ActionExecutor` with rich metadata (ID, description, permissions, input/result Pydantic schemas, risk tier `Read-only`/`Low`/`Medium`/`High`, timeout), window management, media controls, screenshots, allowlisted apps/URLs, and browser untrusted-content & side-effect confirmations.
7. **Extended System Monitoring & Opt-In Proactive HUD Suggestions**: Full `sysinfo.py` telemetry (CPU/temp if available, GPU/VRAM/temp, RAM, disks/health, battery, network, top processes, Jarvis backend/model/queue/task status) with honest unavailable reporting, configurable polling/thresholds, and opt-in non-executing HUD suggestion cards.
8. **Voice Uncertainty Safety & Personality Presets**: STT quality/uncertainty clarification before consequential actions, strict filename/path preservation, honest translation validation notices, and orthogonal KV-cache-friendly personality presets (`Professional`, `Friendly`, `Concise`, `Technical`, `Detailed`, `Custom`).

---

## 5. External Repository Comparison & Risk Assessment

### 5.1 Repository A: `Avinashb722/jarvis-ai-assistant` (License: `MIT`)
*(Already audited in `EXTERNAL_JARVIS_REPOSITORY_AUDIT.md` — concise comparative summary)*
- **Useful Concepts**: Fuzzy/adaptive command aliasing & frequency boosting (`CommandLearningSystem`), user preference profiles, multi-step compound command splitting, system resource/process/battery checks, context-aware suggestion prompts.
- **Unusable / Dangerous Code**: Unbounded `exec()`/`subprocess` execution, raw `os` file/folder manipulation without permission boundaries, committed credentials/private data files (`gmail_credentials.json`, personal data/tokens), blocking console `input()` / synchronous sleeps, incompatible legacy audio/TTS wrappers (`pyttsx3`/`SpeechRecognition`).
- **Adoption Decision**: Adopt **clean-room concepts only** (frequency-boosted alias/command ranking without auto-training; compound NL step decomposition; OS process/battery monitoring; rule-backed proactive suggestions) integrated strictly inside Jarvis's permission/DB architecture. Zero files copied.

### 5.2 Repository B: `AnubhavChaturvedi-GitHub/jarvis-ai-assistant` (License: `GPL-3.0`)
**CRITICAL LICENSE CONSTRAINT**: Repository B is licensed under **GNU General Public License v3.0 (`GPL-3.0`)**. Copying source code from GPL-3.0 into our codebase would trigger strong copyleft obligations across Jarvis. Therefore, **zero source code, prompt text, or assets from Repository B may be copied**; only high-level architectural patterns observed clean-room are evaluated below:
- **Observed Architecture (`requirements.txt`, `jarvis.py`, `co_brain.py`, `Automation/`, `Features/`, `Time_Operations/`, `Vision/`)**:
  - `jarvis.py` / `co_brain.py`: Multi-threaded infinite polling loops combining Speech/Text input with parallel `Battery.battery_Alert` (`psutil.sensors_battery`) and `Time_Operations/throw_alert` scheduled alerts, routing commands via keyword substring checks to `Automation_Brain.py`, `Time_Operations/brain.py`, `create_file.py`, `Brain/brain.py` (`webscout.PhindSearch`), and Windows desktop helpers.
  - `Automation/open_App.py`, `tab_automation.py`, `Youtube_play_back.py`, `scrool_system.py`: Launches apps via unvalidated `subprocess.run(text)` with `pyautogui` Win-key fallback; drives browser tabs, scroll, and media keys via blind `pyautogui.hotkey(...)` keystrokes regardless of active window focus.
  - `Automation/Web_Open.py` + `Web_Data.py`: Allowlist map of website names to URLs opened via `webbrowser.open()`.
  - `Features/set_get_volume.py`, `set_br.py`, `check_running_app.py`: Windows audio volume via `pycaw` / `comtypes`, screen brightness via `wmi` (`WmiMonitorBrightnessMethods`), running process listing via `psutil`.
  - `Features/create_file.py`, `Vision/Vbrain.py`, `ui.py`: Hardcoded extension lookup (`if "python file" in text -> .py`), OpenCV camera capture (`cv2.VideoCapture(0)`), PyQt5 transparent GIF widget.
- **Critical Security, Reliability & Architecture Flaws in Repository B**:
  - **No Permission or Confirmation Boundaries**: Unvalidated `subprocess.run(text)`, Win+Run keyboard macro injection (`pyautogui.write`), and unguarded file creation in CWD.
  - **Fragile Blind GUI Injection**: `pyautogui.hotkey` sends keystrokes blindly to whichever window happens to hold OS focus.
  - **Hardcoded Paths, Committed Binaries & Mutable Text State**: Hardcoded `C:\Users\chatu\...` paths in `Brain/brain.py`, committed `chromedriver.exe` (17.8 MB) and `.pyc` files, unencrypted flat `.txt` state files (`history.txt`, `schedule.txt`, `Alam_data.txt`, `PyWhatKit_DB.txt`).
  - **Tight Infinite Loops / Resource Waste**: Tight `while True` loops in background monitoring threads without backoff or graceful shutdown.
  - **External Unverified LLM Scraper**: Relies on `webscout.PhindSearch` scraping rather than local Ollama + verified search.
- **Clean-Room Patterns Worth Designing Independently in Jarvis**:
  1. Website shortcut allowlist lookup (`web_open` mapped strictly to validated URLs, never arbitrary shell execution).
  2. Windows `psutil` process + battery + brightness/volume telemetry & controls wrapped inside `ActionExecutor` risk tiers & schema validation (with honest unsupported fallbacks).
  3. Bounded scheduled event/battery/system threshold triggers routed to non-intrusive opt-in HUD suggestion cards instead of modal OS popups or TTS spam.

---

## 6. Dependency-Ordered Implementation Plan (Stages A → G)

Each stage must pass its dedicated unit/integration checks (`pytest`, `ruff`, `mypy`, frontend tests/build where applicable) before dependent stages begin:

| Stage | Scope | Key Modules & Deliverables | Verification Gate |
|---|---|---|---|
| **Stage A** | Core Execution — Intelligent Filesystem Access & Project Aliases | Fix `persona.py`, add doc extraction/encoding/secret-redaction/project-tree/extension-search to `filesystem_access.py`, add DB project aliases (`0007` Alembic migration if needed), expose 7 FS tools in `assistant.py` (`stream_turn` local+online tool loop + pause/resume + Brain synthesis), fix `422` FS policy enum mismatch | Stage A FS/tool/permission/redaction/migration tests + `ruff` + `mypy` |
| **Stage B** | Multi-Step Orchestration & Command Continuity | Task classifier, checkpointed sequential dependency step runner inside `AgentOrchestrator` + `AssistantTaskService` + `BackgroundTaskRunner`, unified command history & NL replay/status/continue/cancel with fresh permission checks | Stage B multi-step E2E (including `"Open ERP_V2..."`) + replay/cancel/retry tests |
| **Stage C** | Adaptive Memory Integration | Connect `FiveWayMemoryService` to `Assistant._gather_context()`, unify categories (`User preferences`, `Project memory`, `Command memory`, `Conversation memory`), category toggles, API/UI view/correct/delete/clear, secret exclusion, retrieval/frequency adaptive ranking | Stage C memory isolation, secret rejection, ranking & toggle tests |
| **Stage D** | Desktop & Browser Automation | Upgrade `jarvis/actions.py` with full Action Registry metadata (schemas, risk tiers, timeouts, allowlisted apps/URLs, window focus/list, media, screenshot, safe keyboard/mouse), browser public vs authenticated vs side-effect confirmations | Stage D schema, allowlist, metacharacter injection, approval gating & timeout tests |
| **Stage E** | System Monitoring & Proactive Suggestions | Extend `jarvis/sysinfo.py` (CPU, temps, GPU/VRAM/temp, RAM, disk/health, battery, network, processes, Jarvis health/queue) + clean types + configurable polling/alerts + opt-in WS/SSE HUD proactive cards (no auto-exec) | Stage E sysinfo unavailable handling, threshold alerts & opt-in suggestion tests |
| **Stage F** | Multilingual Voice Safety, Personality Presets, Frontend HUD & Settings | STT uncertainty clarifications + filename/path preservation, KV-efficient personality presets orthogonal to safety rules, live React HUD panels (Task execution, Memory & Aliases, Desktop controls, Sysinfo, Command history, `ui_action` drawer wiring), clearer Settings + unit tests & headless Chrome E2E | Stage F `npm test`, `tsc`, `vite build`, headless Chrome E2E & voice safety tests |
| **Stage G** | Hardening, Performance, Documentation & Full Regression | Full backend `pytest` (including standalone mic/audio test run), `ruff`, `mypy`, Alembic round-trip, performance checks for Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM, write all 7 implementation/security reports + update 6 project docs | Zero test failures, clean lint/typecheck, all deliverables verified on disk |

