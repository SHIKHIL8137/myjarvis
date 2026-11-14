# JARVIS upgrade — phase implementation details

How each phase is built and where it plugs into the existing system. Results/status: `JARVIS_FINAL_ACCEPTANCE_REPORT.md`. Baseline: `JARVIS_UPGRADE_BASELINE.md`.

Decisions that apply to every phase: nothing was rewritten; the earlier untracked scaffolds were replaced where they duplicated existing systems (own approvals, own config) — `jarvis/first_run.py` and `jarvis/integrations/homeassistant.py` were **deleted**, `mcp_client.py`, `ptt.py`, `tray.py`, `jarvis.spec`, `scripts/*` were **rewritten**. Approvals = `ApprovalService`; audit = `audit.record_external`; untrusted/PII = `security/untrusted.py` + `pii_firewall.py`; config = `Settings`/`save_overrides` via the existing `PUT /api/v2/settings`.

## Phase 1 — MCP client
* **SDK**: `mcp` 2.3.0 installed in `.venv` (dry-run showed wheels for cp314; verified end-to-end against a local stdio server). Pinned in `requirements.txt`/`.lock`. v2 renamed `FastMCP` → `MCPServer`; tests use the new name.
* `jarvis/mcp_client.py`: `ServerRuntime` (one asyncio task owns `stdio_client` + `ClientSession`; queue of calls; timeout/cancel/crash → session recycled), `MCPManager` (config-driven registry, bounded on-demand restart, `call_tool`, `execute_approved`, `confirm_spoken`, `status`, `tool_schemas`, `sanitize`), `classify_tool` (read/write/critical).
* Config: `MCPServerSpec`/`MCPSpec` in `jarvis/config.py` (all disabled by default, names validated).
* Brain/agent integration: `Assistant.mcp`; MCP schemas are appended to the tool list in `stream_turn` (not in untrusted-isolated turns); `_execute_tool` routes `<server>.<tool>`; approval requests emit the existing `approval_requested` bus event; approvals are decided through the existing `POST /api/v2/approvals/{token}/decide`, which then runs the approved call (`mcp_result`).
* API: `GET /api/v2/mcp/servers`, `POST /api/v2/mcp/servers/{name}/{start|stop|restart}`, `GET /api/v2/mcp/presets`, `POST /api/v2/mcp/presets/{name}/install`. Settings key `mcp` allowed in `apply_settings_patch` with credential-literal rejection and masked read-back.
* UI: *Settings → MCP servers* (`IntegrationsPanel.tsx`).
* `ApprovalService` gained `list_active()`; `ACTION_RISK_TIERS` gained `mcp_read`.

## Phase 2 — Push-to-talk dictation
* `jarvis/ptt.py`: `parse_hotkey`, `HotkeyStateMachine` (hold/release, tap cancel, optional double-tap latch), `remove_fillers`, dictionary (`config/dictionary.txt`), `DictationHistory` (text-only JSONL, off by default), `Win32Clipboard` + `paste_text`, `DictationService`.
* Hotkey detection polls `GetAsyncKeyState` (no extra dependency, gives true key-up). The combo is **not swallowed**: the focused app also sees it.
* Reuses `Assistant.stt` (same `Whisper` object; `language="en"`, `save_audio=False`). Transcript goes only to paste/history, never to the bus, wake word or `stream_turn`.
* While recording: `Assistant._on_dictation_recording` stops TTS and pauses the voice loop (so it neither hears nor answers the dictation), resuming afterwards only if it was listening.
* Clipboard: previous text clipboard restored (even on error). If the clipboard holds non-text data (images, rich text/HTML) it cannot be restored faithfully, so the text is **typed** with Unicode `SendInput` and the clipboard is left untouched. In practice, clipboards copied from browsers/editors usually carry HTML, so typing is the common path.
* API: `GET /api/v2/dictation/status`, `GET|DELETE /api/v2/dictation/history`; settings key `dictation`.

## Phase 3 — Tray, wizard, health
* `jarvis/tray.py`: ctypes `Shell_NotifyIconW` tray. **Bug fixed during live testing**: window creation failed on 64-bit Windows (missing ctypes prototypes); now declared. `TrayActions` drives the real app through its HTTP API (start/stop mic, mute via `voice/session` pause/resume, dictation toggle via settings, open dashboard/logs, quit → uvicorn `should_exit`).
* `server.py`: `--tray` (default when frozen), `--no-tray`, `--no-browser`; `JarvisServer` handles SIGINT/SIGTERM/SIGBREAK without uvicorn's re-raise (graceful exit code 0); `POST /api/system/shutdown`.
* `jarvis/health.py`: read-only probes (Ollama + models, STT/Piper/wake model files, mic/speaker defaults, disk, DB migration state vs head, offline mode, providers, MCP) with the exact manual instruction for each missing model; first-run state in `data/setup_state.json`; mic level / speaker tone / wake listening helpers.
* API: `/api/v2/setup/{health,state,complete,mic-level,speaker-test,wake-test}`. (The existing `/api/v2/system/health` was left alone.)
* UI: wizard + health card + panels in `IntegrationsPanel.tsx`; App sends first-run users to Settings. Pure logic in `setupLogic.ts` (node tests).

