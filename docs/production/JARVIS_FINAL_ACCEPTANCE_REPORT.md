# JARVIS upgrade — final acceptance report (round 2: machine verification and stabilisation)

Date: 2026-11-14 · Windows 11, Python 3.14.6 · Ryzen 5 5600H, GTX 1650 · Ollama 0.40.16 (`qwen3:4b-instruct`, `qwen3:4b`, `qwen2.5vl:3b`)
Nothing was committed. Pre-existing uncommitted STT work was left untouched.
Legend — **PASS** · **PARTIAL** (works, a stated part unverified/limited) · **FAIL** · **NOT TESTED**.
Evidence kinds are never mixed: *Automated* (pytest/node/scripts run here) · *Live-local* (real Windows APIs, real Ollama/Whisper/Tesseract/OpenRouter, run here) · *Browser* (real Chromium via Playwright) · *Manual* (needs you).

> **Corrections to my previous report.** (1) I wrote that this PC has no Whisper model — **wrong**: `models\whisper` contains `medium.en`, `base`, `small`, `small.en`, `distil-large-v3`; Piper `en_GB-alan-medium` and the wake models are also installed. (2) The 100 % tool-selection/intent numbers were measured on sets I had tuned against; the held-out numbers below are the honest ones. (3) "Wizard/health UI works" was not browser-verified then; it is now.

## 1. Acceptance table

