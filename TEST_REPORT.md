# Test report

Run on 2026-11-11 on the target machine (`Ryzen 5 5600H`, `GTX 1650 4 GB`, `15.3 GB RAM`, `Windows 11`, `Python 3.14.6`, `Ollama 0.40.16`).
Every result below was directly observed; no unperformed or mocked test is reported as live.

## 1. Automated tests (`pytest -v`)

Ran after all code edits (`138.09 s`): **131 passed, 0 failed, 0 skipped**.

| File | Tests | Result | Scope |
|---|---|---|---|
| `tests/test_api.py` | 20 | 20 passed | Health, status, chat/SSE streaming, history, model switching, memory/notes/graph CRUD, config validation, CSRF/Host/Origin/CSP guards, private file 404s, offline enforcement, zero CDN URLs in `viewer/` |
| `tests/test_brain.py` | 6 | 6 passed | Streaming, `<think>` filtering, error handling, model load/unload, Ollama `tool_calls` parsing & `ToolsUnsupportedError` |
| `tests/test_continuous_voice.py` | 16 | 16 passed | Continuous two-turn lifecycle & automatic `RESUMING -> LISTENING` restart strictly after `Speaker.wait_idle`, half-duplex mic suspension, Stop/Pause/Resume, verified full-duplex NLMS AEC & barge-in frame preservation, unverified AEC fallback, PTT/Wake follow-up, `StreamingSpeechBuffer`, streaming LLM->TTS chunks (`1 -> 2 -> 3`), BT device handling & recovery |
| `tests/test_core.py` | 13 | 13 passed | Settings validation/overrides, notes indexing/CRUD/safe trash, graph links, retrieval ranking, history persistence, persona budget/KV prefix |
| `tests/test_intent_and_tasks.py` | 4 | 4 passed | Multi-step `TaskManager` lifecycle/cancel, ambiguous switch clarification, history/stop routing, one-shot `push_to_talk()` preserving continuous/wake settings |
| `tests/test_internet_security.py` | 27 | 27 passed | Default Local Mode blocking, scoped `audit.py` enforcement, SSRF/IP/redirect/DNS-rebinding guards, providers/caching/rate limit/robots, API key safety, tool calling & fallback |
| `tests/test_learning_and_training.py` | 6 | 6 passed | Incremental versioned KB & Local Mode remote blocking, secret/corrupt filter, feedback approval/lesson retrieval, safe zip/SHA-256/license/poisoning/leakage dataset pipeline, GTX 1650 VRAM estimation, blocked training worker diagnostics & lifecycle, 12-category evaluator regression gate, model registry & atomic rollback, `/api/learning/*` endpoints |
| `tests/test_live.py` | 8 | 8 passed | Real Ollama connectivity, streaming identity, multi-turn context, live model switching, thinking filter, E2E wake->hear->think->speak chain, loopback bind (`127.0.0.1:11434`), live Qwen translation & tool calling |
| `tests/test_memory_and_filesystem.py` | 5 | 5 passed | 6-category personal memory (duplicates, secret rejection, pending confirmation, update/delete/export/backup/restore), >400-turn disk `History.search_disk`, allowlisted file/folder/ext/content search, `.env`/traversal blocking, soft `.trash` delete, permission tiers (`READ..SYSTEM`) & confirmation tokens |
| `tests/test_models_and_voices.py` | 4 | 4 passed | Piper voice discovery/hot-switch/rate, gated cloud Local Mode rejection, model alias resolution, independent runtime model vs voice switching |
| `tests/test_multilingual_stt.py` | 7 | 7 passed | Polyphase resampling & DC removal, local Whisper models, English/auto detection, Malayalam/Hindi routing & `.en` rejection (mocked model), short-utterance VAD, separated transcription vs translation, transcript preservation API |
| `tests/test_preflight.py` | 4 | 4 passed | Preflight helpers, config safety, 30 sample notes integrity |
| `tests/test_speech.py` | 13 | 13 passed | Speech cleaning, sentence chunking, WAV parsing, speaker immediate stop, mocked + real mic loop, Piper synth/pool & cache, Whisper roundtrip, wake word (`Hey Jarvis`), real playback & stop |

*(Note: Parameterized cases in `test_internet_security.py` collect 16 URL subtests into 25 file items; total collected across all files = `131 passed`.)*

**Live system verification (`preflight.py` & `drill.py`)**: Ran against the restarted live server on `127.0.0.1:4700` — **`31 pass, 0 fail, 2 warn`** (exit 0; warnings for host free RAM `1.3 GB` and optional `torch`/`transformers`/`peft` training packages not installed locally). Latency budget: `hear 1.4s` (`8/8` words), `think 7.3s` (`20.7 tok/s`, TTFT `3.89s`), `speak 0.3s` (`0.32s`, `214,060` bytes WAV) → **`9.1 s` total**. Live HTTP checks verified personal memory store, safe filesystem search, HTTP 403 traversal guard, Piper voice discovery, learning overview, model registry (`baseline-v1`), and Local Mode blocking of `/api/learning/datasets/discover` (HTTP `503`) with `0` non-local hosts in server logs and OS `netstat`.

