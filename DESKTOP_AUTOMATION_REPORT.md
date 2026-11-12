# Desktop & Browser Automation Report (Stage D)

## Summary
Jarvis exposes a metadata-rich Desktop & Browser Action Registry with tiered risk levels, Pydantic argument schemas, timeouts, Win32 window/media controls, contained screenshots, opt-in input automation, and SSRF-hardened URL/browser security tiers.

## Implemented Capabilities
- **Action Registry & Metadata (`jarvis/actions.py`)**:
  - Defined `ActionSpec` and `ACTION_REGISTRY` across all `19` registered actions (`search_files`, `read_file`, `inspect_project`, `open_file`, `open_app`, `create_file`, `modify_file`, `move_file`, `delete_file`, `run_command`, `list_windows`, `get_active_window`, `focus_window`, `media_control`, `take_screenshot`, `keyboard_type`, `mouse_click`, `open_url`, `browser_action`), each declaring `description`, `level`, `risk_tier` (`Read-only`, `Low`, `Medium`, `High`), `required_permissions`, `timeout_s`, and `arg_schema`.
  - Added schema validators (`_ACTION_VALIDATORS`), `list_action_registry()`, `history(limit)`, case-insensitive `requires_confirmation()` evaluation, and `ToolOutcome` provenance on every execution.
- **Desktop Capabilities (`jarvis/actions.py`, `jarvis/config.py`)**:
  - Window enumeration, active-window inspection, and foreground focus via `ctypes.windll.user32` (`_enum_windows_win32`, `_get_active_window_win32`, `_focus_window_win32`) gated by `ActionsSpec.allow_window_controls`.
  - Media/volume key controls (`_send_media_key_win32`: `play_pause`, `next`, `previous`, `stop`, `mute`, `volume_up`, `volume_down`) gated by `ActionsSpec.allow_media_controls`.
  - Workspace-contained screenshots (`take_screenshot` into `<default_base>/captures/`) gated by `ActionsSpec.allow_screenshots` with filename sanitization and `verify_file_created(min_bytes=128)`.
  - High-risk desktop input automation (`keyboard_type`, `mouse_click`) disabled by default (`ActionsSpec.allow_input_automation=False`) and requiring explicit confirmation even when opted in.
- **Browser & URL Security (`jarvis/actions.py`, `jarvis/netsec.py`)**:
  - `open_url` validates syntax via `_validate_url_syntax` (blocking `http://`, `file://`, `localhost`, `127.0.0.1`, private IPs, and embedded `user:pass@` credentials) and elevates non-allowlisted domains or `authenticated=True` requests to `SYSTEM` confirmation.
  - `browser_action` enforces 3 security tiers (`public_read`, `open_authenticated`, and always-confirm `side_effect`) and rejects `expose_credentials=True` (`403 Forbidden`), always returning `credentials_exposed=False`.
- **APIs, Intents & HUD (`jarvis/api_v2.py`, `jarvis/intent.py`, `apps/frontend/src/components/OperationsDrawer.tsx`, `apps/frontend/src/features/SettingsPanel.tsx`)**:
  - Added `/api/v2/actions/registry`, `/api/v2/actions/execute`, `/api/v2/actions/{token}/confirm`, `/api/v2/actions/{token}/reject`.
  - Added deterministic intents (`LIST_WINDOWS`, `ACTIVE_WINDOW`, `FOCUS_WINDOW`, `MEDIA_CMD`, `TAKE_SCREENSHOT`, `OPEN_URL_CMD`, `OPEN_EXPLICIT_APP`) with honest outcome phrasing and `approvals`/`desktop` drawer directives.

## Verification
- Verified via `tests/test_stage_d_desktop_browser_actions.py`. Static typing via `mypy jarvis/actions.py jarvis/config.py jarvis/api_v2.py jarvis/intent.py` reported zero errors in those modules.

