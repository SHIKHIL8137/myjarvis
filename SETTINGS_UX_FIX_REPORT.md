# Jarvis Settings Simplification & Audio/Video Fix Report

## 1. Root Cause Analysis (Audio & Video & Settings Issues)

Inspection of the existing Settings frontend (`apps/frontend/src/features/SettingsPanel.tsx`) and backend (`/api/v2/settings/devices` in `jarvis/api_deps.py`) revealed three underlying defects:

1. **Missing host audio device lists in `/api/v2/settings/devices` (`jarvis/api_deps.py`)**:
   - `list_settings_devices()` previously called `sysinfo.audio()`, which returned only `{input, output}` display strings without `inputs` / `outputs` arrays or error isolation.
   - As a result, `Speaker Output Device` and `Microphone Selection` dropdowns in Settings showed only `System Default`.
2. **`output_device` schema mismatch in `SettingsPanel.tsx`**:
   - The frontend read and updated `cfg.audio.output_device` instead of `cfg.tts.output_device` (whereas backend `AudioSpec` only stores `input_device` and `TTSSpec` stores `output_device`), causing speaker output selection state and updates not to bind to the configured TTS output field.
3. **Absent safe browser media device handling & UI error isolation**:
   - No `Audio & Video Devices` diagnostics UI existed, and no utility existed to safely handle missing `navigator.mediaDevices` / `getUserMedia`, rejected `enumerateDevices()` promises, explicit-only media stream acquisition (zero permission requests on tab load), guaranteed track cleanup across stop/unmount/`pagehide`, or React ErrorBoundary containment.

*Operational note*: During verification, an already-running `server.py` process on port `4700` was serving an older backend/frontend build without updated `/api/v2/settings/devices` or `apps/frontend/dist`. Restarting `server.py` is required for port `4700` to serve the updated backend and built `apps/frontend/dist` bundle.

## 2. Files Changed

- `jarvis/api_deps.py` — Added `_safe_audio_inventory()` returning `{available, input, output, inputs: [{index, name}], outputs: [{index, name}], error}` with `sounddevice` error isolation and MME host API filtering (with fallback), wired into `list_settings_devices()`.
- `apps/frontend/src/mediaDevices.ts` — Created browser media helper module exporting `getMediaCapabilities()`, `safeEnumerateMediaDevices()` (never calls `getUserMedia()`), `queryBrowserPermissionState()`, `mapMediaError()` (`permission_denied`, `no_device`, `device_busy`, `overconstrained`, `insecure_context`), and `stopMediaStream()`.
- `apps/frontend/src/mediaDevices.test.ts` — Added 5 unit tests covering missing `mediaDevices`, missing `getUserMedia`, rejected `enumerateDevices()`, hidden labels, zero `getUserMedia` calls during enumeration, `mapMediaError()`, and `stopMediaStream()` track stopping.
- `apps/frontend/src/components/AudioVideoPanel.tsx` — Added `SettingsErrorBoundary` (surfaces errors with error stack logging and Retry without blanking the app) and `AudioVideoPanel` (Host Desktop Audio Hardware inventory + Browser Microphones & Cameras + `Refresh Devices` + explicit `Test Microphone` with live `AudioContext`/`AnalyserNode` meter + `Test Camera` preview + `Stop Mic Test` / `Stop Camera Test` + cleanup on unmount and `pagehide` + explicit `Camera AI Vision: Not Implemented Yet` pill).
- `apps/frontend/src/features/SettingsPanel.tsx` — Preserved strictly the 4 existing tabs (`AI Providers & Routing`, `Voice & Audio`, `Permissions & Privacy`, `System & Data`) and dark HUD layout; added plain-English descriptions in `AI Providers & Routing`; reorganized `Voice & Audio` into clear subsections (`Recommended Settings`, `Speech Recognition`, `Translation`, `Speech Output`, `Listening`, `Audio & Video Devices`); fixed `cfg.tts.output_device` binding; wrapped each tab section and `<AudioVideoPanel />` in `<SettingsErrorBoundary>`.
- `apps/frontend/src/styles/app.css` — Added `.field-hint` and `.recommended-banner` styles matching the existing CSS tokens.
- `tests/test_p10_20_manager_and_fs_security.py` — Added `test_p10_20_settings_devices_inventory_and_output_device_patch` covering `/api/v2/settings/devices` device inventory, `sounddevice.query_devices` failure resilience, and `PUT /api/v2/settings` persistence for `tts.output_device`.

## 3. Settings UX Improvements Added

### AI Providers & Routing
Added concise explanations for technical controls while preserving existing routing behavior, providers, and controls:
- **Active Local Brain**: model used for normal Jarvis conversations.
- **Active Gateway Provider**: service used to connect to the selected model.
- **Fast Model Role**: model role for quick responses.
- **Advanced Reasoning Model Role**: model role for complex reasoning.
- **Coding Model Role**: model role for coding tasks.
- **Embedding Model**: converts text into vectors for semantic search and memory.
- **Fallback Provider**: backup provider if the active provider fails.
- **Context Window**: conversation history token limit per request.
- **Request Timeout**: max wait time before failing a slow model request.

