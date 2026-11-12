# Test report

Updated on 2026-11-11 after completing **P10 — Reliability, Security, Memory, Workforce & Autonomous Learning Hardening** and **P10.11 — Final Critical Security and Reliability Corrections** on the target machine (`Ryzen 5 5600H`, `GTX 1650 4 GB`, `15.3 GB RAM`, `Windows 11`, `Python 3.14.6`, `Ollama 0.40.16`, `Node v24.16.0`).
Every result below was directly observed; no unperformed, skipped, or mocked test is reported as live. See [P10_TEST_REPORT.md](P10_TEST_REPORT.md) and [P10_IMPLEMENTATION_REPORT.md](P10_IMPLEMENTATION_REPORT.md) for the complete matrix.

## 1. Automated tests & static verification (post-P10.11 edits)

Ran `.venv\Scripts\python.exe -m pytest -v` after all P10.11 code edits (`227.40 s` / `0:03:47`): **164 passed, 3 skipped, 0 failed** (`98` external dependency deprecation warnings from `langsmith` and `sounddevice`; skipped tests require live Docker daemon or `JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL`).

| File | Tests | Result | Scope |
|---|---|---|---|
| `tests/test_p10_11_hardening.py` | 10 | 8 passed, 2 skipped | Committed `base_sha..HEAD` secret block, post-review commit invalidation, rename/delete/binary/`>200 KB`/oversized scan, Docker CLI & no-silent-fallback + env stripping, `PGPASSFILE`/`--exit-on-error`/non-zero/prod-DB/pgvector checks (live PG & Docker skipped), training blockers/override/concurrency/QLoRA guard, real active subprocess cancellation & `asyncio.to_thread` responsiveness, gateway `require_streaming` + `prompt+max_tokens` context gate, DR DB-only vs full tracked+untracked repo restore & overwrite guard |
| `tests/test_ai_os_agents_workforce_dr.py` | 6 | 6 passed | Document Engine (`PDF`/`DOCX`/`PPTX`/`XLSX`), Reminders/Calendar/Email, LangGraph + Git worktree + interrupt/restart resume, diff secret scan, DR backup/tamper, `/api/v2/*` & dist compliance |
| `tests/test_ai_os_memory_decision_gateway.py` | 5 | 5 passed | Unicode Malayalam tokenization & PII/secret firewall, DPAPI + AES-256-GCM vault, 5-way memory & legacy migrator, 3-layer DecisionEngine & gateway guard, overview read-only / TTS prune / DI |
| `tests/test_p10_workforce_safety.py` | 5 | 5 passed | State machine transition guard, failed QA/exhausted retries halt before approval/merge, untracked `.env`/binary/symlink manifest scan without leaking raw secret, post-review diff invalidation, worktree collision/ownership/uncommitted cleanup guard, `ExecutionPolicy` deny list |
| `tests/test_p10_pgvector_memory.py` | 5 | 4 passed, 1 skipped | Embedding normalization & dimension mismatch guard, vector+SQL expiry & purge, full lifecycle CRUD/duplicates/supersession/audit/ranking, PgVector DB-side SQL operators & 1,000-vector SQLite benchmark + migration; live `@pytest.mark.pg` test skipped cleanly without `JARVIS_PG_TEST_URL` |
| `tests/test_p10_dr_gateway_learning.py` | 4 | 4 passed | Encrypted `.jvault` backup/HMAC tamper detection/full isolated directory restore/retention, real chunked SSE streaming/paid-model lock/central firewall/breaker/cancellation/`ResourceGovernor`, KB license/poisoning/cross-dedup/auto-promote block/remote training spec, `/api/v2/*` CRUD & CSRF |
| `tests/test_api.py` | 20 | 20 passed | Health, status, chat/SSE streaming, history, model switch, memory/notes/graph, config, CSRF/Host/Origin/CSP, offline enforcement, zero CDN URLs in `viewer/` |
| `tests/test_brain.py` | 6 | 6 passed | Streaming, `<think>` filter, friendly errors, unload/no-pull, Ollama `tool_calls`, thinking-budget fallback |
| `tests/test_continuous_voice.py` | 16 | 16 passed | Continuous voice two-turn loop, Stop/Pause/Resume, half-duplex suspension, NLMS AEC fallback, barge-in, streaming chunks, BT handling |
| `tests/test_core.py` | 13 | 13 passed | Settings/overrides, notes CRUD/trash/graph, retrieval ranking, history, persona budget |
| `tests/test_intent_and_tasks.py` | 4 | 4 passed | `TaskManager`, clarification prompts, history/stop routing, one-shot PTT |
| `tests/test_internet_security.py` | 25 | 25 passed | Local Mode enforcement, SSRF/IP/redirect/DNS-rebinding guards, providers/rate limit/robots, key redaction, tool calling |
| `tests/test_learning_and_training.py` | 6 | 6 passed | Knowledge versioning & offline block, dataset validation/cleaning, 12-cat evaluator, feedback store, training lifecycle/registry/rollback |
| `tests/test_live.py` | 8 | 8 passed | Live Ollama connectivity, streaming identity, context, model switch, thinking safety, E2E wake->hear->think->speak, `127.0.0.1:11434` bind, live Qwen translation & tools |
| `tests/test_memory_and_filesystem.py` | 5 | 5 passed | Personal memory CRUD/backup/restore, disk history search, allowlisted FS bounds & soft delete, permission tiers |
| `tests/test_models_and_voices.py` | 5 | 5 passed | Piper discovery/hot-switch, gated cloud Local Mode rejection & `safe_request`, aliases, independent model vs voice switch |
| `tests/test_multilingual_stt.py` | 7 | 7 passed | Polyphase resampling, Whisper local models, EN/ML/HI routing, short VAD, transcript vs translation preservation |
| `tests/test_preflight.py` | 4 | 4 passed | Preflight helpers, config safety, 30 sample notes |
| `tests/test_speech.py` | 13 | 13 passed | Speech cleaning, chunking, WAV parsing, speaker stop, mic loop, Piper cache, Whisper roundtrip, wake word, real playback/mic |

