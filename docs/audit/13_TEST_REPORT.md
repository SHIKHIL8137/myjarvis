# 13 — Audit Verification & Test Execution Report

> **Audit Scope & Integrity Statement**: Zero source files, configs, tests, or DB schemas were altered during this audit. All numbers below come directly from commands executed during this audit session on `2026-11-11`.

---

## 1. Automated Python Suite (`pytest -v`)

**Command**: `.\.venv\Scripts\pytest.exe -v` (with `pytest.ini` configuring `-ra --strict-markers`, Python `3.14.6`, `pytest 9.2.0`)
**Result**: **132 passed in 170.26s (0:02:50)** — exit code `0`, **0 failed**, **0 skipped**.

| Test File | Tests Executed & Passed | Coverage Focus Verified |
| :--- | :---: | :--- |
| `tests/test_api.py` | **20** | Health, chat, SSE streaming order, history, model switching, notes CRUD & graph, config validation, path traversal/private-file block, zero-CDN viewer check |
| `tests/test_brain.py` | **6** | `ThinkFilter` tag stripping across chunk boundaries, Ollama tool call parsing, unload on switch |
| `tests/test_continuous_voice.py` | **16** | Continuous 2-turn loop, stop/pause/resume, barge-in interrupt, half-duplex TTS mic suppression, error recovery (disconnect/Whisper/Ollama/Piper), stream cleanup, Bluetooth/NLMS AEC |
| `tests/test_core.py` | **13** | Default config, overrides merge, graph index links, TF-IDF retrieval, JSONL history torn-line tolerance, persona budget |
| `tests/test_intent_and_tasks.py` | **4** | `TaskManager` lifecycle/cancel, ambiguous switch clarification, PTT config isolation |
| `tests/test_internet_security.py` | **25** | Local mode socket block, SSRF private/CGNAT/loopback/port checks, DNS rebinding, redirect validation, `_PinnedTransport`, download byte limit, provider cache/robots, key redaction |
| `tests/test_learning_and_training.py` | **6** | Knowledge versioning/rollback & offline block, feedback lifecycle, dataset poisoning/license/leakage checks, training estimator/lifecycle/eval/registry/rollback, learning API |
| `tests/test_live.py` | **8** | Live Ollama `127.0.0.1:11434` connectivity (`@pytest.mark.live`), streaming identity, multi-turn context, model switch unload, thinking leak check, E2E wake→hear→think→speak, loopback binding check, Qwen translation/tool-calling |
| `tests/test_memory_and_filesystem.py` | **5** | Personal memory categories, secret rejection, backup/restore, disk history search, safe filesystem & action tiers |
| `tests/test_models_and_voices.py` | **5** | Piper voice discovery/switching/rate, cloud adapter offline block & netsec gating, alias resolution |
| `tests/test_multilingual_stt.py` | **7** | Bandlimited resampling, local Whisper discovery, EN/ML/HI routing, short VAD recovery, translation preserving original transcript |
| `tests/test_preflight.py` | **4** | VRAM parse, free RAM sanity, config key hygiene, sample notes |
| `tests/test_speech.py` | **13** | `clean_for_speech`, sentence chunking, Piper TTS synth/cache, Whisper roundtrip, openWakeWord `hey_jarvis`, real hardware playback & mic release (`@pytest.mark.mic`) |
| **Total** | **132** | **All 132 passed (0 failures, 0 skips)** |

---

## 2. Frontend, Dependency & Static Checks

| Verification Check | Command Executed | Observed Result | Findings / Gaps |
| :--- | :--- | :--- | :--- |
| **JS Syntax Validation** | `node --check viewer/js/*.js` | **6/6 passed (`exit=0`)** | All 6 custom ES modules (`api.js`, `galaxy.js`, `learning.js`, `main.js`, `md.js`, `panels.js`) parse cleanly. |
| **Virtualenv Integrity** | `.\.venv\Scripts\python.exe -m pip check` | **Passed** | `No broken requirements found.` |
| **Dependency Manifest Alignment** | AST third-party import scan vs `requirements.txt` & `requirements.lock` | **Mismatch flagged** | `requirements.txt` contains UTF-8 BOM (`\ufeff`) and omits direct imports (`pydantic`, `scipy`, `requests`) present only in `requirements.lock` (`55` pinned packages). Optional ML/AEC imports (`torch`, `peft`, `transformers`, `speexdsp`, `webrtc_audio_processing`) are lazily guarded. |
| **Static Security Regex Scan** | Python scan across `server.py`, `jarvis/**/*.py`, `viewer/js/*.js` | **Clean baseline** | 0 `shell=True`, 0 `os.system`, 0 non-vendor `eval`/`exec`, 0 `verify=False`, 0 `pickle.load`/`yaml.load`, 0 SQL f-strings, 0 non-local plaintext `http://` runtime endpoints (`main.js` SVG namespace only). |
| **Live Server & Ollama Health** | `GET http://127.0.0.1:4700/api/health` | **200 OK** | Verified live server (`status=ok`, `ollama 0.40.16`, models `["qwen3:4b-instruct", "qwen2.5vl:3b", "qwen3:4b"]`, `notes=30`, `mode="local"`). |
| **Target CLI Tools Probe** | `opencode --help`, `graphify --help`, `Get-Command` | **Ready / Partial** | **Installed**: `opencode 1.19.5`, `graphify 0.9.80`, `git`, `node`, `npm`, `docker`, `ollama`. **Missing**: `psql`, `ruff`, `mypy`. |

---

## 3. Checks Blocked or Not Executed During Audit (By Constraints / Missing Tooling)

| Check / Tool | Status | Reason |
| :--- | :--- | :--- |
| **`ruff` / `mypy` / `tsc` / `vitest`** | **Not Run (Unavailable)** | `ruff`, `mypy`, and React/Vitest toolchain are not installed in this codebase/environment yet. |
| **PostgreSQL / `pgvector` / Alembic checks** | **Not Run (Not Built)** | `psql`, `sqlalchemy`, `alembic`, `psycopg`/`asyncpg`, and `pgvector` do not exist in codebase or host. |
| **Live External Cloud LLM / Search Runs** | **Not Executed** | Deliberately skipped during this audit-only phase because zero cloud provider API keys exist in OS `.env*`, keeping host strictly inside Local Mode. |
| **Frontend Runtime Browser Suite** | **Not Executed in Audit** | No live Playwright runner executed in this phase; historical screenshots/logs exist in `.playwright-mcp/` from prior sessions. |
