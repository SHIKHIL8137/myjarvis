# JARVIS Upgrade Baseline (Phase 0)

Date: 2026-11-13 · Branch `main` @ c26a6ae + uncommitted work · Python 3.14.6 · Windows 11
Nothing was implemented in this phase. No commits were made.

## 1. Test baseline (actual results)

| Check | Result | Evidence |
|---|---|---|
| Backend pytest (`-m "not live and not mic and not integration and not pg"`) | **298 passed, 2 skipped, 10 deselected**, 209 s | Skips: Docker daemon not running (2). Deselected: live/mic/integration/pg markers — **NOT TESTED** in this run |
| Frontend `npm test` | **12/12 pass** | node --test |
| Frontend `npm run build` (tsc + vite) | **PASS** | 47 modules |
| Ruff (`jarvis tests server.py`) | **FAIL: 55 errors** (45 auto-fixable) | Mostly in tests; 13 are in the new untracked files (`first_run.py` 5, `tray.py` 4, `mcp_client.py` 4) |
| mypy (`jarvis`) | **FAIL: 33 errors in 9 files** | e.g. `assistant.py:4044` `str | None` → `str` |
| Alembic | `current` = `heads` = `0011_p5_undo_journal` on the default DB | Fresh-DB and upgrade-from-older validation **NOT TESTED** yet (existing tests `test_p5…`, `test_p10_…` touch migrations; I did not audit their coverage) |
| Ollama | Running; `qwen3:4b-instruct` present | `/api/tags` |
| OpenRouter live | **NOT TESTED** (no key exercised) | |

Environment gaps: official `mcp` SDK, `pystray`, `pynput`, `pyperclip`, `pyinstaller` are **not installed** in `.venv`. Tesseract (`C:\Program Files\Tesseract-OCR`) is on PATH; `rapidocr` is installed system-wide (not confirmed in `.venv`). Inno Setup (`iscc`) is not installed.

## 2. Key finding: prior scaffolding exists, untracked and unwired

The working tree already contains untracked, unreviewed code for Phases 1, 2, 3, 5, 6. None of it is imported by `api.py`, `api_v2.py`, `assistant.py`, `brain.py` or `server.py` (grep confirmed), and only `tests/test_ptt.py` exists for it.

| File | Lines | State |
|---|---|---|
| `jarvis/mcp_client.py` | 292 | Stdlib JSON-RPC stdio client (`MCPSession`, `MCPClient`). **No** config model, disabled-by-default gating, allowed-tools list, timeouts config, approval hook, audit, untrusted/PII filtering, or brain integration (`grep approval|audit` → nothing). No tests. |
| `jarvis/ptt.py` | 322 | ctypes global hotkey + clipboard backup/paste. Not wired to the existing STT instance in the assistant; no filler removal / dictionary / history / TTS pause visible. Tests: `tests/test_ptt.py` (passes in the 298). |
| `jarvis/tray.py` | 390 | ctypes tray. Not launched from `server.py`. No tests. |
| `jarvis/first_run.py` | 240 | Backend checks only; no API route, no React wizard. No tests. |
| `jarvis/integrations/homeassistant.py` | 120 | Own approval logic (`SAFE_READ_TOOLS` frozenset, hardcoded tool names) — **duplicates** `jarvis/agents/approvals.py`; spoken confirmation absent. Imports from `mcp_client`. No tests. |
| `jarvis.spec`, `scripts/build-windows.ps1`, `scripts/setup.iss` | 43/45/21 | Draft only. Spec hardcodes `D:/projects/myjarvis`; bundles the whole `jarvis/` dir as data; build script runs the *whole* test suite and silently skips PyInstaller if missing. Spec name differs from the requested `jarvis_desktop.spec`. Never built. No smoke test. |

Decision needed (see §7): treat these as drafts to harden, or discard and rewrite. Default plan: harden — they are consistent with the instructions' "reuse, don't rebuild".

## 3. Existing capabilities (verified present in code)

