# Installation (Windows)

This machine is already set up. These steps rebuild it from scratch. Steps 2–5 need the internet once during setup; Local Mode needs no internet afterward.

Requirements: Windows 10/11, Python 3.14, Ollama 0.40+, ~5 GB of free disk, a microphone and headphones.

```powershell
cd D:\projects\myjarvis

# 1. Virtual environment and pinned packages
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.lock

# 2. Language models (Ollama must be running). The second one is optional.
ollama pull qwen3:4b-instruct
ollama pull qwen3:4b

# 3. Piper voice
.\.venv\Scripts\python.exe -m piper.download_voices en_GB-alan-medium --data-dir models\piper

# 4. Multilingual Whisper models (base is default; small improves compound & Indic accuracy)
.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='models/whisper'); WhisperModel('small', device='cpu', compute_type='int8', download_root='models/whisper')"

# 5. Wake-word models
.\.venv\Scripts\python.exe -c "from openwakeword import utils; utils.download_models(model_names=['hey_jarvis'], target_directory='models/wake')"

# 6. Start and verify
.\start-jarvis.ps1
.\.venv\Scripts\python.exe preflight.py
```

If PowerShell refuses to run the scripts: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## What needs the internet, and when

| Component | Source | Size |
|---|---|---|
| `pip install` | PyPI | ~540 MB installed (includes `scipy` for polyphase resampling) |
| `ollama pull` | ollama.com | ~2.5 GB per model (`qwen3:4b-instruct`, `qwen3:4b`) |
| Piper voice | Hugging Face | 63 MB |
| Whisper `base` (multilingual) | Hugging Face | 141 MB |
| Whisper `small` (multilingual) | Hugging Face | 464 MB |
| Whisper `small.en` (legacy English-only, optional) | Hugging Face | 464 MB |
| Wake-word models | GitHub | 8 MB |
| 3D library | already in `viewer/vendor/` | 1.4 MB |

The 3D library is already in the project folder, so there is no npm step. Its version and checksum are in `viewer/vendor/VERSIONS.txt`.

## Optional Online Assisted Mode API Keys

DuckDuckGo search requires no API key. If you prefer Tavily, Brave Search, Serper, or a self-hosted HTTPS SearXNG instance, configure keys **outside committed files**:

- **Option A (recommended)**: Set environment variables before starting Jarvis:
  - `JARVIS_TAVILY_API_KEY`
  - `JARVIS_BRAVE_API_KEY`
  - `JARVIS_SERPER_API_KEY`
  - `JARVIS_SEARXNG_API_KEY` (optional bearer token)
- **Option B (System UI)**: Open **System → Internet & Web Search**, pick the provider, paste the key into **Provider API key**, and click **Set**. Keys are written to gitignored `data/secrets.json` and are never echoed back to the browser or saved in `config.json`.

## Using your own notes

Set `"notes_dir"` in `config.json` to a folder of markdown files (absolute path or relative to the project). Sub-folders become categories and `[[Note Title]]` links become graph links. Jarvis writes new memories into a `captures` sub-folder there.
