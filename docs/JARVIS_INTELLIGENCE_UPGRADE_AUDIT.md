# JARVIS Intelligence Upgrade — Comprehensive Architecture Audit

**Date:** 2026-11-13  
**Status:** Baseline Established & Audited  
**Environment:** Windows 11, Ryzen 5 5600H, GTX 1650 4GB VRAM, 16GB RAM, Python 3.14 venv, FastAPI, React/TypeScript Vite HUD  
**Baseline Test Results:** 292 passed, 4 skipped, 0 failed in 348.75s  

---

## 1. Executive Summary

This audit establishes the baseline architectural state of the Jarvis Personal AI Assistant before implementing Phases P0 through P6 of the Master Intelligence Upgrade. 

Jarvis has an extensive foundation of capabilities:
- A 3-Layer Decision Engine (`jarvis/decision/`) with heuristic and LLM classification.
- A hardened model gateway (`jarvis/gateway/`) and Ollama/cloud brain adapter (`jarvis/brain.py`).
- A multi-tier filesystem access manager (`jarvis/filesystem_access.py`) enforcing workspace boundaries and project disambiguation.
- A durable task service and background runner (`jarvis/agents/tasks.py`, `jarvis/agents/task_runner.py`).
- A high-integrity audio and speech translation pipeline (`jarvis/stt.py`, `jarvis/translator.py`, `jarvis/voice.py`).
- A 5-way hybrid memory system (`jarvis/memory/`, `jarvis/memory_store.py`).
- A React/TypeScript Dark HUD frontend with live WebSocket/SSE streaming.

Despite these components, several architectural bottlenecks and disconnects degrade the user experience:
1. **Context-Blind Intent Routing:** The DecisionEngine classifies every turn in isolation without passing recent conversation turns, active tasks, or the active project.
2. **Artificial Task Pollution:** Conversational chat messages (e.g. "What is the difference between VRAM and RAM?") unconditionally create background tasks in SQLite.
3. **Missing Conversational Task Steering:** Users cannot steer, pause, or query active background tasks using natural language; steering is only exposed via raw REST API.
4. **Episodic Project Context Loss:** Brain tool executions (`fs_get_project_structure`, `fs_read_file`) do not update the assistant's active project state.
5. **Model Role Misconfiguration:** Coding models are assigned as default general brains when cloud providers are configured.
6. **Voice Turn Blocking:** Long-running background workflows synchronously block the voice loop and speech turn.

---

## 2. Current Architecture Subsystem Audits

### 2.1 Current Conversation Pipeline

The conversation turn lifecycle runs through `Assistant.stream_turn` -> `Assistant._stream_turn_unlocked` in `jarvis/assistant.py`:
1. **Locking:** Acquires `_turn_lock` (blocking concurrent interactive turns).
2. **Clarification Intercept:** Checks `_get_active_pending_clarification()`. If pending, routes to `_handle_pending_clarification_reply`.
3. **UI Command Match:** Evaluates `_match_ui_command(text)` for drawer opens/closes.
4. **Deterministic Intent Router:** Runs `commands.route(self, text)` via `jarvis/intent.py`.
5. **Decision Engine:** If unrouted, calls `plan = await self.orchestrator.decision.decide(text)`.
6. **Task Record Creation:** Calls `orchestrator.tasks.create_task(text, ...)` unconditionally.
7. **Approval Gate:** If `requires_owner_approval`, generates an approval token and suspends the turn.
8. **Planned Workflow Execution:** Calls `_execute_planned_workflow(...)` for dev workforce, documents, reminders, research, or direct filesystem ops.
9. **Brain RAG + Tool Loop:** If workflow is None, gathers memory/note context (`_gather_context`) and streams tokens via `_iter_tool_turn`.

**Identified Issues:**
- `_turn_lock` holds the entire duration of long multi-step turns, blocking quick conversational answers.
- The conversation history is stored in-memory in `History` and appended to `history.jsonl`, but recent turns are not fed into `orchestrator.decision.decide`.

### 2.2 Current Intent Routing

