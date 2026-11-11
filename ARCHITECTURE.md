# Architecture

One Python process (FastAPI + uvicorn, bound to 127.0.0.1:4700) serves the UI and runs everything except the language model,
which stays in Ollama on localhost:11434.

```
browser (viewer/)  --HTTP/SSE-->  jarvis/api.py  -->  Assistant (assistant.py)
                                                        |-- commands.py   routes checked before the model (remember, switch, stop)
                                                        |-- notes.py      markdown store + graph      retrieval.py  keyword search
                                                        |-- persona.py    the one system prompt       history.py    data/history.jsonl
                                                        |-- brain.py      Ollama client + model manager  --> localhost:11434
                                                        |-- tts.py        Piper + Speaker (sounddevice out)
                                                        |-- stt.py        faster-whisper (CPU)
                                                        |-- wakeword.py   openWakeWord       voice.py  mic loop (sounddevice in)
                                                        |-- audit.py      connection log + offline enforcement
                                                        `-- sysinfo.py    RAM / GPU
```

| Path | Role |
|---|---|
| `server.py` | Entry point; sets offline environment flags and starts uvicorn |
| `config.json` | Committed defaults. `data/settings.json` holds machine-local overrides written by the Settings tab |
| `jarvis/config.py` | Pydantic-validated settings, logging to `logs/jarvis.log` |
| `viewer/` | `index.html`, `style.css`, `js/*.js` (ES modules), `vendor/` (3d-force-graph, Orbitron font). No build step, no CDN |
| `notes/` | Your markdown. Folder = category. `captures/` = things Jarvis was told to remember. `.trash/` = deleted notes |
| `models/` | Whisper, Piper and wake-word files (not in git) |
| `data/` | History, settings overrides, TTS cache (not in git) |
| `preflight.py`, `drill.py`, `bench_llm.py` | Live verification, network drill, model benchmark |
| `tests/` | pytest suite |

## Frontend modules (`viewer/js/`)

| File | Role |
|---|---|
| `api.js` | The only place that calls the backend: `api()`, `chatStream()`, `listen()` (server-sent events), plus DOM helpers and a small event hub |
| `galaxy.js` | The 3D graph: data from `/api/graph`, selection, two-step explore, search, category filter, forces, labels, camera |
| `main.js` | Left rail, reactor HUD and modes, command bar, conversation, live events |
| `panels.js` | Drawers: knowledge explorer (notes CRUD) and system (settings + monitor) |
| `md.js` | Markdown renderer for chat and notes. Escapes HTML first; links are never navigable |

Rendering is kept light for the GTX 1650: pixel ratio capped at 1.5, no particles, labels for at most 40 nodes, lower sphere
detail above 400 notes, rendering paused when the tab is hidden, and the monitor polls only while its drawer is open.

## A turn

1. Text arrives from the chat box (`POST /api/chat/stream`) or from the voice loop (wake word → record until silence → Whisper).
2. `commands.route` checks for an action. If it matches, the action runs and the reply is its receipt.
3. Otherwise the top 3 notes scoring above a threshold are added to the final user message; small talk gets none.
4. `brain.stream` calls Ollama's native `/api/chat` with `num_ctx` 4096. Reasoning (`thinking` field or `<think>` tags) is dropped.
5. Tokens stream to the UI. If speech is on, whole sentences are queued to the Speaker, which synthesises the next chunk while one plays.
6. The exchange is appended to history; the UI flies the galaxy camera to the source note.

All turns go through `Assistant.stream_turn`, and all new notes through `NoteStore.add`.

## API (all under `/api`, loopback only)

| Method and path | Purpose |
|---|---|
| GET `/health`, GET `/status` | Liveness; full status with CPU, RAM, GPU, VRAM, audio devices, voice state |
| POST `/chat`, POST `/chat/stream` | One turn as JSON, or as server-sent events |
| GET/DELETE `/history` | Conversation history |
| GET `/models`, POST `/models/switch` | List and switch local models |
| POST `/voice/hear` | WAV body → text |
| POST `/tts`, POST `/speak`, POST `/speak/stop` | WAV out; speak on the laptop; stop speaking |
| POST `/voice/mic`, POST `/voice/ptt`, GET `/audio/devices` | Microphone on/off, push to talk, device list |
| POST `/memory`, GET `/memory/search` | Create a memory, search notes |
| GET `/notes`, GET/PUT/DELETE `/notes/{id}` | List, read, edit, move to trash |
| GET `/graph` | Nodes and links for the galaxy |
| GET/PUT `/config` | Read settings (including installed voices); change tts, wake, audio, history_turns, boot_brain, num_ctx |
| GET `/events` | Server-sent events: voice state, microphone level while recording, voice-initiated turns, graph changes |
| GET `/drill/connections` | Connection log used by the drill |

## Security

- Binds to 127.0.0.1 only; settings reject any non-loopback host.
- Requests with a non-loopback `Host` or foreign `Origin` get 403 (DNS rebinding, cross-site).
- Every POST/PUT/DELETE needs the `X-Jarvis: 1` header, which a cross-site form or simple request cannot send.
- Only `viewer/` is served; `config.json`, source, notes and data return 404.
- Content-Security-Policy restricts the page to its own origin. `unsafe-eval` is allowed because the vendored 3D library needs it.
- Brain definitions cannot be changed over the API. Deleting a note only moves it to `notes/.trash/`.
- No authentication: this is a single-user loopback app. Anyone logged in to this Windows account can use it.
