# Jarvis (local/offline) — plan

## Machine (inspected 2026-11-11)
| Item | Found | Consequence |
|---|---|---|
| GPU | GTX 1650 (non-Ti), 4 GB VRAM, ~0.5 GB used by Windows | LLM gets the GPU alone; ~3.4 GB usable |
| CPU | Ryzen 5 5600H, 6c/12t | Whisper + Piper run here (CPU, int8) |
| RAM | 15.3 GB total, only ~4 GB free at inspection | Close apps while testing; no big CPU-offloaded models |
| Disk | D: 43.8 GB free | Enough for ~5 models + voices |
| Ollama | 0.40.16, running on :11434 | Models present: `qwen3:4b` (2.5 GB), `qwen2.5vl:3b` (3.2 GB) |
| Python | 3.14.6 (only one) | Wheels exist for faster-whisper, ctranslate2, onnxruntime, openwakeword, piper-tts, sounddevice, numpy |
| Other | Node 24, git, ffmpeg 9.0.1 present; cmake missing | No C++ builds — wheels only |
| Mic | Realtek HD Audio present | Echo risk with speakers -> headphones recommended |

## Models
- **LLM (default): `qwen3:4b`** — already pulled, fits fully in VRAM, supports tools. Must run with thinking off (silent thinking eats the token budget).
- Try if you want a different feel: `gemma3:4b`, `llama3.2:3b`, `phi4-mini` (verify tags on ollama.com/library before pulling).
- Not recommended: the packs' `qwen3.5:9b` (~6 GB) — does not fit 4 GB VRAM; CPU split would be ~3-6 tok/s.
- **STT:** faster-whisper on CPU, int8. `small.en` for commands, `tiny.en`/`base.en` for wake gating. Benchmark in phase 4; the pack's `large-v3-turbo` is likely too slow on CPU.
- **TTS:** Piper (CPU, ONNX, fully offline). The packs' VoiceStudio/VoxCPM2 needs a GPU the LLM already occupies.
- **Embeddings (optional, later):** `nomic-embed-text` (~270 MB). Start with keyword scoring as the pack does.

## Deviations from the prompt packs
1. Smaller brain (above) and CPU-side speech, because of the 4 GB VRAM budget.
2. Piper instead of VoiceStudio voice cloning.
3. 3D libs vendored locally from phase 1 (pack waits until its prompt 06) so offline is true from day one.
4. Keep pack ideas verbatim: stable system-prompt prefix, `?mute=1` flag, finish window, receipts-not-claims, drill.py.

## Structure
```
myjarvis/
  config.json            # brains, voice, hearing, boot_brain (never inside viewer/)
  server.py              # entry: routes only
  jarvis/
    brain.py             # Ollama OpenAI-compatible client, retry, history
    retrieval.py         # note scoring / (later) embeddings
    notes.py             # ONE door: write md, index, graph node
    indexer.py           # scan notes -> viewer/graph-data.js
    tts.py  stt.py       # Piper / faster-whisper, soft-imported
    routes.py            # voice routes list (switch brain, voice, etc.)
  viewer/                # index.html, app.js, vendor/ (pinned, local)
  notes/  captures/
  tests/
  drill.py               # lists every non-localhost connection
```
Server: FastAPI + uvicorn if their wheels install on 3.14 (checked in phase 0); otherwise stdlib `http.server`. Frontend: plain ES modules, no build step.

## Phases (each ends with tests + your approval)
0. **Scaffold + preflight** — venv, deps, `config.json`, `preflight.py` (Ollama up, model present, VRAM, mic). Test: preflight passes.
1. **Notes + 3D graph** — indexer, vendored 3d-force-graph, click-to-fly + side panel. Test: indexer unit test; page renders with network disabled.
2. **Brain** — `/chat` with retrieval, persona (<300 words), stable prefix. Test: question about a note answers correctly; print seconds and token/s.
3. **Voice out** — Piper `/tts`, sentence-chunked playback, cache, mute flag. Test: first-audio latency.
4. **Ears** — `/hear` with faster-whisper. Test: WAV transcription + latency.
5. **Hands-free loop** — browser mic VAD, wake word "Jarvis", finish window, interrupt. Wake word: Whisper-tiny gating (no extra dep) vs openWakeWord `hey_jarvis` (lower CPU) — decide with measurements.
6. **Memory** — "remember that…" -> markdown file -> live node in graph, searchable next turn.
7. **Switch + offline drill** — brain shelf, badge, `drill.py` must print an empty host list.

Risks: low free RAM, CPU Whisper latency, speaker echo into the mic, Python 3.14 wheel gaps (checked only via download, not import).