Routing is partitioned across two layers:
1. **Fast Deterministic Regex Router (`jarvis/intent.py`):**
   - Contains ~50 compiled regexes for media controls, voice rate/switching, model switching, memory confirm/forget, and simple file read/search.
   - Gated by `_host_fs_cue`: queries mentioning file drives or host paths bypass regex routing and proceed to the DecisionEngine.
2. **3-Layer Decision Engine (`jarvis/decision/`):**
   - **Layer 1 (`rules.py`):** Slash commands (`/status`, `/project`, `/code`) and emergency stop.
   - **Layer 2 (`classifier.py`):** `ExistingHeuristicClassifier` (regex department/intent), `ExistingLLMClassifier` (JSON schema output), and `IsolatedLayaClassifier` (shadow/experimental).
   - **Layer 3 (`router.py`):** Maps `DecisionEnvelope` to `RoutingPlan` (`workflow`, `primary_model`, `agent_role`, `allowed_tools`).

**Identified Issues:**
- Intent classification is context-free. A follow-up like *"Focus more on the backend"* or *"What about the database?"* cannot resolve what "it" refers to.
- `INSPECT_PROJECT` regex only matches `(?:inspect|analyze) (?:my |the )?(?:project|codebase)(?: at (.+))?`, failing on natural instructions like *"Explore my ERP_V2 project and explain its architecture"*.

### 2.3 Current STT and Translation Flow

The voice and speech pipeline spans `jarvis/voice.py`, `jarvis/stt.py`, `jarvis/asr_providers.py`, `jarvis/translator.py`, and `jarvis/audio_preprocess.py`:
1. **Audio Capture:** Native 48 kHz / 44.1 kHz input stream in `VoiceLoop` converted to 16 kHz mono float32 for Silero VAD and openWakeWord.
2. **Buffering & Storage:** Raw, un-preprocessed audio is immutably stored in `AudioBufferStore` at `data/audio/{aid}.wav`.
3. **Preprocessing:** DC rumble removal (65 Hz Butterworth HPF), margin-preserving silence trim (160ms boundary), gated amplitude normalization (+14 dB cap, noise floor protected), short utterance padding (160ms).
4. **ASR Decoding:** `FasterWhisperProvider` (CPU INT8 with LRU cache) or specialized `MalayalamASRProvider`.
5. **Quality Gate & Multi-Pass Retry:** `evaluate_transcript_quality` detects repetition loops, script mismatch (Devanagari emitted when Malayalam requested), high no-speech probability, and excessive speaking rates. Bounded multi-pass retries adjust beam size, temperature, and VAD parameters.
6. **Transcript Lifecycle:** `TranscriptRecord` preserves `original_transcript` immutably in DB and memory. Copy editing is guarded by `guard_correction` and translations are verified by `verify_translation`.
7. **Acoustic Feedback Protection:** Safe half-duplex mic suspension during TTS playback (`_turn_gate.set()`), drain wait on Piper audio playback, and a 350ms room reverberation cooldown delay.

**Identified Issues:**
- Background tasks initiated by voice block the voice loop synchronously for minutes without an immediate acknowledgment.
- The UI exposes `SpeechInspectorPanel.tsx` as a separate floating card rather than an integrated tab inside the Operations Center.

### 2.4 Current Model Routing

Model routing operates via `ModelGateway` (`jarvis/gateway/gateway.py`) and `Brain` (`jarvis/brain.py`):
1. **Model Capability Registry:** Gateway defines capability profiles (`streaming`, `tool_calling`, `json_mode`, `embeddings`, `max_ctx`, `pricing_tier`).
2. **Provider Adapters:** Local Ollama adapter, OpenAI-compatible adapter (OpenAI, OpenRouter, HuggingFace), and Gemini adapter.
3. **Breakers & Resilience:** Per-provider circuit breakers with automatic backoff and local Ollama fallback.
4. **Brain Host Isolation:** Ollama host (`http://127.0.0.1:11434`) is strictly isolated from cloud base URLs, preventing tags/ps query leakage to cloud endpoints.

