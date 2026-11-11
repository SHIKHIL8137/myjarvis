# Documentation analysis

Both PDFs in the project root were read completely (text extracted with pypdf; 18 of 18 pages each, no empty pages).

| | Build-Jarvis-Fully-Offline (Nov 2026) | Run-Jarvis-Fully-Local (Oct 2026) |
|---|---|---|
| Structure | Part 1 hardware guide, Part 2 three installs, Part 3 **six** prompts | Part 1 three installs, Part 2 **eight** prompts, "seven things I learned" |
| Platform | Mac and Windows (Windows voice engine: VoxCPM2 on NVIDIA) | Mac first; voice engine is Apple-only |
| Brain | Ollama, `qwen3.5:9b` at 16 GB; "local" + "big" + "claude" shelf | Ollama, `qwen3.5:9b`; "local" + "claude" shelf; `go local` / `go cloud` |
| Ears | faster-whisper `large-v3-turbo` + `small` for the wake word | mlx-whisper large-v3-turbo + small |
| Proof | Prompt 06: switch + vendored libs + `drill.py` | Prompt 07: live `preflight.py` with a latency budget; Prompt 08: watcher, `/drill/connections`, drill |

They overlap on prompts 01–05 almost word for word. Differences: only Build has the hardware guide, the "big" brain and Windows voice
instructions; only Run has the live preflight with a latency budget, the `/drill/connections` endpoint, and the lessons list.
The newer document (Build) wins where they conflict, except that Run's preflight and drill endpoint are kept because Build lacks them.

## Requirements and where they landed

| Requirement (source) | Implementation | Status |
|---|---|---|
| Indexer: scan `.md`, node id = index, folder = group, ~700-char excerpt, links by title mention / shared wikilinks (01) | `jarvis/notes.py` `NoteStore.graph()` | done, live instead of a generated `graph-data.js` |
| 3D galaxy: starfield, glow by group, idle rotation, click flies + lights neighbours + side panel (01) | `viewer/` with vendored 3d-force-graph | done |
| Server on 127.0.0.1:4700 serving only `viewer/`; `config.json` in root, unreachable from the browser (01, 07) | `jarvis/api.py`, `server.py` | done, verified by preflight |
| `/chat`: keyword scoring, title weighted, top notes, answer + `nodes`, short history (01) | `retrieval.py`, `assistant.py`, `history.py` | done (top 3 x 450 chars, measured) |
| Stable system prompt first, variable parts last, < 400 words; no silent thinking (01, 04) | `persona.py` (207 words) | done |
| Voice: chunk ~170 chars, never split a sentence, prefetch next chunk, disk cache, stop (02) | `tts.py` | done with Piper |
| Voice routes checked before the brain (02) | `commands.py` | done |
| Ears: Whisper local, loaded once and warmed, `/hear` takes WAV (03) | `stt.py`, `POST /api/voice/hear` | done, `small.en` on CPU |
| Wake word "Jarvis", silence end-pointing, interrupt while speaking, status line (03) | `wakeword.py`, `voice.py`, orb in UI | done with openWakeWord "Hey Jarvis" |
| Butler persona < 300 words, receipts not claims, greeting, real note count on screen (04) | `persona.py`, `commands.py`, `app.js` | done |
| "Remember that…" writes markdown into `captures/`, indexed immediately, live node birth + fly, ONE door (05) | `NoteStore.add`, `refreshGraph`/`birth` | done |
| Answer proves itself: fly to top source, cluster for 4+, small talk never moves the camera (05) | `focusNodes` | done |
| Brain shelf, spoken switch, `boot_brain`, runtime-only swaps (06) | `brain.py`, `config.json` | done for local models |
| Live preflight with latency budget, exit non-zero on failure (Run 07) | `preflight.py` | done |
| Libraries vendored, `VERSIONS.txt`, watcher, drill, honest list (06 / Run 08) | `viewer/vendor/`, `audit.py`, `drill.py` | done |

## Deliberate deviations (hardware and the offline rule)

1. **Brain:** `qwen3:4b-instruct`, not `qwen3.5:9b`. A 9B model (~6 GB) cannot fit 4 GB of VRAM. `reasoning_effort: none` did nothing on this
   machine; the installed `qwen3:4b` is a thinking-only build, so it is kept as an optional brain and its reasoning is discarded.
2. **Native Ollama API**, not the OpenAI-compatible one: it is the only way to set `num_ctx` (the model default of 262k overflowed VRAM).
3. **Voice:** Piper instead of VoiceStudio. VoiceStudio (github.com/debpalash/VoiceStudio, AGPL-3.0) is a separate desktop app whose
   engines would compete for the same 4 GB card, and its installer is a remote script; it was not installed. Cost: no voice cloning.
4. **Wake word:** openWakeWord's pretrained "hey_jarvis" model instead of running Whisper-small on every utterance (about 2 ms per 80 ms frame).
5. **Audio runs in the backend (sounddevice)**, not in the browser. So there is no `?mute=1` tab flag; the "Speak replies" checkbox and
   the Stop button do that job, and no browser microphone permission is needed.
6. **No cloud brain.** The packs ship a "claude" entry; the brief for this build forbids any cloud fallback, so it does not exist and
   the server blocks every non-loopback connection.
7. **No control tags.** Actions are matched by deterministic routes before the model sees the text; a 4B model is not trusted to emit tags.
8. **No embeddings.** Keyword retrieval is instant at this size; nothing measured justified another model in memory.
