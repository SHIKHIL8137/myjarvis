# P10.21 — Existing Jarvis Feature Verification Report (Phase 0 & Areas A–J)

**Audit Date:** 2026-11-12  
**Jarvis Workspace:** `d:\projects\myjarvis`  
**Jarvis Git HEAD Verified:** `fb007e47803e7b6e9af117368ae67f9f33d6a0b8` (working tree also includes P10.20 React UI fixes and built `apps/frontend/dist/`)  
**Python Environment:** `d:\projects\myjarvis\.venv\Scripts\python.exe` (`Python 3.14.6`)  
**Audit Method:** Strict read-only source inspection of Jarvis modules + cross-reference against recorded post-edit test/preflight/drill reports (`P10_13_TEST_REPORT.md`, `P10_18_VOICE_INTEGRATION_REPORT.md`, `P10_19_TEST_REPORT.md`, `P10_19_VOICE_E2E_REPORT.md`, `P10_20_E2E_TEST_REPORT.md`). Zero Jarvis source/DB/config files were modified.

---

## 1. Existing Verification Baseline & Honest Live-Environment Boundaries

Per `P10_20_E2E_TEST_REPORT.md` and `P10_19_TEST_REPORT.md`, the current codebase has:
- **Python Automated Suite (`pytest`)**: **196 passed, 4 skipped, 0 failed** (`P10_20_E2E_TEST_REPORT.md`). Exact skips were live environment dependencies unavailable on the host:
  1. `tests/test_p10_11_hardening.py:221` — Docker daemon stopped on host.
  2. `tests/test_p10_11_hardening.py:378` — `JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL` unset.
  3. `tests/test_p10_12_hardening.py:165` — Docker daemon stopped on host.
  4. `tests/test_p10_pgvector_memory.py:193` — `JARVIS_PG_TEST_URL` unset.
- **Frontend (`apps/frontend`)**: `tsc --noEmit` (0 errors), `node --test src/graphCore.test.ts` (6/6 passed), `vite build` (41 modules, 0 external URLs).
- **Live Isolated Server (`preflight.py` + `drill.py` on `127.0.0.1:4720` / `4700`)**: **33 pass, 0 fail, 1 warn** (missing optional `torch`, `peft`, `transformers` LoRA pkgs); live Whisper STT <-> Piper TTS WAV loopback (`2.3s` / `0.8s`), host mic open/close (`/api/voice/mic`), live `qwen3:4b-instruct` `/api/chat` & `/api/chat/stream`, host-folder scoped `allow_once` flow, and zero-egress `drill.py` (`api.anthropic.com` blocked by `sys.addaudithook`).
- **What Remains UNVERIFIED on Real Hardware / Live Infrastructure**:
  1. **Live Chrome DevTools Browser E2E / WebGL FPS / Browser `getUserMedia` WebMic**: Blocked (`DevToolsActivePort` absent in host Chrome data dir).
  2. **Physical Room Microphone + Speaker Full-Duplex AEC Barge-In**: Half-duplex is the configured default (`config.json` `"duplex_mode": "half_duplex"`); physical acoustic speaker-bleed cancellation (`ERLE >= 8.0 dB` via `NLMSEchoCanceller.calibrate`) has not been live-calibrated on physical room speakers/mic.
  3. **Live Wake-Word Continuous Ambient Room Listening & Bluetooth Reconnect Under Real Disconnect**: Covered only in mocked/unit `test_continuous_voice.py`, `test_speech.py`, `test_p10_19_autonomous_voice_os.py`.
  4. **Live Docker OpenCode Sandbox & Live PostgreSQL + pgvector Restore**: Skipped due to stopped Docker daemon and unset `JARVIS_PG_*` URLs.

---

## 2. Source-Level Verification by Audit Area (Areas A–J)