**Identified Issues:**
- Coding models (such as `openrouter:qwen/qwen3-coder`) are often selected as the active model for general conversational queries.
- The routing plan in `DecisionEngine` lacks task-specific model role dispatching (Fast Model for simple QA, Reasoning Model for architecture/diagnostics, Coding Model for software exploration).

### 2.5 Current Project Resolution

Project discovery in `jarvis/filesystem_access.py::FilesystemAccessManager.resolve_project_target`:
1. **Step 1 (Aliases):** Exact or fuzzy match against `ProjectAliasRow` table.
2. **Step 2 (Registered Projects):** Matches registered dev workspaces in `ProjectRow`.
3. **Step 3 (Active Project Root):** Matches current active project or sub-folder context.
4. **Step 4 (Trusted Workspace Roots):** Depth-2 scan across configured `workspace_roots`, strictly filtering temporary, build, and hidden directories.
5. **Step 5 (Disambiguation):** Generates numbered candidate prompts (`1. ProjectA`, `2. ProjectB`) when multiple genuine candidates match. Ordinal replies ("the second one") are parsed by `_handle_pending_clarification_reply`.

**Identified Issues:**
- When Brain tool calls (`fs_get_project_structure`, `fs_read_file`) inspect a project, `self.active_project` is not updated.
- There is no automated full-project exploration engine that autonomously builds an architectural inventory, discovers entry points, extracts API routes, and catalogs test/build commands without manual step-by-step steering.

### 2.6 Current Task Execution Flow

Task management resides in `jarvis/agents/tasks.py` (`AssistantTaskService`) and `jarvis/agents/task_runner.py` (`BackgroundTaskRunner`):
- State transitions are validated by `validate_task_transition` across 13 canonical states (`queued`, `planning`, `running`, `awaiting_permission`, `awaiting_approval`, `awaiting_clarification`, `paused`, `completed`, `failed`, `cancelled`, etc.).
- Resource budgets: step limit, token budget, tool budget, time limit.
- Task checkpoints are persisted in SQLite `assistant_tasks` table.

**Identified Issues:**
- Conversational chat requests create task rows, cluttering the task list.
- Task timeline events are emitted on the in-memory EventBus, but lack a unified, persistent, sequence-numbered execution timeline database with replay capabilities.

### 2.7 Current Event Streaming

Realtime updates are delivered via Server-Sent Events (SSE) and WebSocket:
- `/api/events` (and `/api/chat/stream`) publishes events via `EventBus`.
- Frontend listens via `onEvent` in `apps/frontend/src/live.ts`.
- Events include `task_created`, `task_updated`, `task_paused`, `task_resumed`, `permission_requested`, `os_state`, `level`, `transcript`.

**Identified Issues:**
- Events lack monotonic sequence numbers per task.
- If a browser reconnects or refreshes, missed intermediate events cannot be replayed by sequence offset.

### 2.8 Current Memory Architecture

The memory system comprises:
- `jarvis/memory_store.py`: Keyword/semantic memory store for user notes and facts (`memories` table).
- `jarvis/memory/five_way.py`: 5-Way isolated memory partitions (`user_profile`, `preferences`, `project_facts`, `technical_knowledge`, `conversation_summaries`).
- `jarvis/learning/feedback.py`: Verified feedback and correction store with owner approval workflow.
- `jarvis/retrieval.py`: Hybrid BM25 + vector search over knowledge chunks.

**Identified Issues:**
- General conversational turns append memory unconditionally without confidence/utility filtering.
- Long-term memory lacks structured provenance tracking linking facts directly to the source task or user confirmation.

### 2.9 Current Frontend Integration

The user interface is built with React 19, TypeScript, and Vite:
- `AssistantHome.tsx`: Main HUD workspace rendering 3D Hologram, chat feed, permission cards, and clarification candidates.
- `OperationsDrawer.tsx`: 10-tab operations center (`tasks`, `agents`, `research`, `routines`, `actions`, `history`, `aliases`, `memory`, `desktop`, `system`).
- `VoiceDock.tsx`: Real-time audio waveform visualizer and PTT/Host mic toggles.
- `SpeechInspectorPanel.tsx`: Speech & translation diagnostics inspector.
- `AudioVideoPanel.tsx`: Device inventory and microphone test meter.

