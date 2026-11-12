# JARVIS P10.18 — Voice Integration & Unified Routing Report

## 1. Shared Voice & Text Execution Pipeline
Both spoken inputs and typed commands flow into the single `Assistant.stream_turn()` execution path (`jarvis/assistant.py`):
- **Host Continuous / PTT / Wake Microphone (`jarvis/voice.py`)**: `VoiceLoop` transitions map cleanly to canonical OS states (`LISTENING`/`WAKE_WORD` -> `Listening`, `SPEECH_DETECTED` -> `Speech detected`, `PROCESSING` -> `Transcribing`, `GENERATING` -> `Thinking`, `SPEAKING` -> `Speaking`, `IDLE`/`PAUSED` -> `Idle`, `ERROR` -> `Error`) and publish `assistant_state` inside `voice_state` events. Captured audio queues into `_run_voice_utterance()` -> Whisper STT -> `Translator.process_stt()` -> `assistant.transcript.final` -> `stream_turn(..., source="voice")`.
- **Browser Microphone Command Endpoint (`POST /api/voice/command`)**: Accepts 16 kHz PCM WAV uploads (up to 10 MB) recorded directly from the UI `VoiceDock` (`Speak` toggle via WebAudio `AudioContext` -> 16-bit mono WAV encoding), runs STT + translation/correction, emits `assistant.transcript.final`, and executes `stream_turn(..., source="voice")`.
- **Bottom Command Dock (`VoiceDock.tsx`)**: Text prompts route through `POST /api/chat/stream` with the same `stream_turn()` execution, TTS voice toggle (`speak`), real SSE `level` / WebAudio RMS waveform, and shared `Interrupt` (`POST /api/voice/interrupt` + `POST /api/speak/stop` + stream abort).

## 2. Decision Engine Integration & Safety Gates
Inside `Assistant.stream_turn()`:
1. Fast deterministic `commands.route()` handles instant local commands (`/help`, note/memory quick actions, model/voice switches) with `Understanding` -> `Executing` -> `Completed`.
2. Non-command turns invoke `await self.orchestrator.decision.decide(text)`, emitting `Planning` and publishing `decision.routed` telemetry + `meta.decision` summary (`department`, `intent`, `complexity`, `workflow`, `agent_role`, `requires_owner_approval`).
3. **Owner Approval Enforcement**: When `plan.decision.requires_owner_approval` is `True`, execution pauses safely: an `ApprovalRequestRow` is persisted via `orchestrator.approvals.create_request()`, `assistant.state.changed` emits `Waiting for approval`, and a receipt with the approval token is returned for review in Workforce/Approvals.
4. **Actionable Workflow Delegation**: Explicit `document_export` and `reminder_manage` commands transition through `Delegating` -> `Executing` cleanly via `orchestrator.start_assistant_workflow()`.
5. **Conversational / Research / Coding RAG**: Runs `Thinking` (`_gather_context` + `conversation_used` highlighting notes/memories/knowledge), transitions to `Executing` (`tool_used`) during `web_search`/`fetch_webpage` calls, `Speaking` while `Speaker` plays TTS chunks, and `Completed` -> `Idle` while preserving the `/api/chat/stream` SSE order (`meta -> token... -> done`).

## 3. Verified Live Audio Loop (`preflight.py`)
- Piper TTS `/api/tts`: `258,092 bytes` WAV generated in **0.38 s** (`en_GB-alan-medium`).
- Whisper STT `/api/voice/hear` roundtrip: transcribed `"Good afternoon, so the local voice is live at 26,447."` (`7/8 words` matched in **1.77 s**).
- Host microphone lifecycle (`/api/voice/mic`): transitioned cleanly to `listening` (`error=None`) and released cleanly on disable.

