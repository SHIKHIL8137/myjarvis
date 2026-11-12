# P10.21 — Prioritized Implementation Roadmap

**Audit Date:** 2026-11-12  
**Target Codebase:** Jarvis (`d:\projects\myjarvis`)  
**Principle:** Incremental clean-room engineering extending existing Jarvis services (`AssistantTaskService`, `DBBackedCheckpointSaver`, `WebSearchService`, `FilesystemAccessManager`, `ModelGateway`, `VoiceLoop`, `Persona`). Zero Mark LVII source code copying; zero rewrites of working Jarvis subsystems.

---

## Priority Matrix (`P0` – `P3`)

| Priority | Work Package | Impact | Effort | Key Dependencies | Target Modules |
|---|---|---|---|---|---|
| **P0.1** | **Prompt-Injection Untrusted Data Delimiters + Tool Outcome Contract (`done`/`unverified`/`failed`)** | Very High (trust & safety) | Small (1–2 days) | None | `jarvis/security/untrusted.py` (new), `jarvis/persona.py`, `jarvis/assistant.py`, `jarvis/actions.py` |
| **P0.2** | **Persistent Checkpointed Background Task Runner & Boot Recovery** | Critical (reliability) | Medium (3–5 days) | Alembic rev `0004`, P0.1 | `jarvis/agents/tasks.py`, `jarvis/agents/orchestrator.py`, `jarvis/agents/task_runner.py` (new), `jarvis/db/models.py`, `jarvis/api_v2.py` |
| **P0.3** | **Multi-Stage Verified Deep Research Engine + Quote/Citation Checker** | Very High (core AI capability) | Medium (3–4 days) | P0.1, P0.2 | `jarvis/agents/research.py` (new), `jarvis/agents/assistant_graph.py`, `jarvis/search.py`, `jarvis/departments/assistant/documents.py` |
| **P1.1** | **Physical Voice/UI Live Hardware Verification (Chrome DevTools + Physical Room Mic/AEC/Wake)** | High (honest verification) | Small–Medium (1–2 days) | Host Chrome `--remote-debugging-port`, mic/speaker | Existing `tests/test_continuous_voice.py`, `voice.py`, `aec.py`, `apps/frontend` |
| **P1.2** | **DB-Backed Reversible Action Undo Journal (With Hash Conflict Detection)** | High (user safety & reversibility) | Medium (2–3 days) | P0.1, Alembic rev `0004` | `jarvis/undo.py` (new), `jarvis/filesystem_access.py`, `jarvis/actions.py`, `jarvis/api_v2.py` |
| **P1.3** | **Runtime Self-Knowledge Capability Block in System Prompt** | High (stops model hallucination) | Small (1 day) | P0.1 | `jarvis/persona.py`, `jarvis/assistant.py` |
| **P1.4** | **Unified Routines & Quiet Hours Engine (Consolidating Reminders/Watchers)** | High (assistant autonomy) | Medium (3–4 days) | P0.2, Alembic rev `0004` | `jarvis/departments/assistant/routines.py` (new), `reminders.py`, `jarvis/assistant.py`, `jarvis/api_v2.py` |
| **P2.1** | **Instant Voice Acknowledgment & `wake_active_timeout_s` Config + Silent-Sink Audio Probe** | Medium (voice UX polish) | Small (1–2 days) | P1.1 | `jarvis/assistant.py`, `jarvis/voice.py`, `jarvis/sysinfo.py`, `jarvis/config.py` |
| **P2.2** | **Read-Only Privacy-Gated Vision (`qwen2.5vl:3b` / Multimodal `Brain` + Upload/Explicit Screenshot)** | Medium–High | Medium–High (3–5 days) | P0.1, `ResourceGovernor` VRAM policy | `jarvis/vision.py` (new), `jarvis/brain.py`, `jarvis/gateway/gateway.py`, `jarvis/api_v2.py`, React drawer |
| **P2.3** | **Gateway Unification (`ModelGateway` on local path + Background `waiting_quota` Pause/Resume)** | Medium | Small–Medium (2 days) | P0.2 | `jarvis/assistant.py`, `jarvis/gateway/gateway.py` |
| **P3.1** | **Auto-Switching Local Piper Voice Map (EN / ML / HI)** | Low–Medium | Small (1–2 days) | Installed regional `.onnx` voices | `jarvis/tts.py`, `jarvis/config.py` |
| **P3.2** | **Non-Blocking 90s Interactive Quick-Confirm Voice/HUD Countdown** | Low–Medium | Small (1–2 days) | Existing approval/permission cards | `jarvis/agents/approvals.py`, React inline cards |
| **P3.3** | **Opt-In Authenticated Companion Mobile Access (strict TLS + PIN + explicit manual toggle)** | Low | Medium–High (4+ days) | Separate explicit security review | `jarvis/api.py`, settings UX |