### Area A — Voice & Audio Reliability
- **Inspected Files**: `jarvis/wakeword.py` (1–37), `jarvis/aec.py` (1–170), `jarvis/stt.py` (1–160), `jarvis/tts.py` (1–150, 480–616), `jarvis/voice.py` (1–160, 480–909), `config.json` (27–78), `P10_19_VOICE_E2E_REPORT.md`.
- **What Exists**:
  - Formal `VoiceLoop` state machine (`VoiceState`), stream open/close barrier events (`_stream_open_ev`, `_stream_closed_ev`) preventing fake `Listening` status, Silero VAD + RMS onset hysteresis, polyphase resampling to 16 kHz, safe half-duplex default releasing hardware mic during TTS + `mic_resume_delay_ms=300`, optional NLMS AEC (`NLMSEchoCanceller`, `EchoReferenceBuffer`) gating full-duplex barge-in strictly on measured `ERLE >= 8.0 dB`, Bluetooth output fallback + PortAudio `_terminate/_initialize` refresh on stream retry (`_open_stream` / `_play_with_bt_recovery`), local `openwakeword` `hey_jarvis_v0.1.onnx` (`WakeWord`), streaming sentence-chunked Piper TTS (`StreamingSpeechBuffer` suppressing code blocks), and `interrupt_all()` cancellation.
- **Verified Gaps**:
  - **No Wake-Word Auto-Sleep Timer**: `voice.py:795-812` implements short `wake_followup_s` (`6.0s`) after a wake-word turn, whereas Mark LVII has a 120s active awake idle watchdog + explicit awake/sleep state gate.
  - **No Startup Audio Probe for Silent Drivers**: `_open_stream_once` falls back to native device sample rate, and `Speaker._play_with_bt_recovery` falls back to default device, *but* Jarvis lacks Mark's timed silence read/write transport verification (`_transport_works`) to catch silent DirectSound sinks vs MME.
  - **Static Single Configured Piper Voice**: `config.json:29` fixes `tts.voice = "en_GB-alan-medium"`. While STT/Translator supports `en`/`ml`/`hi`/`manglish`, Piper synthesis does not auto-switch loaded `.onnx` voices dynamically when response script changes.
  - **Live Hardware Barge-In / Room Wake-Word / BT Disconnect**: Unit-tested, **not live-verified on physical room hardware**.

### Area B — Background Tasks & Execution Persistence
- **Inspected Files**: `jarvis/agents/tasks.py` (1–210), `jarvis/agents/checkpointer.py` (1–100), `jarvis/agents/orchestrator.py` (1–310, 440–560, 680–812), `jarvis/actions.py` (1–297), `jarvis/assistant.py` (175–310).
- **Critical Three-Way Distinction Required**:
  1. **Persisting Task Metadata**: **YES** (`AssistantTaskService` persists `AssistantTaskRow` with `status`, `plan_json`, `selected_agents`, `tools_used`, `current_step`, `result_summary`, `error_info`, and publishes `task_updated` SSE events). Note legacy `actions.py:TaskManager` is pure RAM (`maxlen=100`).
  2. **Persisting Workflow Checkpoints**: **PARTIAL** (`DBBackedCheckpointSaver` persists LangGraph `storage`/`writes`/`blobs` into `AgentRunRow.state_payload`, and loads them in `checkpointer.py:__init__`).
  3. **Automatically Recovering and Resuming Execution**: **MISSING**. `Assistant.start()` (`assistant.py:199-207`) only starts `_watch_notes`, `_watch_knowledge`, `_watch_reminders`, and `_watch_memory_expiry`. Neither `AssistantTaskService` nor `AgentOrchestrator` scans for orphaned `"running"` / `"queued"` / `"planning"` tasks or `AgentRunRow` runs at boot, re-queues them, enforces retry bounds (`MAX_ATTEMPTS`), manages step/time budgets with toolless graceful wrap-up, supports `not_before` scheduling, handles `waiting_quota` suspension, or allows live steering (`steer`). `DBBackedCheckpointSaver` is ONLY resumed manually when `resume_workflow_after_approval(token=...)` is explicitly called!
- **Status**: **Partially implemented** (metadata + LangGraph checkpoint storage exist; autonomous background queue/worker, startup recovery, retry limits, quota pause, and steering are missing).