### Static checks, builds, migrations & dependency audit (post-edit)

| Check | Result | Scope & Evidence |
|---|---|---|
| `ruff check` (12 P10.11 changed `jarvis/` files + `test_p10_11_hardening.py`) | **Passed** | `All checks passed!` |
| `mypy --follow-imports=silent` (12 P10.11 changed `jarvis/` files) | **Passed** | `Success: no issues found in 12 source files` (`27` pre-existing errors remain in 7 legacy files under unfiltered `mypy jarvis`) |
| `pip check` & `pip-audit --local` | **Passed** | `No broken requirements found.` and `No known vulnerabilities found` |
| Alembic migration roundtrip (temp SQLite DB) | **Passed** | `alembic upgrade head -> downgrade base -> upgrade head` exited `0,0,0` (`0001_initial_schema` -> `0002_p10_hardening`) |
| React + TypeScript frontend (`apps/frontend`: `npm run build`) | **Passed** | `tsc --noEmit && vite build` transformed `35` modules in `1.01 s` (`dist/assets/index-DaALhkLZ.js` `173.15 kB`) |

**Live system verification (`preflight.py` & `drill.py`)**: Ran against live `server.py` on `127.0.0.1:4700` — **`32 pass, 0 fail, 2 warn`** (exit `0`; warnings for `1.5 GB free RAM` and optional `torch`/`transformers`/`peft` packages not installed locally). Latency budget: `hear 1.6s` (`8/8` words), `think 9.6s` (`21.5 tok/s`, TTFT `6.3s`), `speak 0.4s` (`0.40s`, `247,852` bytes WAV) → **`11.7 s` total**. Offline drill: `0` external references across `viewer/` & `apps/frontend/dist/`, `0` non-local hosts in server logs and OS `netstat`, watcher blocked `api.anthropic.com`, and `/api/v2/system/overview` passed.

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
| Ollama loopback binding (`OLLAMA_HOST=127.0.0.1:11434`) | **Passed** | `preflight.py` (`ollama bind address: loopback only (127.0.0.1)`) and `test_ollama_endpoint_bound_to_loopback_only` both passed post-upgrade. |
| Offline drill (`drill.py`) | **Passed** | `viewer/` & `apps/frontend/dist/` static scan: `0` external references; server connection log: `[]` non-local; OS `netstat`: `[]` non-local; watcher self-test blocked `api.anthropic.com`. |
| Live private IP block & redirect protection | **Passed** | Live `safe_request("GET", "https://127.0.0.1/", ...)` raised `NetworkSecurityError: blocked non-public IP address: 127.0.0.1`; live `https://python.org/` → `https://www.python.org/` verified redirect hop validation. Unit tests verified all 16 private/CGNAT/IPv6-mapped/invalid URLs, DNS-rebinding SSRF, redirect-to-localhost/HTTP, size caps, and unscoped socket blocking even when Online Assisted Mode is enabled. |
| API key non-exposure & outbound PII firewall | **Passed** | `api_key` redacted as `REDACTED` in URL sanitizer, rejected in `PUT /api/config`, and absent from `/api/config`, `/api/status`, `/api/internet`, and `/api/drill/connections`; `pii_firewall.scrub_messages` & Windows DPAPI/AES-256-GCM secrets tested in `test_ai_os_memory_decision_gateway.py`. |

## 5. Physical offline tests