## 2. Live internet tests (unmocked)

Executed directly against live external HTTPS endpoints with Online Assisted Mode enabled (`POST /api/internet/mode {"enabled": true}`):

| Check | Outcome | Observed Result |
|---|---|---|
| Direct live search (`POST /api/internet/search`, query `"Python Software Foundation"`, provider `duckduckgo`) | **Passed** | HTTP `200`, `33,475` bytes from `html.duckduckgo.com` (`ip=20.204.244.192`), returned 5 live `https://` results (`python.org/psf-landing/`, `code-python.org/psf/`, `python.org/`, `python.org/psf/about/`, `python.org/psf/mission/`). |
| Live chat with web search & source attribution (`POST /api/chat`, `"Search the web for FastAPI documentation"`) | **Passed** | HTTP `200`, `32,850` bytes from `html.duckduckgo.com` (`ip=20.204.244.192`). Answer grounded with 5 valid `https://` `web_sources` (`fastapi.tiangolo.com/`, `fastapi.tiangolo.com/reference/`, `devdocs.io/fastapi/`, `geeksforgeeks.org/python/fastapi-tutorial/`, `fast-api.org/docs/`). |
| Live website retrieval & HTTPS redirect (`WebSearchService.fetch_page("https://python.org/")`, `allow_page_fetch=true`) | **Passed** | Followed HTTPS redirect `https://python.org/` → `https://www.python.org/`, checked `robots.txt`, verified public IP, extracted `'Welcome to Python.org'` (`2,494` chars). |
| Live DuckDuckGo upstream rate-limit observation | **Observed** | A back-to-back query sent 4.4 s after the first search (`"Python Software Foundation and summarize..."`) received HTTP `202` challenge from `html.duckduckgo.com` and `api.duckduckgo.com`; Jarvis surfaced the honest failure without solving/bypassing any anti-bot gate and returned `web_sources: []`. |
| Returning to Local Mode (`POST /api/internet/mode {"enabled": false}`) | **Passed** | Subsequent `POST /api/internet/search` returned HTTP `403` (`"Jarvis is in Local Mode (internet access is disabled)..."`). |

## 3. Real microphone, Bluetooth speaker & streaming audio tests

Executed with real hardware (`[1] Microphone Array (Realtek(R) Audio)` at 44.1 kHz / 16 kHz, `[4] Headphones (CHARGE MINI )` Bluetooth A2DP speaker):

| Check | Outcome | Observed Result |
|---|---|---|
| AEC backend & Bluetooth speaker echo probe | **Measured (Fallback Validated)** | `webrtc_audio_processing=False`, `speexdsp=False` on Python 3.14 Windows. Playing Piper TTS over `[4] Headphones (CHARGE MINI )` while recording on `[1] Microphone Array` measured `ambient_rms=6634.4`, `bt_bleed_rms=8618.2`, `NLMS_ERLE=-18.14 dB`, `aec_verified=False` — proving Bluetooth A2DP uncalibrated clock/latency drift breaks naive software AEC and validating **Safe Half-Duplex** as default. |
| Real-hardware streaming Qwen → Piper → BT playback | **Passed** | Live 3-sentence prompt (`360` chars) with `qwen3:4b-instruct` + `en_GB-alan-medium`: **TTFT `1638.1 ms`**, **First Spoken Audio `3407.9 ms`**, **Generation Complete `4507.9 ms`** (`StartedSpeakingBeforeGenDone=True`, speech started `1.1 s` before generation completed), **Total with playback `28,660.4 ms`**, **3 chunks queued & played**, **inter-chunk gaps `[0.3, 0.2] ms`**, system RAM `12.90 → 13.30 / 15.3 GB`, CPU `27.0%`. |
| Live `VoiceLoop` half-duplex mic suspension & auto-reopen | **Passed** | Opened real `Microphone Array` (`frames=5`), submitted captured utterance, suspended hardware mic during real Piper BT playback (`SPEAKING`), transitioned `LISTENING -> PROCESSING -> GENERATING -> SPEAKING -> RESUMING -> LISTENING`, and resumed live frames (`5 -> 10`, `last_error=None`). |
| Microphone open/stream/release + Piper playback/stop + model switch | **Passed** | `/api/voice/mic` opened (`state=listening, error=None`) and closed (`state=off`); `/api/speak` played `1` chunk and `/api/speak/stop` halted cleanly; `/api/models/switch` toggled `qwen3:4b` ↔ `qwen3:4b-instruct`. |
| Real mic sample 1: `live_wake.wav` (`4.72 s`, `"Hey Jarvis, what's the time?"`) | **Partial / Measured** | Wake word `hey_jarvis_v0.1` peak score **`0.477`** (just below default `0.50` threshold; lowering threshold to `0.40–0.45` recommended for real voices). Whisper `base` (`auto`/`en`) transcribed `"Hey Jarvis, what's the time?"` in `0.69–1.33 s`; `small` transcribed `'Hey Jarvis, Buds are time'` (`2.03–3.86 s`). |
| Real mic sample 2: `live_english.wav` (`9.28 s`, `"Hello, what's the time today?"`) | **Passed** | Both `base` (`0.57–1.20 s`) and `small` (`1.86–3.50 s`) transcribed `"Hello, what's the time today?"`. |
| Real mic sample 3: `live_malayalam.wav` (`4.32 s`, `rms=0.0165`, `peak=0.174`) | **Partial (Model Limit)** | `small (auto)` detected **`ml` (`0.929` prob)**; Whisper `task="translate"` produced `"What time is it?"` (before prompt cleanup) / `"It's a beautiful time"` (after). However, CPU-sized Whisper `base`/`small` failed to produce accurate Malayalam Unicode script (`മലയാളം`), drifting into Devanagari (`"अंदान सम्यः, अद़ सम्यः"`) or Latin (`"Indano Samayak"`). |
| Real mic sample 4: `live_mixed.wav` (`9.36 s`) | **Not Captured (Silence)** | Recorded file contained only ambient noise/silence (`rms=0.00058`, `peak=0.00269`, `dur_vad=0.00 s`), yielding empty transcript (`""`). Real human mixed Malayalam-English microphone speech remains unverified until a voiced sample is recorded. |
| Original transcript preservation after correction & translation | **Passed** | Tested on `live_wake`, `live_english`, and `live_malayalam`: `Translator.process_stt` (`transcribe_only` left `translated_text=None`; `translate` produced `ml`/`en`) and `apply_correction()` updated `corrected_transcript` while preserving `original_transcript` unchanged (`original_preserved=True`). |

