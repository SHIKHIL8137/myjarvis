# Troubleshooting

First step for any problem: `.\.venv\Scripts\python.exe preflight.py` (with Jarvis running).
Logs: `logs\jarvis.log` (application), `logs\server.err` (startup errors), and `logs\external_audit.jsonl` (outbound web audit).

| Symptom | Cause and fix |
|---|---|
| `start-jarvis.ps1` says Jarvis did not start | Read `logs\server.err`. Port 4700 in use: change `"port"` in `config.json` |
| Scripts are blocked by PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Header shows "OLLAMA DOWN"; replies say model is unreachable | Start the Ollama app, or run `ollama serve` |
| "Model … is not installed" | `ollama pull qwen3:4b-instruct` (needs internet once) |
| First reply after start takes 5–15 s | The model is loading into VRAM/RAM. Later replies start in ~1–3 s |
| Replies or STT are slow | Close memory-heavy apps; on 16 GB RAM free memory can drop below 2 GB. Use Whisper `base` (`balanced` or `fast`) instead of `small` |
| Wrong words or domain terms misheard | Add terms/names to **Vocabulary hints** in **System → Speech & Translation**, switch Whisper model from `base` to `small`, or use **Quality: Accurate (beam 5)** |
| Malayalam/Hindi speech transcribes in English | Check **Whisper model** is `small` or `base` (NOT `small.en`, which is English-only), and set **Input language** to `ml` or `hi` (or `auto`) |
| Speech is silently translated or not translated | Check **Speech action** (`Transcribe only` vs `Transcribe + translate`) and **Translate target** in System settings |
| Jarvis hears / transcribes its own Bluetooth speaker output | Keep **Audio duplex** set to **Safe Half-Duplex** (default) and ensure **Mic resume delay** is ≥ `250–300 ms`. Uncalibrated Bluetooth A2DP latency prevents reliable full-duplex software AEC |
| Voice barge-in does not trigger during speech | Expected in **Safe Half-Duplex** mode (microphone is intentionally suspended during playback; use the **■** button to interrupt) |
| Header shows `OLLAMA DOWN` even though `ollama serve` is running | Ensure `config.json` uses `http://127.0.0.1:11434/v1` rather than `localhost` (Windows IPv6 `[::1]` resolution adds a ~2.1 s ConnectTimeout against IPv4-bound Ollama) |
| Short words like "Stop" or "Yes" aren't captured | Lower `silence_ms` carefully or speak slightly louder; Jarvis recovers short utterances via VAD fallback down to 200 ms |
| "Hey Jarvis" is not detected or wakes on its own | Use headphones; adjust Wake threshold in System settings (0.5 → 0.4 to increase sensitivity, or 0.65 to reduce false triggers) |
| "cannot open microphone" or sample rate errors | Another app holds the device or rate differs from 16 kHz. Pick the device in Settings and toggle the microphone; Jarvis auto-falls back to native device rate + polyphase resampling |
| Web search says "Jarvis is in Local Mode" | Internet access is OFF by default. Click **INTERNET** on the HUD or enable **Online Assisted Mode** in System settings |
| DuckDuckGo search fails with rate-limit/challenge | DuckDuckGo can rate-limit automated queries. Configure `Tavily`, `Brave`, `Serper`, or `SearXNG` in System settings |
| "API key is not configured" | Set the provider env var (`JARVIS_TAVILY_API_KEY`, `JARVIS_BRAVE_API_KEY`, `JARVIS_SERPER_API_KEY`) or save it under **System → Internet & Web Search** |
| Page retrieval blocked (`robots.txt` / private IP / `https://`) | Expected security enforcement: Jarvis refuses `http://`, localhost/private IPs, redirects to internal hosts, oversized pages, and disallowed `robots.txt` paths |
| 403 "missing X-Jarvis header" on API calls | Include `X-Jarvis: 1` header on POST/PUT/DELETE requests |
| 403 "forbidden host" | Open the app as `http://127.0.0.1:4700/` or `http://localhost:4700/` |
| A note was deleted by mistake | It is in `notes\.trash\`. Move the file back into a category folder |
| Local fine-tuning job reports `blocked` (`missing optional packages`) | Intentional: `.venv` does not bundle heavy training packages (`torch`, `transformers`, `peft`), and 4B QLoRA exceeds 4 GB VRAM alongside Ollama. Keep RAG Knowledge Pipeline + Feedback Learning active locally or configure a GPU environment for smaller LoRA targets |
| Filesystem read/search returns 403 Permission Denied | Path falls outside `filesystem.allowed_roots` or targets sensitive/excluded files (`.env`, `.git`, keys, system folders) |
| Model promotion returns 403 (`evaluation is required`) | Run the fixed 12-category evaluation (`Run Evaluation`) first, and verify Ollama compatibility (`peft_lora` directories must be converted to GGUF/Modelfile before Ollama can load them) |

## Hardware Limitations (Ryzen 5 5600H + GTX 1650 4 GB + 16 GB RAM)
- **4 GB VRAM budget**: `qwen3:4b-instruct` (~2.2 GB in VRAM + CPU split) occupies most of the GTX 1650, so Whisper runs on CPU (`int8`, 6 threads) and Piper runs on CPU.
- **CPU STT trade-off**: Multilingual `base` runs faster than real time (~1.2 s for 4–5 s audio, +139 MB RAM), while `small` takes ~3.6 s (+319 MB RAM) for better compound and Indic accuracy. Larger models like `medium` or `large-v3-turbo` are too slow/heavy for interactive voice turns on this CPU/RAM budget. Note: on real microphone tests, Whisper `small` detected Malayalam (`ml`, 0.93) and translated short queries accurately (`task="translate"`), while native Malayalam Unicode script orthography in `base`/`small` can drift into Devanagari/Latin due to Whisper small/base training limits.
- **Wake word threshold**: Real human voices often score ~0.45–0.50 on `hey_jarvis_v0.1` (observed `0.477` on microphone test); lower the Wake threshold in System settings (`0.50 → 0.42`) if wake detection feels strict.
- **4B model tool calling**: `qwen3:4b-instruct` supports Ollama tool calling, though small 4B models can occasionally skip a tool call under strict prompts — Jarvis pairs Ollama tool calling with deterministic intent fallback routing.

To reset: stop Jarvis, then delete `data\history.jsonl` (conversation), `data\settings.json` (settings overrides), `data\secrets.json` (saved API keys), or `data\tts_cache\` (cached speech). Notes are never touched by a reset.
