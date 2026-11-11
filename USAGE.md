# Usage

```powershell
.\start-jarvis.ps1      # start; opens http://127.0.0.1:4700/
.\stop-jarvis.ps1       # stop
```

## The screen

| Area | What it is |
|---|---|
| Centre | The 3D knowledge galaxy: every note is a star, coloured by folder, linked by real `[[wikilinks]]` and title mentions |
| Left rail | Search, inspector for the selected note, top hubs, category filter, statistics, force sliders, display options, and `LOCAL MODE` / `ONLINE` status |
| Top bar | **Fit**, **Reset** (camera), and the **Notes**, **Chat** and **System** drawers |
| Right HUD | Jarvis reactor, status chips (`OLLAMA OK`, `LOCAL` / `ONLINE ASSISTED`, active model), live STT config, and five mode buttons |
| Bottom | The command bar (stays available whatever else is open) |

## Talking & Multilingual Speech

| You want to | Do this |
|---|---|
| Choose voice mode | Use the selector in the command bar: **Continuous** (default hands-free loop), **Wake Word** (`"Hey Jarvis"` + follow-up window), or **Push-to-Talk** (single turn) |
| Start / Stop continuous listening | Click the microphone button (**🎙**) once; speak naturally and pause — Jarvis responds via streaming Piper speech and automatically resumes `LISTENING` after playback finishes |
| Pause / Resume temporarily | Click **⏸** (Pause) to mute listening and stop playback, or **▶** to resume |
| Ask once manually | Click **●** (Push-to-Talk, captures a pre-roll buffer so word onsets aren't clipped) |
| Silence or interrupt a reply | Click **■** to interrupt/stop generation and Piper playback immediately (barge-in by voice is disabled in Safe Half-Duplex mode) |
| Type a command | Use the command bar (press `/` to jump to it) |

### Audio Echo Prevention (Safe Half-Duplex vs Full-Duplex)

Open **System → Voice & Conversation** to configure:
- **Audio duplex**: **Safe Half-Duplex** (default) suspends and releases the microphone while Jarvis thinks and speaks over Bluetooth/laptop speakers, preventing Jarvis from transcribing his own output, then waits **Mic resume delay (`300 ms`)** before reopening the mic. **Full-Duplex** enables voice barge-in only when acoustic echo cancellation (AEC) has been verified on the hardware; otherwise Jarvis automatically falls back to Safe Half-Duplex and displays the fallback notice in the command bar and System Monitor.

### Selecting transcription languages and quality

1. Open **System → Speech & Translation**.
2. Choose **Whisper model**:
   - `base` (default): ~1.2 s latency on Ryzen 5 5600H CPU, +139 MB RAM, supports multilingual auto-detect.
   - `small`: ~3.6 s latency, +319 MB RAM, higher accuracy for Malayalam/Hindi/compound terms.
   - `small.en`: English-only legacy model.
3. Choose **Input language**: `Auto-detect (auto)`, `English (en)`, `Malayalam (ml)`, `Hindi (hi)`, Tamil, Telugu, Kannada, etc.
4. Choose **Quality / speed**: `Fast (beam 1)`, `Balanced (beam 3)`, or `Accurate (beam 5)`.
5. Optionally add **Vocabulary hints** (comma-separated terms, names, or technical words such as `Jarvis, Ollama, FastAPI, VRAM`).
6. Click **Save**.

### Translating voice commands & correcting transcripts

Transcription and translation are strictly separated — Jarvis **never translates silently** when **Speech action** is set to `Transcribe only`:

1. To translate incoming speech automatically, set **Speech action** to `Transcribe + translate` and select a **Translate target** (e.g. `English (en)`, `Malayalam (ml)`, `Hindi (hi)`), then click **Save**.
2. Open **Chat** after speaking to see the **Transcript & Translation card**:
   - Displays **detected language + probability**, **confidence score**, and **Original** transcript (always preserved immutably).
   - Edit the **Corrected** field and click **Apply** to refine transcription errors without overwriting the original transcript.
   - Pick any target language in the dropdown and click **Translate** to translate on demand using local `qwen3:4b-instruct` (or Whisper's built-in speech-to-English translator when translating non-English audio to English).

## Enabling and Disabling Internet Access (Local vs Online Assisted Mode)

Jarvis starts in **Local Mode** (`internet.enabled: false`), blocking all external connections.

### Enable Online Assisted Mode
1. Click the **INTERNET** button on the right HUD (switches from `off` to `on`), **or** open **System → Internet & Web Search**, check **Online Assisted Mode (web search)**, and click **Save**.
2. Configure your search provider (`DuckDuckGo`, `Tavily`, `Brave`, `Serper`, or HTTPS `SearXNG`), optional **Allow webpage content retrieval**, and write-only **Provider API key** if required.
3. Ask questions needing live data (e.g. *"What is the latest news on Python 3.14?"* or *"Search the web for..."*). Your local `qwen3:4b-instruct` model invokes `web_search` (or deterministic intent routing falls back automatically) and displays clickable `https://` source links under the answer.

### Return to strict Local Mode
- Click **INTERNET** on the HUD again (`on` → `off`), or uncheck **Online Assisted Mode** in System settings and click **Save**. The HUD badge returns to **LOCAL**, and `jarvis/audit.py` blocks all outbound connections immediately.

## Commands handled directly (typed or spoken)

| Say or type | Result |
|---|---|
| "Remember that …", "Note to self …", "Don't let me forget …", "Log this …" | Saves a markdown note in `notes/captures/`; a new star appears |
| "Show my notes" | Opens the knowledge explorer |
| "Find information about …", "Search my knowledge for …" | Searches local notes and highlights them in the galaxy |
| "Search the web for …" | Runs approved web search in Online Assisted Mode, or replies honestly that Local Mode is active |
| "Switch to the thinking model" / "… the fast model" | Switches local Ollama model |
| "Which model are you using?", "How many notes do you have?" | Answered directly from local system state |
| "Stop" | Stops speaking |