## Phase 4 — Tool selection and intent judge
* `jarvis/tool_select.py`: IDF-weighted keyword scoring (stemming + small domain-neutral synonym map) blended with the project's embedding provider (`orchestrator.memory.vectors.embedder`; tool vectors cached; failure → keyword only). `select_tools` returns ≤ `Settings.tools_max_per_turn` (default 6) + the always-present `discover_tools` meta-tool, whose handler appends withheld matches to the live tool list for the next round. No tool names/counts are hardcoded.
* `jarvis/intent_judge.py`: fast path (stop/cancel via `intent.STOP` + extras, echo vs `Speaker.recent_text` while `EchoReferenceBuffer.is_playback_recent`, wake-word prefix strip, directed imperative/question, backchannel) → LLM only for ambiguous always-listening utterances (strict JSON, 1.5 s timeout, transcript delimited as data) → conservative fallback. Integrated in `Assistant._voice_turn`; stop → `interrupt_all`; echo/ignore dropped with a `voice_ignored` event; spoken MCP confirmation is checked after the echo test and before ignore.
* `Speaker.recent_text` added (`jarvis/tts.py`). Evaluation data/runner: `benchmarks/phase4_eval.json`, `scripts/eval_phase4.py` → `benchmarks/phase4_results.json`.

## Phase 5 — Installer
* `jarvis_desktop.spec` (relative paths, `collect_all` for native-library packages, `mcp.cli` excluded), `scripts/build-windows.ps1`, `scripts/setup.iss` (per-user, Start Menu, optional autostart task), `scripts/smoke_test.py`, `INSTALLATION.md`.
* Frozen-mode paths in `jarvis/config.py`: `RESOURCE_ROOT` (bundle: frontend, migrations, defaults) vs `ROOT` (writable `%LOCALAPPDATA%\JARVIS` or `JARVIS_HOME`), `ensure_app_home()` seeds config/notes once; `JARVIS_PORT` override. `api.py`/`health.py` read bundled resources from `RESOURCE_ROOT`.
* Windowed build: std streams guarded in `server.py`.

## Phase 6 — Home Assistant
Preset only (`mcp_presets.HOMEASSISTANT`), see `JARVIS_HOME_ASSISTANT_SETUP.md`. Critical (lock/cover/alarm) handling is the generic `critical_keywords` mechanism of the MCP manager.

## Phase 7 — Screen reading
* `jarvis/screen.py`: `ScreenReader.read_screen` (disabled → first-use approval via `ApprovalService` (`screen_first_use`) → rate limit (2 s, 6/min) → block check over **all visible top-level windows** + focused native password box → in-memory `ImageGrab` → local Tesseract via stdin/stdout → redact → `wrap_untrusted(screen_ocr)`), audit for every outcome (no titles, no text).
* `Settings.screen` (`enabled=False`, `first_use_approved=False`, editable blocklist incl. password managers, banking, sign-in); **`actions.allow_screenshots` default changed to `False`**; the legacy `take_screenshot` action now also honours the same blocklist (`ActionExecutor.screen_guard`).
* `read_screen` tool offered to the model only when enabled; approving the first-use request (existing decide endpoint) sets `first_use_approved` and captures nothing.
* Limits: browser/Electron password fields are invisible to Win32 — covered only by title heuristics ("sign in", "log in", "password", bank names…). Tesseract must be installed by the owner.

## Database
No schema change was needed. Added `tests/test_db_migrations.py` (fresh upgrade, step-by-step, parity between Alembic and the app's `create_all`, upgrade with data preserved, old DB opened by the app is *not* falsely stamped, read-only copy of the real DB). `alembic stamp` was never used.

## Round 2 stabilisation (2026-11-14)
See `JARVIS_FINAL_ACCEPTANCE_REPORT.md` section 4. Notable behaviour changes: microphone probing/diagnostics and device selection (`health.probe_input_devices`), dictation follows `audio.input_device`, Setup & Integrations is its own Settings tab, bounded MCP `stop()` and uvicorn graceful shutdown, `select_tools` offers a modest registry whole when nothing matches, filler words no longer defeat stop commands, verified `ha-mcp` command line. Earlier statements that the PC had no speech models were wrong (models are installed).