- **Config**: `jarvis/config.py` (`offline_enforced=True` default; `internet.enabled`; providers; OpenRouter base URL/model).
- **Offline/audit**: `jarvis/audit.py` (`audit.install(enforce=…)`, `is_online_enabled`), `jarvis/netsec.py`.
- **Security**: `security/untrusted.py` (`wrap_untrusted`, spoof-delimiter detection), `pii_firewall.py` (`redact_secrets`, `scrub_messages`), `secrets.py`, `outcomes.py`.
- **Approvals**: `agents/approvals.py` `ApprovalService` (`create_request`, `decide`, `consume_approved`, TTL, DB-backed).
- **Model gateway**: `gateway/gateway.py`, `providers.py` (OpenRouter `:free` slug fallback exists), `interface.py`, `sandbox_proxy.py`. Latest commit c26a6ae isolates Ollama from cloud models.
- **Runtime registry**: `core/runtime_registry.py` + `docs/production/JARVIS_AGENT_RUNTIME_REGISTRY.md`.
- **Agents/orchestration**: `agents/` (orchestrator, multistep, task_runner, profiles, research), `departments/assistant/routines.py`.
- **Memory**: `memory/` (embeddings with `hash_embed` fallback, pgvector, five_way), `memory_store.py`, personal memory tables.
- **Voice**: `stt.py`, `asr_providers.py`, `audio_preprocess.py`, `voice.py`, `wakeword.py`, `tts.py`, `aec.py` (NLMS canceller, `EchoReferenceBuffer`), `intent.py` (975 lines, rule-based routing).
- **Workflows**: `/assistant/briefing` and routine CRUD/run/poll endpoints in `api_v2.py`; proactive engine in `proactive.py`.
- **Screenshot (capture only)**: `actions.py` `take_screenshot` via `PIL.ImageGrab`, gated by `allow_screenshots` (defaults **True**), saved to `captures/`. **No OCR, no window/password-field blocking, no first-use approval, no untrusted wrapping.** Tesseract/rapidocr are installed but unused by code.
- **DB**: Alembic 0001–0011, `db/engine.py`, `db/models.py`.
- **Frontend**: React/Vite; components `OperationsDrawer`, `SpeechInspectorPanel`, `VoiceDock`, etc. 12 unit tests (graph, media devices, speech status only).

## 4. Missing capabilities

1. MCP: config, lifecycle supervision, tool exposure `<server>.<tool>`, brain/agent integration, approvals, audit, sanitization, API + UI. SDK not installed.
2. Tool selection (`tool_select.py`): none exists (`grep select_tools` → nothing). Embedding + keyword building blocks exist.
3. Intent judge: `intent.py` is routing for *what* to do; no TTS-echo/stop/directed-at-JARVIS classifier with a fast path found. `aec.py` provides echo primitives to reuse.
4. Dictation: history, settings UI, dictionary file, filler removal, STT instance sharing.
5. Tray + setup wizard + system health page (UI).
6. Installer: working spec, build, smoke test, `INSTALLATION.md` (a root `INSTALLATION.md` already exists — content not yet reviewed against the new flow).
7. Home Assistant preset via the MCP config, spoken confirmation.
8. `read_screen` with OCR and protections.
9. Frontend tests beyond 3 areas; no tests for any new feature.

## 5. Bugs and architectural risks

- **Dirty tree**: 13 modified + 17 untracked files are uncommitted (STT accuracy work + the scaffolds). Risk of mixing phases; suggest committing the STT work separately when you choose.
- **Ruff 55 / mypy 33 failures pre-exist**; the instructions require reporting honestly, so Phase gates will use "no *new* errors" plus fixing those in touched files, unless you want the whole backlog cleared first.
- `allow_screenshots=True` default conflicts with "opt-in, approval on first use" for Phase 7.
- Home Assistant draft hardcodes tool names and has its own approval path — violates "no duplicate approval systems" and "no hardcoded capabilities".
- `jarvis.spec` bundles source as data and uses an absolute path; PyInstaller on Python 3.14 + `faster-whisper`/`onnxruntime`/`openwakeword` is **unverified** and may fail.
- Python 3.14 compatibility of the official `mcp` SDK is **unverified** (not installed; I have not tried `pip install`). The stdlib client in `mcp_client.py` is the fallback the instructions allow, but needs the test suite required by Phase 1.
- Alembic `current` was checked only on the default DB; `docs/migrations` guide not re-verified. No `alembic stamp` was or will be used.
- Many P10 reports in the repo root assert completion; I did not re-verify them. Treat claims there as **unverified** unless a test above covers them.
- Settings-page fix (OpenRouter/Ollama separation, c26a6ae): code present in `config.py`/`providers.py`; there are test files for models/voices, but I have not yet confirmed a test for "settings load with Ollama offline" — to be checked in the Model routing acceptance work.

## 6. Acceptance-criteria status (initial)