- **`offline-test.ps1` with Wi-Fi physically disconnected**: **Not run** during this session because disconnecting the Windows network adapter would sever the active AI coding session itself. To verify physically offline on the laptop, turn Wi-Fi/Airplane mode ON and run `.\offline-test.ps1` (results save to `logs\offline-test.txt`).

## 6. Unverified / blocked external scopes

- **Live Chrome DevTools browser inspection (`http://127.0.0.1:4700/` and `/legacy`)**: Blocked in this session because Chrome remote debugging (`C:\Users\shikh\AppData\Local\Google\Chrome\User Data\DevToolsActivePort`) was not active (`ENOENT`). Enable remote debugging via `chrome://inspect/#remote-debugging` in Chrome to verify interactive React rendering in the live browser; static `tsc --noEmit && vite build`, `preflight.py` HTTP byte-hash parity, and `FastAPI TestClient` API tests all passed.
- **Live PostgreSQL + `pgvector` server execution**: Not run against a live PostgreSQL server because `JARVIS_PG_TEST_URL` credentials were not configured; SQLAlchemy ORM models, Alembic migrations, isolated vector scopes, and LangGraph checkpointing were verified against SQLite (`WAL` + foreign keys).
- **Live OpenCode & cloud LLM providers**: Not executed against live external APIs (system kept in Local Mode with `providers.allow_cloud=false`); OpenCode CLI (`1.19.5`) & Graphify (`0.9.80`) presence checked and Dev Workforce orchestrator verified end-to-end with an injected worktree coder, real `git worktree`, real `Graphify`, and real `.venv` `pytest` execution inside the worktree.


## Speech Recognition & Translation Accuracy Upgrade Verification (2026-11-12)

- **Full Python Test Suite (`.\.venv\Scripts\pytest.exe -q -rs`)**: **229 passed, 4 skipped** (`231.54s`), zero failures across unit, API, live Ollama Qwen translation/tool-calling, real microphone stream open/release, continuous half-duplex/barge-in voice lifecycle, and DB persistence tests.
  - Skipped (host prerequisites absent): `test_p10_11_hardening.py:221` (Live Docker daemon not running), `test_p10_11_hardening.py:378` (`JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL` not set), `test_p10_12_hardening.py:165` (Live Docker daemon not running), `test_p10_pgvector_memory.py:193` (`JARVIS_PG_TEST_URL` not set).
- **Speech Upgrade Suite (`tests/test_speech_pipeline_upgrade.py`)**: **7 passed** covering `parse_wav_raw`/`downmix_to_mono`/`bandlimited_resample` across 8/16/44.1/48 kHz stereo+mono, audio diagnostics (empty, short, clipped, silence floor guard), script-mismatch bounded retry, LRU cache eviction + offline specialized provider gating, correction hallucination guard + semantic translation verification auto-retry, SQLite `VoiceTranscriptRow` persistence + `original_transcript` immutability + consented eval export + delete, and REST replay/diagnostics/retry/correct/consent/delete endpoints.
- **Alembic Schema Migration Round-Trip (`0006_voice_pipeline_upgrade`)**: `upgrade head` -> `downgrade -1` -> `upgrade head` exited `0` (`ALEMBIC_ROUNDTRIP_OK`).
- **Frontend Tests & Production Build (`apps/frontend`)**: `npm test -- --run` (**6 passed, 0 failed**) and `tsc --noEmit && vite build` exited `0` (`dist/assets/index-lFZkh7vw.js`).
- **Multilingual ASR Benchmark Before vs After (15-WAV manifest `benchmarks/multilingual/manifest.json`)**:
  - `base fast`: WER `20.4% -> 10.0%`, CER `5.2% -> 3.3%`, Lang Acc `100.0%`
  - `base balanced`: WER `13.1% -> 12.9%`, CER `3.4% -> 3.5%`, Del Rate `0.0%`
  - `small balanced`: WER `20.3% -> 10.7%`, CER `5.0% -> 3.7%`, fixed technical terms (`FastAPI`, `PostgreSQL`, `Ollama`, `Qwen`, `TypeScript`, `Node.js`)
  - `small.en balanced`: WER `19.9% -> 11.8%`, CER `4.8% -> 3.5%`
- **Live Diagnostic Samples Verification**:
  - `benchmarks/samples/live_mixed.wav` (pure room noise floor, `rms=0.00058`): short-circuited without Whisper hallucination (`text=''`, `provider='none'`, `quality_flags=['no_speech_energy', 'very_low_input_volume', 'mostly_silence']`).
  - `benchmarks/samples/live_malayalam.wav`: flagged `quality_flags=['script_mismatch']`, `recognition_quality='low'`, `retry_triggered=True` when Whisper `small` emitted Devanagari on `ml` speech (optional local Malayalam specialized weights not installed offline yet).
