# Troubleshooting

First step for any problem: `.\.venv\Scripts\python.exe preflight.py` (with Jarvis running). It names the broken link.
Logs: `logs\jarvis.log` (application) and `logs\server.err` (startup errors).

| Symptom | Cause and fix |
|---|---|
| `start-jarvis.ps1` says Jarvis did not start | Read `logs\server.err`. Port 4700 in use: change `"port"` in `config.json` |
| Scripts are blocked by PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Header shows "ollama down"; replies say the model is unreachable | Start the Ollama app, or run `ollama serve` |
| "Model … is not installed" | `ollama pull qwen3:4b-instruct` (needs internet once) |
| First reply after start takes 5–15 s | The model is loading into memory. Later replies start in about 1–3 s |
| Replies are slow in general | Close memory-heavy apps; free RAM was often under 2 GB during testing. See HARDWARE_BENCHMARK.md |
| The thinking model takes half a minute | Expected: it reasons silently first. Switch back to `qwen3:4b-instruct` |
| "Hey Jarvis" is not detected | Say both words, then pause. Lower the wake threshold in Settings (0.5 → 0.4). Check the right microphone is selected |
| It wakes on its own or hears itself | Use headphones. Raise the wake threshold |
| It cuts you off mid-sentence | Raise "Silence before I stop listening" in Settings (800 → 1200 ms) |
| "cannot open microphone" | Another app holds the device, or it was unplugged. Pick a device in Settings, then toggle the microphone |
| No sound | Pick the speaker in Settings; make sure "Voice output enabled" and "Speak replies" are on |
| Wrong words transcribed | `small.en` on CPU is the speed/accuracy trade-off here. Speak closer to the microphone |
| 403 "missing X-Jarvis header" when calling the API yourself | Add the header `X-Jarvis: 1` to POST/PUT/DELETE requests |
| 403 "forbidden host" | Open the app as `http://127.0.0.1:4700/` or `http://localhost:4700/` |
| A note was deleted by mistake | It is in `notes\.trash\`. Move the file back into a category folder |
| Galaxy is empty | `notes_dir` in `config.json` points at a folder without `.md` files |
| "offline mode: blocked outbound connection" in the log | Something tried to reach the internet and was stopped. That is the guard working |
| preflight warns about the Ollama bind address | See the last section of OFFLINE_MODE.md |

To reset: stop Jarvis, then delete `data\history.jsonl` (conversation), `data\settings.json` (settings), or `data\tts_cache\`
(cached speech). Notes are never touched by a reset.