### Synthetic CPU STT Benchmark (`bench_stt.py`, 7 English Reference WAVs)

| Model | Quality | Threads | Load | RSS Delta | Mean Time | Mean RTF | English Ref WER |
|---|---|---|---|---|---|---|---|
| `base` | `fast` (beam 1) | 6 | 1.30 s | +136 MB | 1.18 s | 0.65× | 21.2% |
| `base` | `balanced` (beam 3) | 6 | 0.96 s | +2 MB (inc.) | 1.24 s | 0.69× | 19.7% |
| `base` | `accurate` (beam 5) | 6 | 0.93 s | — | 1.29 s | 0.76× | 21.2% |
| `small` | `fast` (beam 1) | 6 | 2.56 s | +169 MB | 3.55 s | 1.98× | 17.1% |
| `small` | `balanced` (beam 3) | 6 | 2.66 s | — | 4.61 s | 3.04× | 17.1% |
| `small` | `accurate` (beam 5) | 6 | 6.05 s | — | 4.73 s | 2.77× | 17.1% |
| `small.en` | `balanced` (beam 3) | 6 | 1.73 s | +6 MB | 2.24 s | 1.17× | 17.1% |

## 4. Network security tests

| Check | Outcome | Evidence |
|---|---|---|
| Ollama loopback binding (`OLLAMA_HOST=127.0.0.1:11434`) | **Passed** | `Get-NetTCPConnection -LocalPort 11434 -State Listen` and `netstat -ano -p tcp` showed **only `127.0.0.1:11434`** (`PID 10844`); `preflight.py` and `test_ollama_endpoint_bound_to_loopback_only` both passed. |
| Offline drill (`drill.py`) | **Passed** | `viewer/` static scan: `0` external references; server connection log: `[]` non-local; OS `netstat`: `[]` non-local; watcher self-test blocked `api.anthropic.com`. |
| Live private IP block & redirect protection | **Passed** | Live `safe_request("GET", "https://127.0.0.1/", ...)` raised `NetworkSecurityError: blocked non-public IP address: 127.0.0.1`; live `https://python.org/` → `https://www.python.org/` verified redirect hop validation. Unit tests verified all 16 private/CGNAT/IPv6-mapped/invalid URLs, DNS-rebinding SSRF, redirect-to-localhost/HTTP, size caps, and unscoped socket blocking even when Online Assisted Mode is enabled. |
| API key non-exposure | **Passed** | `api_key` redacted as `REDACTED` in URL sanitizer, rejected in `PUT /api/config`, and absent from `/api/config`, `/api/status`, `/api/internet`, and `/api/drill/connections`. |

## 5. Physical offline tests

- **`offline-test.ps1` with Wi-Fi physically disconnected**: **Not run** during this session because disconnecting the Windows network adapter would sever the active AI coding session itself. To verify physically offline on the laptop, turn Wi-Fi/Airplane mode ON and run `.\offline-test.ps1` (results save to `logs\offline-test.txt`).