**Identified Issues:**
- Task execution progress in `OperationsDrawer.tsx` is summary-based and lacks a live step-by-step activity timeline.
- Speech Inspector is separated from the Operations Center drawer.

---

## 3. Comprehensive Implementation Plan (P0–P6)

| Phase | Core Objective | Key Deliverables & Implementation Targets |
|---|---|---|
| **P0: Conversation Intelligence & Intent Understanding** | Context-aware request analysis, separation of chat vs tasks, conversation continuity | 1. Implement structured `RequestContext` containing active task, active project, and recent turns.<br>2. Upgrade `DecisionEngine.decide` to accept `RequestContext`.<br>3. Bypass `create_task` for `direct_chat` / `general_chat` turns.<br>4. Implement conversational task steering ("focus on backend", "pause task").<br>5. Enhance system prompts for honest, accurate, non-hallucinatory replies. |
| **P1: Speech Recognition & Translation Intelligence** | Immutable transcripts, echo prevention, multilingual script integrity | 1. Ensure immutable `original_transcript` pipeline contract across all voice endpoints.<br>2. Add non-blocking voice-to-background task dispatch with instant voice acknowledgment.<br>3. Enforce script consistency checks for Malayalam, Manglish, Hindi, and English.<br>4. Structural verification of numbers, dates, negations, and entities on translated text.<br>5. Mount Voice Diagnostics tab in Operations Center. |
| **P2: Autonomous Project Exploration Engine** | Autonomous multi-step codebase exploration, architecture mapping, project memory | 1. Build `ProjectExplorerAgent` that autonomously analyzes manifests, entry points, routes, models, and tests.<br>2. Update `self.active_project` context on all filesystem tools.<br>3. Generate structured project architecture reports with AST and dependency inventories.<br>4. Persist structured project memory in `ProjectRegistryService`. |
| **P3: Live Task Execution & Activity Timeline** | Persistent, sequence-numbered execution timeline with live SSE streaming | 1. Create `TaskTimelineEvent` schema with monotonic sequence numbers and correlation IDs.<br>2. Persist execution events in SQLite (`task_events` table).<br>3. Stream live timeline events over SSE with replay support (`/api/v2/tasks/{id}/events`).<br>4. Upgrade `OperationsDrawer.tsx` Tasks tab with real-time execution timeline and controls. |
| **P4: Model Routing & Answer Quality** | Task-based model routing, coding model correction, evaluation dataset | 1. Expand model registry with specialized task roles: `fast_model`, `reasoning_model`, `coding_model`.<br>2. Route general conversation to general instruction models, coding tasks to coder models.<br>3. Implement automated model evaluation benchmark dataset.<br>4. Hardened fallback behavior preserving privacy and local-mode boundaries. |
| **P5: Memory, Personalization & Controlled Learning** | 9-category memory isolation, provenance tracking, controlled skill learning | 1. Refine memory classification into 9 distinct categories with source provenance.<br>2. Strict memory write policy preventing storage of transient or unverified data.<br>3. Controlled correction loop with versioned approval and rollback.<br>4. Reusable skill learning with explicit owner authorization gate. |
| **P6: Voice Experience, Verification & Full Integration** | Instant acknowledgment, natural turn-taking, verified task completion | 1. Fast voice acknowledgment for multi-step tasks before background execution.<br>2. Voice task controls ("What are you doing?", "Show progress", "Stop").<br>3. Strict outcome verification: distinguish `verified`, `unverified`, and `failed`.<br>4. Scenario K end-to-end integration test spanning voice, tasks, and project exploration. |

---

## 4. Verification Requirements & Baseline Metrics

- **Current Non-Live Test Suite:** 292 passed, 4 skipped, 0 failed in 348.75s.
- **Frontend Test Suite:** 12 passed, 0 failed.
- **Frontend Production Build:** Clean (0 errors).
- **Target Regression Criterion:** 0 regressions across all existing test suites. Every phase will include dedicated unit and integration tests verifying new capabilities before advancing.