---

## Recommended Phased Implementation Sequence

### Phase A — Trust, Verification & Background Persistence Foundation (`P0`)
1. Implement `jarvis/security/untrusted.py` + `ToolOutcome` contract and wrap all search/page/doc injections cleanly in `persona.py` & `assistant.py` (**P0.1**).
2. Migrate `AssistantTaskRow` (Alembic `0004`) and implement `BackgroundTaskRunner` with startup re-queue of non-approval-gated tasks, retry limit (`max_attempts=3`), step/time budget graceful wrap-up, cooperative shutdown, and `POST /api/v2/tasks/{task_id}/steer` (**P0.2**).
3. Replace memory-only `research` branch in `assistant_graph.py` with clean-room 5-stage `ResearchRunner` storing SHA1-keyed `pages/` snapshots, isolating memory tools from web tools, verifying quotes deterministically in Python, and flagging missing citations `[?]` (**P0.3**).

### Phase B — Hardware Truth, Reversibility, Honesty & Routines (`P1`)
4. Execute real hardware/UI verification on host (enable Chrome remote debug port -> capture live WebGL FPS & DOM interactions; run calibrated `NLMSEchoCanceller` + wake-word room test + BT disconnect verification) (**P1.1**).
5. Build DB-backed `UndoJournalRow` + hash-checked rollback + voice/UI `"undo"` drawer integration (**P1.2**).
6. Add compact KV-cache-friendly `RuntimeCapabilities` block to `persona.py` synced with `/api/v2/system/overview` (**P1.3**).
7. Build unified `RoutineEngine` + `quiet_hours` + grace windows integrating `ReminderService` and existing watchers (**P1.4**).

### Phase C — Voice Latency Polish & Gated Vision (`P2`)
8. Add optional fast voice acknowledgment before multi-step tool runs, configurable `wake_active_timeout_s`, and Windows silent-sink audio diagnostic probe (**P2.1**).
9. Add opt-in read-only image analysis (`qwen2.5vl:3b` local default + upload/explicit single-frame capture) under `ResourceGovernor` + privacy checks (**P2.2**).
10. Route local turns cleanly with unified `ModelGateway` quotas & `waiting_quota` task suspension (**P2.3**).

---

## Mandatory Test & Verification Requirements Per Milestone

- **P0.1 Tests:** Unit tests proving crafted `<<<END_UNTRUSTED_DATA>>>` injection strings inside webpage titles/bodies/snippets are escaped and cannot forge instruction boundaries; tests proving `unverified` outcomes prevent fake completion summaries.
- **P0.2 Tests:** Kill-and-restart simulation tests (create running task + checkpoint -> simulate shutdown -> restart `Assistant` / `BackgroundTaskRunner` -> confirm auto-resume from checkpoint without re-running completed steps, max attempt cap enforced, `awaiting_permission`/`awaiting_approval` NEVER auto-resumed without decision, `steer` note applied).
- **P0.3 Tests:** Offline + mocked search integration tests proving `PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY/REVISE`, verbatim vs missing quote detection in Python (`quote_support`), `[?]` injection for bogus citation numbers, stripped `recall_memory` inside web runner, and Local Mode staying strictly KB-only (`audit.py` verified).
- **P1.2 Tests:** Undo lifecycle tests for file edit, file create, move, directory-not-empty refusal, and SHA-256 mismatch conflict refusal after external edit.
- **P1.4 Tests:** Scheduler tests covering `quiet_hours` wrap-around midnight (`23:00-07:00`), missed-run grace window expiry vs catch-up, and explicit user-timed alarm bypassing quiet suppression.
- **P2.2 Tests:** Privacy & VRAM tests ensuring Local Mode refuses cloud vision, confidential project blocks cloud vision, and temp images are deleted post-inference.
- **Regression Bar:** Full existing `pytest`, `ruff`, `mypy`, `tsc`, `vite build`, `preflight.py`, and `drill.py` must continue to pass with zero regressions and honest disclosure of any host skips.