### Voice & Audio
Reorganized inside the existing `Voice & Audio` tab (keeping speech/translation behavior unchanged and without adding OpenCode or extra providers):
- **Recommended Settings** summary banner (`Speech Recognition` = `Balanced`, `Language` = `Auto-detect`, `Audio Preprocessing` = `Light`, `Translation` = `Transcribe only`, `Suppress Mic During TTS` = `On`, `Native Mic Sample Rate` = `On`).
- **Speech Recognition**: `STT Provider`, `STT Model`, `Language Selection & Auto Language Detection`, `Recognition Quality Preset`, `Audio Preprocessing` — each with short explanation + recommended setting.
- **Translation**: explicitly notes that transcription and translation are separate operations (`Translation Mode: Transcribe only vs Translate`, `Target Language`).
- **Speech Output**: `TTS Provider`, `Piper Voice`, `Speaker Output Device` (fixed to bind `cfg.tts.output_device`), and `Spoken Replies` toggle.
- **Listening**: `Listening Mode (Continuous / Wake Word / Push to Talk)`, `Microphone Selection (Host Input Device)`, `Voice Barge-in`, `Suppress Mic During TTS`, and `Native Mic Sample Rate (Audio Capture)`.
- **Audio & Video Devices**: host audio hardware status, passive browser mic/camera device listing, `Refresh Devices`, explicit `Test Microphone` and `Test Camera` controls, track cleanup on stop/unmount/`pagehide`, and explicit `Camera AI Vision: Not Implemented Yet` badge.

## 4. Verification Summary

### Automated Checks
- **Frontend Unit Tests (`cd apps/frontend; npm test`)**: **12 passed, 0 failed** (`node --test "src/**/*.test.ts"`), including all 5 new `mediaDevices.test.ts` tests.
- **Frontend Typecheck & Production Build (`cd apps/frontend; npm run build`)**: `tsc --noEmit && vite build` exited `0`, transforming `45` modules (`dist/assets/index-C-oelyKs.js` `290.18 kB`, `dist/assets/index-c9dVwR21.css` `25.72 kB`).
- **Backend Lint & Typecheck**:
  - `.\.venv\Scripts\ruff.exe check jarvis/api_deps.py tests/test_p10_20_manager_and_fs_security.py` -> `All checks passed!`
  - `.\.venv\Scripts\mypy.exe --follow-imports=silent jarvis/api_deps.py` -> `Success: no issues found in 1 source file`
- **Backend API, Settings, Continuous Voice & Speech Suite**:
  - `.\.venv\Scripts\pytest.exe -q -rs tests/test_p10_20_manager_and_fs_security.py tests/test_api.py tests/test_continuous_voice.py tests/test_speech_pipeline_upgrade.py tests/test_speech.py` -> **68 passed, 0 failed** (`31.92s`), including `test_p10_20_settings_devices_inventory_and_output_device_patch` and live microphone tests.

### Headless Chrome E2E Verification (`#/settings` on isolated FastAPI/Uvicorn port `4799`)
- **All 4 Settings Tabs & Zero Permission Request on Open**:
  - Navigated repeatedly across `Voice & Audio` -> `Permissions & Privacy` -> `System & Data` -> `AI Providers & Routing` -> `Voice & Audio` with **0 console or page errors**.
  - Verified host dropdowns populated (`Speaker Output Device`: `4` options; `Microphone Selection`: `4` options), passive browser mic/camera selects populated without invoking `getUserMedia()`, `Recommended Settings` banner rendered, and `Camera AI Vision: Not Implemented Yet` pill visible.
- **Browser Mic & Camera Stream Lifecycle (`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream`)**:
  - Verified `0` streams opened on tab load before explicit button click (`streams: 0`).
  - Clicked `Test Microphone` -> `1` active audio track opened, button updated to `Stop Mic Test`, `0` error alerts.
  - Clicked `Test Camera` -> camera video track opened (`liveTracks: 2`, `<video>` `srcObject` attached), `0` error alerts.
  - Switched away to `JARVIS ASSISTANT` -> `liveTracks` dropped immediately to `0` (`audio:ended`, `video:ended`).
  - Reopened `SETTINGS` -> `Voice & Audio` -> `liveTracks: 0`, all 5 headings rendered with `0` console/page errors.

## 5. Remaining Limitations

- **Camera AI Vision**: Deliberately not implemented in this phase; explicitly marked `Camera AI Vision: Not Implemented Yet` in the UI (`AudioVideoPanel.tsx`).
- **MediaError live browser branches**: Physical hardware errors (`NotAllowedError` / `NotFoundError` / `NotReadableError`) were verified via `mapMediaError()` unit tests in `mediaDevices.test.ts`; headless Chrome fake-device mode tests successful stream creation and cleanup rather than OS hardware busy locks.
- **Existing Server on Port `4700`**: Any pre-existing `server.py` process started before these edits must be restarted so it serves the updated `jarvis/api_deps.py` and `apps/frontend/dist`.

