# Jarvis — a fully local voice assistant

Runs on this Windows laptop with no cloud service: a local language model (Ollama), local speech-to-text (Whisper), local
text-to-speech (Piper), a "Hey Jarvis" wake word, markdown memory, and a 3D galaxy of your notes.

```powershell
.\start-jarvis.ps1      # starts the backend and opens http://127.0.0.1:4700/
.\stop-jarvis.ps1       # stops it (Ollama and its models are left alone)
```

The screen is a command centre: the 3D galaxy of your notes fills the centre, the left rail manages knowledge, the reactor on
the right shows Jarvis's state, and the command bar sits at the bottom. Type there, or click the microphone button and say
**"Hey Jarvis"** followed by your question. Use headphones.

| Document | What it covers |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Setting up from scratch, exact commands |
| [USAGE.md](USAGE.md) | Talking to Jarvis, memory, models, settings |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Modules, data flow, API |
| [OFFLINE_MODE.md](OFFLINE_MODE.md) | What is enforced, how to verify with Wi-Fi off |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common problems |
| [TEST_REPORT.md](TEST_REPORT.md) | What was tested and the results |
| [HARDWARE_BENCHMARK.md](HARDWARE_BENCHMARK.md) | Speed and memory on this machine |
| [DOCUMENTATION_ANALYSIS.md](DOCUMENTATION_ANALYSIS.md) | How the two prompt-pack PDFs map to this build |

`PLAN.md` is the original planning note and is kept for history; this README and the documents above are current.

## Check that everything works

```powershell
.\.venv\Scripts\python.exe -m pytest -q      # automated tests
.\.venv\Scripts\python.exe preflight.py      # live end-to-end check of the running system, with a latency budget
.\offline-test.ps1                           # the same, but only runs with the network switched off
```

## Known limits

- The physical Wi-Fi-off test has to be run by you (`offline-test.ps1`); see OFFLINE_MODE.md.
- The wake phrase is "Hey Jarvis". A bare "Jarvis" is not reliable.
- No voice cloning, no web research, no cloud model.
- Your Ollama listens on all network interfaces (`OLLAMA_HOST=0.0.0.0`); see TROUBLESHOOTING.md to restrict it.
