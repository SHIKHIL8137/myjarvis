# Architecture

One Python process (FastAPI + uvicorn, bound to `127.0.0.1:4700`) serves the UI and runs everything except the language model,
which stays in Ollama on `127.0.0.1:11434`.

```
browser (viewer/)  --HTTP/SSE-->  jarvis/api.py  -->  Assistant (assistant.py)
                                                        |-- intent.py / commands.py  centralized router (voice/text/actions/learning)
                                                        |-- memory_store.py  6-category personal memory + duplicate/secret/backup
                                                        |-- filesystem.py   allowlisted file/codebase search, bounded read, soft .trash
                                                        |-- actions.py      permission tiers (READ..SYSTEM), confirmations, TaskManager
                                                        |-- notes.py / retrieval.py  markdown store + 3D graph + keyword retrieval
                                                        |-- persona.py      stable system prompts + provenance-separated context sections
                                                        |-- history.py      data/history.jsonl + unbounded on-disk archival search
                                                        |-- brain.py        Ollama native + tool calling + gated OpenAI/Anthropic adapter
                                                        |-- translator.py   transcript preservation + Whisper/Qwen translation
                                                        |-- stt.py / tts.py / aec.py / wakeword.py / voice.py  speech & audio pipeline
                                                        |-- learning/       knowledge_pipeline.py, feedback.py, evaluation.py
                                                        |-- learning/training/  dataset pipeline, hardware estimator, PEFT worker, registry, rollback
                                                        |-- search.py / netsec.py / audit.py  loopback guard & scoped HTTPS gateway
                                                        `-- sysinfo.py      CPU / RAM / GPU / audio