### Area C — Deep Research & Multi-Step Information Synthesis
- **Inspected Files**: `jarvis/agents/assistant_graph.py` (1–148), `jarvis/assistant.py` (695–717, 1561–1578), `jarvis/search.py` (380–569), `jarvis/learning/knowledge_pipeline.py` (1–100).
- **What Exists**: `WebSearchService` (`duckduckgo`/`tavily`/`brave`/`serper`/`searxng` + `fetch_page` with `robots.txt` + HTML extraction + `netsec.safe_request`), single-turn LLM web tool loop in `stream_turn()`, offline `KnowledgePipeline` fallback when offline (`web_offline` in `assistant.py:1562-1576`), and `DocumentService` PDF/DOCX/PPTX/XLSX export.
- **Verified Gaps**:
  - In `assistant_graph.py`, `task_kind="research"` routes to `summarize`, which **literally only searches personal/experience memory** (`Gathered N relevant personal/experience items.`) and runs **zero** web research!
  - `fetch_page()` truncates plain text at `2500` chars and caches temporarily in RAM without persisting SHA1-keyed full source snapshots (`pages/`) for post-hoc verification.
  - Zero multi-stage iterative loop (`PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY/REVISE`), zero contradiction detection, zero code-level `quote_support()` substring/window check against fetched pages, and zero citation `[n]`/`[?]` verification.
- **Status**: **Partially implemented** (web search + page fetch + doc export exist; multi-stage verified deep research is missing).

### Area D — Routines & Proactive Automation
- **Inspected Files**: `jarvis/departments/assistant/reminders.py` (1–75), `jarvis/assistant.py` (175–310, 1293–1326), `jarvis/learning/training/training_scheduler.py` (1–100).
- **What Exists**: `ReminderService` polls due reminders every 15s (`_watch_reminders`) and stores a mostly unused `recurrence_cron` field; internal watchers exist for notes/knowledge/memory expiry and LoRA training readiness.
- **Verified Gaps**: No unified `RoutineEngine` supporting arbitrary user/voice-managed routines (`say`/`prompt`/`task`/`builtin`), weekly/monthly/interval/startup/idle triggers, missed-run catch-up grace windows, `quiet_hours` separation (unprompted check-ins suppressed vs explicit user alarms), pause/resume, or run-history inspection.
- **Status**: **Partially implemented**.

### Area E — Verification, Confirmation, Undo & Error Handling
- **Inspected Files**: `jarvis/agents/approvals.py` (1–170), `jarvis/filesystem_access.py` (1–200 + inspected methods), `jarvis/actions.py` (1–297), `jarvis/audit.py` (1–196).
- **What Exists**: Strong DB-backed `ApprovalService` (READ/WRITE/EXECUTE risk tiers, TTL 900s, single-use `consume_approved()`), `FilesystemAccessManager` (9 granular ops, `once`/`session`/`persistent` read-only scopes, canonical path + reparse + TOCTOU checks), and `AuditEventRow` logging.
- **Verified Gaps**:
  - **Reversible Undo Journal (`Missing`)**: `actions.py._history` and `AuditEventRow` are audit logs only — there is zero reversible operation journal (no file-edit/move/trash/settings rollback stack with conflict detection).
  - **Uniform Tool Outcome Protocol (`Missing`)**: Tool responses return mixed dicts (`ok`/`status`/`reply`) without a standardized `done` / `unverified` / `failed` post-condition contract blocking LLM fabrication.
  - **Quick HUD Countdown Confirmation (`Partially implemented`)**: Full inline permission & approval cards exist, *but* `ApprovalRequestRow` defaults to 15-min passive queue rather than Mark's 90s voice-first confirm/cancel banner.

### Area F — Perception, Screen & Multi-Modal Context
- **Inspected Files**: `config.json` (10–26), `jarvis/gateway/gateway.py` (168–246, 428–461), `jarvis/gateway/providers.py` (237–515), Repo-wide search for `qwen2.5vl|screenshot|webcam|camera|require_vision`.
- **What Exists**: `ModelGateway` schema supports `require_vision: bool` and `GeminiAdapter`/`AnthropicAdapter` mark `vision=True`, `ollama` lists `qwen2.5vl:3b` on host per `P10_19_TEST_REPORT.md:4`.
- **Verified Gaps**: **Zero** screen capture (`mss`/PrintWindow), **zero** OS Accessibility/UI-tree parser, **zero** OCR reader, **zero** camera capture, and **zero** multimodal image-attachment chat/voice API routes in `api.py`/`api_v2.py` or `brain.py`!
- **Status**: **Missing** (schema capability flag only; end-to-end vision capture and routing are unimplemented).

