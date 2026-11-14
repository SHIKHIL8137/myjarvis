# Installation (Windows 10/11)

Two ways to run JARVIS: the **packaged app** (no Python needed) or a **developer install** from source.
In both cases the AI models are installed **by you, separately** — JARVIS never downloads models, installs software or changes Windows settings on its own. Local Mode (offline) is the default; nothing leaves the PC unless you turn on Online Assisted mode.

## A. Packaged app

1. Get `JARVIS-Setup-<version>.exe` (built by `scripts\build-windows.ps1`, see section D) or the portable folder `dist\jarvis\`.
2. Run the installer. It installs **per user** (no administrator rights) and adds a Start Menu shortcut.
   Optional checkboxes: desktop shortcut, **start JARVIS automatically when I sign in** (off by default; stored as a normal per-user `Run` entry and removed on uninstall).
3. Install the separate requirements below (Ollama + models), then start **JARVIS** from the Start Menu.
4. A tray icon appears and the dashboard opens at <http://127.0.0.1:4700>. The **setup wizard** appears on first run (Settings page).

**Where things live** — the program is in the install folder (read-only). Your data is in **`%LOCALAPPDATA%\JARVIS`** (override with the `JARVIS_HOME` environment variable):

| Folder / file | Purpose |
|---|---|
| `config.json`, `config\` | defaults copied on first start (`config\dictionary.txt` = dictation words, `config\vocab_hints.txt` = speech hints) |
| `data\` | database, memory, settings overrides, encrypted secrets, dictation history |
| `models\whisper`, `models\piper`, `models\wake` | speech models you install (below) |
| `notes\`, `logs\` | notes and logs (tray → *Open logs folder*) |

Uninstalling removes the program only; `%LOCALAPPDATA%\JARVIS` is kept. Delete it yourself for a full wipe.

## B. Requirements you install separately (the installer never does this)

| Component | Needed for | How |
|---|---|---|
| **Ollama** (0.40+) | the local language model | <https://ollama.com/download> |
| LLM model | answers | `ollama pull qwen3:4b-instruct` |
| Whisper model | speech-to-text, dictation | `.venv\Scripts\python.exe -c "from faster_whisper import download_model; download_model('medium.en', cache_dir=r'models\whisper')"` (developer install). Packaged app: run the same call with any Python that has `faster-whisper`, using `cache_dir=` **`%LOCALAPPDATA%\JARVIS\models\whisper`** |
| Piper voice | spoken replies | download `en_GB-alan-medium.onnx` and `.onnx.json` from <https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/alan/medium> into `%LOCALAPPDATA%\JARVIS\models\piper` |
| Wake-word model | "Hey Jarvis" | `python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'], target_directory=r'%LOCALAPPDATA%\JARVIS\models\wake')"` |
| Tesseract OCR (optional) | *read the screen* | <https://github.com/UB-Mannheim/tesseract/wiki> (install, restart JARVIS) |
| `uvx` (optional) | Home Assistant MCP preset | part of [uv](https://docs.astral.sh/uv/) |

The **Settings → Setup / System health** page shows which of these are missing and prints the exact command for each. Disk need: about 2.5 GB per Ollama model, ~1.5 GB for `medium.en`, 63 MB per Piper voice.

## C. First-run configuration

Open Settings; the wizard walks through: Ollama detection and installed models → choose the LLM → speech model → Piper voice → **microphone level meter** → **speaker test** → **wake-word test** → finish. Every choice is saved through the normal Settings API. Skipped steps can be done later from Settings.

Other switches (all **off** by default): MCP servers, push-to-talk dictation (default hotkey `Ctrl+Alt+Space`), screen reading, Online Assisted mode.
Windows may ask for microphone permission: *Settings → Privacy & security → Microphone → allow desktop apps*.

## D. Building the installer yourself

```powershell
cd D:\projects\myjarvis
.\.venv\Scripts\python.exe -m pip install pyinstaller      # once; the build script will NOT install it for you
.\scripts\build-windows.ps1                                # frontend -> PyInstaller -> smoke test -> optional Inno Setup
```

Output: `dist\jarvis\jarvis.exe` (portable, ~470 MB, no models) and, if [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, `dist\JARVIS-Setup-<version>.exe`.
Flags: `-SkipFrontend`, `-SkipSmoke`, `-RunTests`. The **smoke test** (`scripts\smoke_test.py --exe dist\jarvis\jarvis.exe`) launches the packaged app with a throw-away home and free port, checks `/api/health`, the frontend and that MCP is empty by default, then shuts it down through its own API and requires exit code 0.

## E. Developer install (from source)

Requirements: Python 3.14, Node 20+, Ollama.

```powershell
cd D:\projects\myjarvis
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.lock
cd apps\frontend; npm ci; npm run build; cd ..\..
ollama pull qwen3:4b-instruct
.\.venv\Scripts\python.exe -m piper.download_voices en_GB-alan-medium --data-dir models\piper
.\.venv\Scripts\python.exe -c "from faster_whisper import download_model; download_model('medium.en', cache_dir=r'models\whisper')"
.\.venv\Scripts\python.exe -c "from openwakeword import utils; utils.download_models(model_names=['hey_jarvis'], target_directory='models/wake')"
.\start-jarvis.ps1                                         # or: .\.venv\Scripts\python.exe server.py --tray
.\.venv\Scripts\python.exe preflight.py
```

If PowerShell refuses to run scripts: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
`python server.py` runs headless; `--tray` adds the tray icon (default in the packaged app), `--no-browser` skips opening the dashboard.

## F. What needs the internet, and when

| Component | Source | Size |
|---|---|---|
| `pip install` | PyPI | ~540 MB installed |
| `ollama pull` | ollama.com | ~2.5 GB per model |
| Piper voice | Hugging Face | 63 MB |
| Whisper model | Hugging Face | 0.15–1.5 GB depending on size |
| Wake-word models | GitHub releases | ~10 MB |

After setup, Local Mode needs no internet. Cloud models (OpenRouter etc.), web search and network MCP servers require you to enable **Online Assisted** mode and, for cloud LLMs, `allow_cloud`; every outbound request is audited in `logs\external_audit.jsonl`.

## G. Troubleshooting

* Tray icon missing → run `jarvis.exe --no-tray` from a terminal to see errors, check `logs\jarvis.log`.
* Dashboard says backend down → another program may hold port 4700 (`JARVIS_PORT=4701` changes it for one run).
* Dictation hotkey does nothing → Settings → Dictation must be **On**; the combo must include a modifier; some apps running as administrator cannot receive pasted text from a normal-rights process.
* Antivirus flags `jarvis.exe` → it is an unsigned PyInstaller build; sign it yourself or build from source.
* More in `TROUBLESHOOTING.md`.