```

| Path | Role |
|---|---|
| `server.py` | Entry point; sets HuggingFace offline/telemetry flags and starts uvicorn |
| `config.json` | Committed defaults (`internet.enabled: false` by default). `data/settings.json` holds local overrides; `data/secrets.json` holds optional API keys (gitignored) |
| `jarvis/config.py` | Pydantic-validated settings (`STTSpec`, `TranslationSpec`, `AudioSpec`, `InternetSpec`) and secret helpers |
| `viewer/` | `index.html`, `style.css`, `js/*.js` (ES modules), `vendor/` (3d-force-graph, Orbitron font). No build step, no CDN |
| `notes/` | Markdown notes. Folder = category. `captures/` = remembered items. `.trash/` = deleted notes |
| `models/` | Whisper (`base`, `small`, `small.en`), Piper, and openWakeWord models (not in git) |
| `data/` | History, settings overrides, secrets, TTS cache (not in git) |
| `preflight.py`, `drill.py`, `bench_llm.py`, `bench_stt.py` | Live verification, network drill, LLM and STT CPU benchmarks |
| `tests/` | pytest suite |

## Frontend modules (`viewer/js/`)

| File | Role |
|---|---|
| `api.js` | Single backend client (`api()`, `chatStream()`, `listen()`), DOM helpers, and event bus |
| `galaxy.js` | 3D knowledge graph: selection, two-step explore, search, category filter, forces, labels, camera |
| `main.js` | Left rail, reactor HUD & mode toggles (including `INTERNET`), command bar, conversation, source links, transcript correction/translation card |
| `panels.js` | Drawers: knowledge explorer (notes CRUD) and System (settings for brain, voice, speech/translation, internet/provider, live monitor) |
| `md.js` | Safe Markdown renderer (HTML-escaped first; `[[wikilinks]]` become buttons; only validated `https://` links render as external anchors) |

## Speech, Streaming TTS & Voice State Machine

1. **Voice Interaction Modes & State Machine (`voice.py`)**: Supports **Continuous Conversation** (default, auto-resumes listening after each reply), **Wake-Word Mode** ("Hey Jarvis" + configurable `wake_followup_s` window), and **Push-to-Talk**. Explicit synchronized [`VoiceState`](file:///d:/projects/myjarvis/jarvis/voice.py) transitions (`IDLE`, `WAKE_WORD`, `LISTENING`, `SPEECH_DETECTED`, `PROCESSING`, `GENERATING`, `SPEAKING`, `INTERRUPTED`, `RESUMING`, `PAUSED`, `ERROR`) prevent duplicate utterances (`utt_id`), concurrent TTS sessions, and accidental restarts after Stop.
2. **Echo Prevention & Duplex Modes (`aec.py`, `voice.py`)**:
   - **Safe Half-Duplex Mode (default, `duplex_mode: "half_duplex"`)**: Releases the hardware `InputStream` immediately upon end-of-speech, waits through `PROCESSING -> GENERATING -> SPEAKING` until `Speaker.wait_idle()` completes, pauses for `mic_resume_delay_ms` (`300 ms`), and re-opens the microphone in `RESUMING -> LISTENING`. Prevents Bluetooth/speaker audio from re-entering Whisper.
   - **Full-Duplex Mode (`duplex_mode: "full_duplex"`)**: Uses [`NLMSEchoCanceller`](file:///d:/projects/myjarvis/jarvis/aec.py) with far-end [`EchoReferenceBuffer`](file:///d:/projects/myjarvis/jarvis/aec.py) for barge-in **only when `aec.verified == True`**; automatically falls back to Safe Half-Duplex if WebRTC/SpeexDSP/NLMS AEC is unverified.
3. **Audio capture (`voice.py`) & STT (`stt.py`)**: Opens the configured input device (16 kHz with native-rate polyphase resampling via `scipy.signal.resample_poly`, plus PortAudio table refresh on BT reconnect). Uses streaming Silero VAD + adaptive RMS gating, `speech_confirm_ms` (`180 ms`), `pre_roll_ms` (`240 ms`), and multilingual `faster-whisper` (`base` default or `small`/`small.en`).
4. **Incremental Streaming LLM → Piper TTS (`tts.py`, `assistant.py`)**: [`StreamingSpeechBuffer`](file:///d:/projects/myjarvis/jarvis/tts.py) incrementally parses streaming Ollama tokens into complete English/Malayalam/Indic sentences (preserving decimals/abbreviations, skipping code blocks), queueing them via bounded queues (`_text_q` maxsize 32, `_audio_q` maxsize 3) so Piper synthesizes and plays early sentences while Qwen continues generating, flushing any final clause at end of stream, and emitting `SPEAKING` only when `sd.play` begins.
5. **Separated translation (`translator.py`)**: Preserves `original_transcript`, `corrected_transcript`, `detected_language`, `translated_text`, and `target_language` in `TranscriptStore`. Never translates silently when `translation.mode == "transcribe_only"`.

## Conversation Turn & Modes

- **Local Mode (default, `internet.enabled: false`)**: All outbound non-loopback connections are blocked by `jarvis/audit.py`. Turns run `commands.route` → local note retrieval → `brain.stream` (`PERSONA`).
- **Online Assisted Mode (opt-in, `internet.enabled: true`)**: Keeps the local `qwen3:4b-instruct` model and local notes. `Assistant.stream_turn` offers strictly whitelisted backend tools (`web_search`, and `fetch_webpage` if `allow_page_fetch` is enabled) to Ollama `/api/chat`, falling back deterministically via `needs_web_intent()` if a model lacks or skips tool calling. Answers cite exact `https://` source URLs in `web_sources`.

## API (all under `/api`, loopback only)

| Method and path | Purpose |
|---|---|
| `GET /health`, `GET /status` | Liveness and full status (Ollama, brain, voice, STT, translation, internet/search, system metrics) |
| `POST /chat`, `POST /chat/stream` | Execute one turn as JSON or SSE (includes `web_sources` and tool status events when online) |
| `GET/DELETE /history` | Conversation history |
| `GET /models`, `POST /models/switch` | List and switch local Ollama models |
| `POST /voice/hear`, `/voice/correct`, `/voice/translate`, `GET /voice/transcripts` | Transcribe WAV with metadata, update `corrected_transcript` while preserving `original_transcript`, translate, list recent records |
| `POST /tts`, `POST /speak`, `POST /speak/stop` | Piper WAV synthesis and playback control |
| `POST /voice/mic`, `POST /voice/session`, `POST /voice/interrupt`, `POST /voice/ptt`, `GET /audio/devices` | Voice session lifecycle (`start`, `pause`, `resume`, `stop`, `interrupt`, `mode`), interruption, PTT, and MME device list |
| `GET /internet`, `POST /internet/mode`, `POST/DELETE /internet/key`, `POST /internet/search` | Mode indicator/toggle, write-only provider API key storage, direct search, external audit log |
| `POST /memory`, `GET /memory/search`, `GET/PUT/DELETE /notes*`, `GET /graph` | Notes CRUD and 3D graph |
| `GET/PUT /config` | Safe settings read/write (excludes secrets and brain definitions) |
| `GET /events`, `GET /drill/connections` | Live SSE stream and connection audit log |
| `GET/POST/PUT/DELETE /memory/items*`, `/memory/backup`, `/memory/export`, `/memory/restore`, `GET /history/search` | Structured 6-category personal memory & full on-disk conversation search |
| `GET /files/roots`, `/files/search`, `/files/read`, `/files/inspect`, `POST /files/write` | Allowlisted local filesystem search, project inspection, and gated writes |
| `GET /actions/pending`, `POST /actions/execute`, `/actions/{token}/confirm|reject`, `GET /tasks*` | Permission-tiered actions (`READ`..`SYSTEM`) & tracked `TaskManager` lifecycle |
| `GET /voices`, `POST /voices/switch`, `POST /voices/rate` | Runtime Piper voice discovery, hot-switching, and speed control |
| `GET /learning/overview`, `/learning/knowledge/*`, `/learning/feedback/*`, `/learning/datasets/*`, `/learning/training/*`, `/learning/evaluate`, `/learning/registry/*` | Knowledge updates (RAG index + rollback), feedback curation, validated dataset pipeline, approval-gated LoRA worker, 12-category evaluation, model registry & rollback |

## Security

- Binds to `127.0.0.1` only; foreign `Host`/`Origin` requests receive 403; mutating requests require `X-Jarvis: 1`.
- Browser CSP (`connect-src 'self'`) prevents direct browser-to-internet requests; external requests originate solely from `jarvis/netsec.py` in Online Assisted Mode.
- `jarvis/audit.py` remains enforced even in Online Assisted Mode: arbitrary sockets still raise `ConnectionError`; only scoped `netsec.safe_request` contexts are permitted.
- `jarvis/netsec.py` enforces `https://` port 443 only, host allowlists for search providers, SSRF/private/CGNAT/link-local IP blocking, single-resolution IP pinning (anti-DNS-rebinding), manual redirect hop re-validation, `trust_env=False`, timeouts, and download byte limits.
- API keys live only in environment variables or gitignored `data/secrets.json`, are redacted in logs, and are never returned over HTTP.