| Area | Status |
|---|---|
| Runtime discovery tests (installed/missing/unavailable/unauthenticated) | NOT TESTED (module exists; coverage not audited) |
| Model routing (fast/advanced/coding, Ollama vs OpenRouter, fallback, retired IDs) | PARTIAL (code exists; coverage not audited) |
| Memory CRUD/provenance/restart persistence | PARTIAL (tests exist in 298; mapping not audited) |
| Security (injection, file access, approvals, MCP malicious output, PII) | PARTIAL (non-MCP exist; MCP none) |
| DB fresh + upgrade migrations | NOT TESTED |
| Personal workflows (briefing, routines) | PARTIAL (`test_p4_routines_and_proactive.py` passes); calendar/email connectors: only documented in `JARVIS_CONNECTOR_CATALOG.md`, not verified |
| Phases 1–7 | Phase 1/2/3/5/6 scaffolds only; 4 and 7 not started |

## 7. Proposed implementation sequence

Follows your order. Each phase ends with tests, doc update, and a stop for approval.

1. **Phase 1 MCP** — harden `mcp_client.py` (config in `Settings`, supervisor/restart, per-server timeouts, allow-list, approvals via `ApprovalService`, audit, `wrap_untrusted` + `redact_secrets`, brain registration); API routes + UI panel; tests with a local fake stdio MCP server. Try `pip install mcp` in a throwaway check first; keep stdlib client if incompatible.
2. **Phase 2 PTT** — wire `ptt.py` to the shared STT, add dictionary/filler/history/settings, TTS pause.
3. **Phase 3 Tray/first-run/health** — wire tray into `server.py`, API routes for `first_run`, React wizard + health page.
4. **Phase 4 tool_select + intent judge** — new module, 30+30 labelled evals with measured accuracy/latency.
5. **Phase 5 Installer** — `jarvis_desktop.spec` (rename/rewrite), build script, smoke test; verify PyInstaller on 3.14 actually works.
6. **Phase 6 Home Assistant** — rebuild as an MCP preset on Phase 1 config; remove duplicate approval logic.
7. **Phase 7 read_screen** — new opt-in capability using existing `ImageGrab` + local OCR; flip default of `allow_screenshots`.

Cross-cutting acceptance tests (runtime discovery, routing, DB migrations, memory restart, security) are scheduled at the end of the phase they touch, and consolidated in the final report.

## 8. Questions for you before Phase 1

1. Harden the existing untracked scaffolds (recommended) or rewrite from scratch?
2. Ruff/mypy backlog: only keep touched files clean (recommended), or clear all 55/33 first?
3. May I `pip install mcp` (and later `pystray`/`pyinstaller`) into `.venv`? Per your rules I will not install anything without approval.
4. Should the pending STT-accuracy changes be committed separately by you first, so phase diffs stay clean?

---

## Progress log (updated 2026-11-14)

All seven phases were implemented in one pass at the owner's request (the original "stop after each phase" rule was superseded by the later instruction). Details: `JARVIS_PHASE_IMPLEMENTATION_DETAILS.md`; verified status per feature: `JARVIS_FINAL_ACCEPTANCE_REPORT.md`.

| Phase | State |
|---|---|
| 1 MCP client | Implemented on the official `mcp` 2.3.0 SDK; 25 automated tests; third-party servers not tried |
| 2 Dictation | Implemented; 21 automated tests; live hotkey/paste not tested (no Whisper model on this PC) |
| 3 Tray / wizard / health | Implemented; tray verified live (found + fixed a 64-bit ctypes bug); wizard UI not eyeballed |
| 4 Tool selection + intent judge | Implemented; 33 + 38 labelled examples; recall@6 100 %, intent 100 % (dev sets, live Ollama judge 425–909 ms) |
| 5 Installer | PyInstaller build + smoke test pass; Inno Setup installer **not built** (not installed) |
| 6 Home Assistant | Preset + policy tests pass; no live HA |
| 7 Screen OCR | Implemented, off by default; real Tesseract verified; live screen capture not run |

Baseline questions answered: scaffolds were hardened/rewritten (two deleted as duplicates); lint cleaned only in touched files (ruff 55 → 36, mypy 33 → 30, none left in touched code); `mcp` and `pyinstaller` installed into `.venv` only; the STT work was left uncommitted and untouched.

Baseline test numbers vs now: backend 298 → 431 passed (1 environment failure: the default Bluetooth microphone delivers no audio even outside JARVIS), frontend 12 → 19, DB migration validation added (was NOT TESTED).