| Feature | Status | Evidence | Tests | Remaining work |
|---|---|---|---|---|
| Microphone failure + device selection/diagnostics | **PASS** (root cause found, fixed in app; the test itself still fails on this PC's default device) | `jarvis/health.py` (`probe_input_devices`, `mic_diagnosis`), `GET /api/v2/setup/mic-devices`, health row, wizard input-device selector, `ptt.py` now follows `audio.input_device` | Root cause, live: Windows default input `Headset (realme Buds Air8)` opens fine but delivers **0 frames** (also for a bare `sounddevice.InputStream`); `Microphone Array (Realtek)` delivers audio. Same test body with the Realtek array as default → **passes** (live-local). Automated: 3 new tests (silent default, name/id selection, dictation device). Browser: wizard shows "Headset … — NO AUDIO", recommends the Realtek array; selecting it → "audio OK", meter endpoint returns −70.7 dB. | `tests/test_speech.py::test_real_microphone_opens_streams_and_releases` still **fails while the Bluetooth headset is the Windows default** (not weakened/skipped). Put the headset in hands-free mode or set the Realtek array as Windows default input, then re-run. |
| MCP client | **PASS** | `jarvis/mcp_client.py`, API `/api/v2/mcp/*`, UI card | Automated: 25 tests (`test_mcp_client.py`, `test_mcp_integration.py`). **Browser (isolated instance): disabled by default → Enable → Start → READY, 11 tools with read/write risk chips.** Live: audit file received `mcp_server_start`; clean shutdown left no child process. Packaged exe: MCP list empty by default. | Third-party servers untried. **Defect found & fixed:** `ServerRuntime.stop()` had an unbounded wait; an intermittent teardown hang was seen in 2 of ~13 combined runs before the fix and 0 of 8 after (not proof). |
| Server shutdown (tray Quit / `POST /api/system/shutdown`) | **PASS** (defect fixed) | `server.py` | **Defect:** an open dashboard tab (SSE stream) made uvicorn wait forever after Quit — process stayed alive. Reproduced with Playwright, fixed with `timeout_graceful_shutdown=5`, re-verified (serve returns, no process left) + regression test. | — |
| Dictation | **PARTIAL → mostly verified** | `jarvis/ptt.py` | **Live-local end-to-end:** injected Ctrl+Alt+Space (real OS key state) → shared real Whisper `medium.en` → "Um," removed, dictionary applied → typed into a real Win32 window: *"Hello JARVIS, this is a dictation test. Oh, it works."*, history written, no menu left open, 12.1 s including first model load. Automated: 19 + 3 tests. | **Substituted:** the audio was Piper speech injected instead of a human voice (the Realtek mic itself was verified separately, −53 dB room noise). Clipboard *paste+restore* not run live (your clipboard holds rich non-text data; running it would destroy it) — the typed path was exercised live. Whisper turned "Uh" into "Oh" (not removed). Hold/double-tap with a human, and Alt+Space window-menu side effects in other apps: you. |
| First-run wizard + health page | **PASS** (UI) · hardware steps PARTIAL | `IntegrationsPanel.tsx` (now its own **Setup & Integrations** tab), `jarvis/health.py` | **Browser:** auto-redirect to Settings on first run, wizard steps 1–5, health rows with real values, exact fix text for the mic. Live-local: `/setup/speaker-test` ok, `/setup/wake-test` ran 5 s on the real mic (score 0.001). Defects fixed here: redirect lost to a hash race; wizard claimed "all components found" before checks loaded; wizard opened buried in a tab. 8 node tests. | Speaker tone audibility and "Hey Jarvis" detection by a human voice: you. |
| Tray | **PASS** (live-local, earlier) | `jarvis/tray.py` | real tray icon + hidden window + menu-command dispatch; 1 automated live Win32 test | Context menu pop-up and balloon not clicked by me. |
| Screen reading | **PASS** (live-local) | `jarvis/screen.py` | **Live, real desktop + real Tesseract:** disabled → refused; enabled → `approval_required`; after approval (my own sandbox owner) real capture, 1.7–1.9 k chars, wrapped as untrusted; with a window titled "Sign in to your bank" open → **refused** ("blocked pattern 'bank'"), title not echoed or audited; after closing → works again. Audit rows correct. 18 automated tests. | Your real settings were not touched, so the real app still has screen reading **off** and approval pending. Browser password fields cannot be detected (title heuristics only). |
| Home Assistant | **PARTIAL** | preset + `JARVIS_HOME_ASSISTANT_SETUP.md` | **Package/config verified against authoritative sources** (ha-mcp README: `uvx --python 3.14 ha-mcp@latest`, `HOMEASSISTANT_URL`/`HOMEASSISTANT_TOKEN`, `readOnlyHint` annotations; HA official MCP docs). Preset args corrected (`--python 3.14 ha-mcp@latest`). Policy tests 12 pass. | **Not run against a live HA.** The README says stdio is "not recommended"; `@latest` is unpinned remote code (pin a version); first start downloads Python/packages. HA's own `/api/mcp` endpoint is HTTP+OAuth — not supported by this client. |
| Tool selection + intent | **PARTIAL — honest held-out numbers** | `scripts/eval_phase4.py`, `benchmarks/phase4_heldout.json` (32 + 34 new examples, run once before any change), `phase4_heldout_results_FIRST_RUN.json` | **Held-out, first run:** tool selection recall@6 **68.8 %** (keyword) / 65.6 % (hash embeddings), top-1 37.5 %; intent **91.2 %** (live Ollama judge; LLM latency 466–1439 ms, fast path < 0.1 ms). Dev set (tuned): 100 % / 94.7–100 %. | **Selection quality is weak on paraphrases** ("jot down", "write to", "put on"). Measured end-to-end: the local 4B model called `discover_tools` in **0 of 12** missed cases, so it cannot be relied on to recover. Mitigations shipped: a registry ≤ 3·k is offered whole when nothing matches (no regression for today's 9 built-in tools); filler words no longer defeat "okay stop"/"please be quiet" (fast stop). Known miss kept: "can you pass me the salt" → query (prefers answering over dropping a real request). **With large MCP registries, selection will hide tools — raise `tools_max_per_turn`.** Post-fix numbers are no longer independent. |
| OpenRouter | **PASS** (key/auth) · completion NOT TESTED | real call | Live: `POST /api/v2/gateway/providers/openrouter/health` → 200, 485 ms, key configured, breaker CLOSED; audited as outbound `GET openrouter.ai/api/v1/auth/key`. Mocked routing/fallback/retired-slug tests 19. | No paid chat completion was sent (your settings allow paid models; I did not spend money). |
| PostgreSQL | **NOT TESTED** | — | `JARVIS_PG_TEST_URL` is not set; something listens on :5432 but I have no credentials and did not guess. The 2 PG tests skip. | Provide a scratch URL (§3). |
| Windows installer | **PARTIAL** | `jarvis_desktop.spec`, `scripts/*`, `INSTALLATION.md` | Rebuilt after all fixes: PyInstaller OK; **smoke test PASSED** (health, frontend, MCP empty, exit code 0 via shutdown API). **New live check: the packaged exe transcribed Piper speech with `medium.en` ("The packaged build can transcribe speech.", 13.3 s)** using a throw-away home with a junction to the models (models untouched). | **Inno Setup 6 is not installed → installer .exe not built** (I did not install it; §3). Unsigned exe. |
| Security | **PASS** | security suites + new tests | Automated, as before; live: screen blocklist, offline-gated MCP, key never printed. | — |
| Database | **PASS** | `tests/test_db_migrations.py` | 8 tests on disposable DBs; health row "0011_p5_undo_journal (head …)" in the real browser and in the packaged exe. | PostgreSQL not covered. |
| Existing functionality | **PARTIAL** | whole repo | Backend: **436 passed, 1 failed, 2 skipped** (the failure = mic test above) + marker-deselected group 8 passed / 2 skipped (PG). Frontend **19/19**, production build OK. | — |

## 2. Quality gates (final)

| Check | Result |
|---|---|
| `pytest -m "not live and not mic and not integration and not pg"` | **436 passed, 1 failed (default Bluetooth mic delivers no audio), 2 skipped (Docker)** |
| `pytest -m "integration or pg or mic"` | 8 passed, 2 skipped (PG URL missing) — run earlier this round |
| Frontend `npm test` / `npm run build` | 19/19 / OK (tsc + vite) |
| Ruff `jarvis tests server.py scripts` | 36 errors, **none in any file created or modified by this work** (baseline 55) |
| mypy `jarvis` | 30 errors / 7 files, none in new code (baseline 33) |
| Packaged exe | rebuilt after the last code change; smoke test PASSED; STT inside the exe PASSED |

## 3. Exact manual steps still required from you

1. **Microphone (fixes the 1 failing test):** either put the realme Buds Air8 into hands-free/headset mode, or set *Microphone Array (Realtek)* as the Windows default input (Settings → System → Sound → Input), or select it in JARVIS (Setup & Integrations → wizard step 5, or Voice & Audio → input device). Then `pytest tests/test_speech.py`.
2. **Dictation with your voice:** Settings → Setup & Integrations → Dictation **On** (hotkey `Ctrl+Alt+Space`). Focus Notepad, hold the hotkey, say "um, hello jarvis", release. Expect the text to appear, JARVIS to stay silent, and TTS to stop while recording. Then copy plain text first and confirm it is restored after dictating; try double-tap hands-free after enabling it. Check that Alt+Space does not leave a window menu open in your usual apps.
3. **Wizard hardware steps:** step 6 speaker (do you hear the tone?) and step 7 wake word (say "Hey Jarvis" within 5 s).
4. **Screen reading (your real settings):** Setup & Integrations → Screen reading **On**; ask "what's on my screen"; approve the first-use request in Approvals; ask again; open a window titled "Sign in" and confirm refusal. Install nothing: Tesseract is already present.
5. **MCP with a real server:** add one you trust (e.g. a filesystem MCP server), Enable → Start, ask for a read (runs) and a write (approval appears).
6. **Home Assistant:** follow `JARVIS_HOME_ASSISTANT_SETUP.md`; pin `ha-mcp@<version>` after reviewing it; checklist at the end of that file.
7. **PostgreSQL:** give me (or set yourself) a *scratch* database URL, then run
   `$env:JARVIS_PG_TEST_URL="postgresql://user:pass@localhost:5432/jarvis_test"; $env:JARVIS_PG_SCRATCH_URL="postgresql://user:pass@localhost:5432/jarvis_scratch"; .\.venv\Scripts\python.exe -m pytest -m pg -q` (never point it at data you care about).
8. **Installer (needs your consent to install software):** `winget install JRSoftware.InnoSetup`, then `.\scripts\build-windows.ps1` → `dist\JARVIS-Setup-2.0.0.exe`; install it and check the Start Menu entry and the autostart checkbox.
9. **OpenRouter chat (optional, may cost money):** Settings → AI Providers → test a prompt on a `:free` model first.
10. If large MCP registries are used: raise `tools_max_per_turn` (default 6) — see the tool-selection row.

## 4. Changes made this round

Created: `benchmarks/phase4_heldout.json`, `phase4_heldout_results.json`, `phase4_heldout_results_FIRST_RUN.json`, `config/dictionary.txt` (commented sample), `docs/production/verification/*.png` (browser screenshots).
Modified: `jarvis/health.py` (mic probe/diagnosis), `jarvis/api_v2.py` (`/setup/mic-devices`), `jarvis/ptt.py` + `jarvis/assistant.py` (dictation follows `audio.input_device`), `jarvis/mcp_client.py` (bounded stop), `server.py` (bounded graceful shutdown), `jarvis/tool_select.py` + `jarvis/intent_judge.py` (see table), `jarvis/integrations/mcp_presets.py` (verified ha-mcp args), `apps/frontend/src/App.tsx` + `features/SettingsPanel.tsx` + `features/IntegrationsPanel.tsx` (own tab, redirect fix, loading state, mic selector, clipboard wording), `scripts/eval_phase4.py` (`--heldout`), `tests/test_health_tray.py`, `tests/test_tool_select_intent.py`, `tests/test_screen.py` (audit goes to a temp file — the previous version wrote test entries into your real `logs/external_audit.jsonl`; **those few `screen_capture`/test lines are still in it**), docs (`JARVIS_HOME_ASSISTANT_SETUP.md`, this report).
Side effects on your machine: while testing I briefly set and then reset `audio.input_device` in your real `data/settings.json` (it is back to null); your real `data/setup_state.json` was **not** created (wizard still shows as first-run). No packages were installed this round. Test servers on ports 4711–4713 were shut down; throw-away folders live under `%TEMP%`.

## 5. Dynamic model discovery, recommendations and in-chat switching

**Status: PASS for mocked/isolated behaviour and for a real read-only OpenRouter catalog fetch; PARTIAL overall** (OpenAI catalog and any real completion on a cloud model were NOT TESTED: no OpenAI key is configured and no paid call was authorised).

### Files changed / added
- `jarvis/model_catalog.py` (new): per-provider discovery (OpenAI `/models`, OpenRouter `/models`, installed Ollama), 10-minute TTL cache, manual refresh, stale flag, error classification (auth / rate_limited / timeout / network / offline_blocked / cloud_disabled), filters, task recommendations, validated `select()`.
- `jarvis/model_chat.py` (new): token-based understanding of model requests (list, filter, recommend, compare, current, use, "use the fastest"), typo tolerant, no fixed phrase list.
- `jarvis/intent.py`: one hook before the legacy `WHICH_MODEL`/`SWITCH_MODEL` rules (they still handle aliases such as "fast", "reasoning").
- `jarvis/assistant.py`: `self.model_catalog`. `jarvis/api.py`: `GET /api/models/catalog`, `GET /api/models/recommend`, `POST /api/models/select`.
- Frontend: `components/ModelPicker.tsx` (new), `features/AssistantHome.tsx` (model chip opens the selector; selector renders inline under a chat reply), `components/VoiceDock.tsx`, `styles/app.css`.
- Tests: `tests/test_model_catalog.py` (31 tests). QA tools: `scripts/qa_model_server.py`, `scripts/real_catalog_check.py`.

### Existing services reused (nothing duplicated)
- Active model/provider state is still only `Brain.current`; switching goes through `Brain.switch`. The `model` bus event that already existed refreshes the UI indicator.
- Credentials: `config.get_provider_api_key`/`has_provider_api_key` (never returned, logged or sent to the browser). Base URLs: `settings.providers.*` (never taken from chat input).
- Network: `netsec.safe_request` (https only, DNS/SSRF checks, host pinned to the configured base URL, Local-Mode refusal). Audit: `audit.record_external` for each catalog fetch and each switch. Paid rule: `gateway.providers.is_paid_model`-style semantics (OpenRouter `:free` suffix or zero price).

### Behaviour
| Item | Result |
|---|---|
| Discovery only from provider data; no invented names | PASS (tests; real OpenRouter fetch: 436 models, 17 free) |
| OpenAI price/context/capabilities | Not provided by `/models`, so shown as "not provided"; never guessed |
| Cache, TTL, manual refresh, stale warning, no per-message calls | PASS (tests + browser) |
| Failure after a good fetch keeps the list but flags it STALE | PASS |
| 401/403, 429, timeout, network error: classified, no secret in output | PASS (tests; browser via mock) |
| Rejected credential blocks switching from a stale cache | PASS |
| Local Mode / internet off: zero network calls | PASS (test counts 0 calls; browser shows `offline_blocked`) |
| Paid model: nothing switches until explicit confirmation; pricing shown | PASS (API, chat, browser) |
| Invalid / unavailable / URL-like identifiers rejected | PASS |
| Conversation preserved across a switch | PASS |
| Recommendations only from metadata (vision needs image modality; speech/long-docs need evidence); name-based hints are labelled `[name hint]`; negative OpenRouter prices (variable routers) treated as unknown | PASS (a real-data run found the negative-price bug; fixed + regression test) |

### Browser results (isolated server, mock cloud transport, real Ollama and real UI)
Screenshots: `docs/production/verification/model_selector_coding.png`, `model_selector_offline_mobile.png`.
| # | Case | Result |
|---|---|---|
| 1-3 | Ask "Show available models." in chat; list with provider, price, context, capabilities, current marker | PASS |
| 4-7 | Provider filter, free filter, search (OpenRouter + free + "coder" -> 1 model) | PASS |
| 8-9 | Coding and translation recommendations with reasons (coding verified in browser; translation covered by tests) | PASS / PARTIAL |
| 10-12 | Select a free OpenRouter model; indicator changes; next message reached the mock provider with `model=deepseek/deepseek-chat:free` | PASS |
| 13 | "Use GPT-4.1": refused until confirmed; Confirm button then switch; next message routed to `gpt-4.1` | PASS |
| 14 | "Switch back to local Ollama" | PASS |
| 15 | Unavailable model | PASS (API) |
| 16 | Disconnected provider | PASS (tests only; not in browser) |
| 17-18 | Invalid credential, rate limit, timeout, network down | PASS (API, with mode flipped on the mock) |
| 19 | Offline-enforced instance | PASS |
| 20 | Browser refresh keeps provider/model (state lives in the backend) | PASS |
| - | Phone width (390 px): no horizontal overflow | PASS (the surrounding two-column dashboard is still cramped at that width; pre-existing) |
Console: only the pre-existing `/favicon.ico` 404.

### Real provider checks
- OpenRouter public catalog (read-only, no key, no cost): PASS, 436 models, 17 free.
- OpenAI catalog: NOT TESTED (no key). Real completion routing on any cloud model: NOT TESTED (no paid or keyed call authorised). The routing evidence above comes from a mock provider that echoes the model id it received.

### Remaining limitations
- Only OpenAI, OpenRouter and Ollama have catalog adapters; other providers return `unsupported` (add an adapter in `ModelCatalog`).
- Recommendations are metadata/name-hint heuristics, not benchmarks. Translation quality in particular has no provider metadata.
- The selected model is process state: it resets to the configured default when the backend restarts (same as before this feature).
- Chat requests mentioning "models" together with a list/recommend verb are treated as model management; a very unusual phrasing may fall through to the normal assistant.

### Regressions caught by the full backend suite and fixed (this feature round)
- `How much VRAM do I need for a local model?` was treated as "list models" (a provider word plus "model" was enough). The list trigger now needs an explicit list verb, a plural "models" with what/which, or a short terse filter; questions about models are ignored. Regression test added.
- Short on-topic queries (`versus quantization`, from speech) were rejected by the new memory grounding gate; one distinctive (8+ letter) shared word now suffices for queries of two words or fewer. Regression test added.
- Backend: full suite on the pre-fix tree was 507 passed / 2 failed / 4 skipped (the 1 microphone test deselected: it fails on this machine's audio device). Both failures were re-run after the fixes and pass; the full suite was not re-run after the fixes.
