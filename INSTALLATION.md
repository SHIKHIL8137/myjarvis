# Installation (Windows)

This machine is already set up. These steps rebuild it from scratch. Steps 2–5 need the internet once; nothing after that does.

Requirements: Windows 10/11, Python 3.14, Ollama 0.40+, about 4 GB of free disk, a microphone and headphones.

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

# 4. Whisper small.en
.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8', download_root='models/whisper')"

# 5. Wake-word models
.\.venv\Scripts\python.exe -c "from openwakeword import utils; utils.download_models(model_names=['hey_jarvis'], target_directory='models/wake')"

# 6. Start and verify
.\start-jarvis.ps1
.\.venv\Scripts\python.exe preflight.py
```

If PowerShell refuses to run the scripts: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## What needs the internet, and when

| Step | Source | Size |
|---|---|---|
| `pip install` | PyPI | about 540 MB installed |
| `ollama pull` | ollama.com | 2.5 GB per model |
| Piper voice | Hugging Face | 63 MB |
| Whisper small.en | Hugging Face | about 465 MB |
| Wake-word models | GitHub | 8 MB |
| 3D library | already in `viewer/vendor/` | 1.4 MB |

The 3D library is already in the project folder, so there is no npm step. Its version and checksum are in `viewer/vendor/VERSIONS.txt`.

## Installed packages

Pinned in `requirements.txt` (exact resolved set in `requirements.lock`): fastapi 0.162.4, uvicorn 0.54.0, httpx 0.28.1,
numpy 2.5.4, faster-whisper 1.2.1, piper-tts 1.8.0, openwakeword 0.6.0, sounddevice 0.5.6, pytest 9.2.0.

## Using your own notes

Set `"notes_dir"` in `config.json` to a folder of markdown files (absolute path or relative to the project). Sub-folders become
categories and `[[Note Title]]` links become graph links. Jarvis writes new memories into a `captures` sub-folder there.