### Area G — Model Routing, Failover & Provider Adaptation
- **Inspected Files**: `jarvis/gateway/gateway.py` (55–465), `jarvis/gateway/providers.py` (1–515), `jarvis/brain.py` (151–250), `jarvis/config.py` (1–360).
- **What Exists**: Rich multi-provider `ModelGateway` (`ollama`, `openai`, `anthropic`, `openrouter`, `gemini`, `huggingface`, `custom`, `opencode`), circuit breakers (`CLOSED`/`OPEN`/`HALF_OPEN`), `ResourceGovernor`, 24h token budget & RPM limits, `Laya` classifier, PII firewall, and privacy classification (`confidential`/`restricted` blocks cloud egress).
- **Verified Gaps**:
  - Ollama remains strict primary default in `stream_turn` (`_stream_llm_with_tts` line 560 skips `ModelGateway` completely when `active_prov == "ollama"`!).
  - Background workflows lack a `waiting_quota` pause-and-resume state when all eligible providers hit rate/quota limits.

### Area H — Runtime Self-Knowledge & Prompt Design
- **Inspected Files**: `jarvis/persona.py` (1–92), `jarvis/assistant.py` (175–310), `jarvis/api_v2.py` (209), `jarvis/sysinfo.py` (1–62).
- **Verified Gap**: While `/api/status` and `/api/v2/system/overview` expose rich telemetry to the React UI, `persona.py` uses static `PERSONA`/`ONLINE_PERSONA` strings for KV-cache reuse — runtime capabilities and limits (wake readiness, mic state, Local vs Online mode, Docker daemon status, PostgreSQL vs SQLite, granted filesystem folders, loaded agents/skills) are **not** summarized to the LLM system prompt dynamically (`Implemented, not integrated` / `Missing` in prompt injection).

### Area I — UI, Companion Access & Controls
- **Inspected Files**: `jarvis/api.py`, `jarvis/api_v2.py`, `jarvis/assistant.py` (`_match_ui_command`), `P10_20_E2E_TEST_REPORT.md` (with note of Chrome `DevToolsActivePort` block).
- **What Exists**: Consolidated 2-destination React 18/TS/Vite AI OS (`JARVIS ASSISTANT` + `SETTINGS`) with WebGL (`RING`, `CUBE`, `FACE`, `Knowledge Graph`), SSE `/api/events`, WebMic PCM WAV upload, inline filesystem permission cards, contextual drawers (`explorer`, `approvals`, `workforce`, `memory`, `system`, `voice`, `learning`, `tasks`), and single-screen voice UI commands.
- **Verified Gaps**: Localhost-only binding (`127.0.0.1:4700`); no authenticated remote phone companion server (QR pairing / encrypted LAN relay), and live Chrome DOM/WebGL remains blocked on `DevToolsActivePort`.

### Area J — Security, Privacy & Prompt-Injection Defense
- **Inspected Files**: `jarvis/audit.py` (1–196), `jarvis/netsec.py` (referenced via `search.py` & reports), `jarvis/security/pii_firewall.py` (1–61), `jarvis/security/secrets.py` (1–122), `jarvis/filesystem_access.py` (1–650), `jarvis/skills/security.py` (1–139), `jarvis/gateway/sandbox_proxy.py` (1–150), `jarvis/persona.py` (1–92).
- **Where Jarvis is Strongly Verified**: Kernel `sys.addaudithook` local-mode airgap, ContextVar scoped egress firewall, SSRF IP filtering, DPAPI secrets + AES-256-GCM DR export, `PIIFirewall` secret redaction, 9-op granular host filesystem scopes (`once`/`session`/`persistent` read-only), Docker sandbox proxy with scoped tokens, AST skill scanner, and prompt precedence tags inside `<jarvis_skill>` (`skills/security.py`).
- **Verified Gaps**:
  1. **Raw Web Search & Knowledge Snippets in `persona.py`**: `persona.py:user_message()` injects `Web search results:` and `Indexed documentation:` inline into the user turn **without** Mark-style unforgeable delimiters (`<<<UNTRUSTED>>>`) or tag-escape sanitization (`Implemented partially` — skills have delimiters, web results/KB do not).
  2. **In-Code Memory+Web Exfiltration Split**: Jarvis separates intents (`web_research` vs `memory_manage`), *but* `stream_turn()` still gathers personal/note context (`_gather_context`) *before* potentially invoking `web_search` / `fetch_webpage` tool calls in the same LLM context, enabling theoretical indirect prompt injection exfiltration via crafted `fetch_webpage` or query URLs if Online Assisted Mode is enabled!

