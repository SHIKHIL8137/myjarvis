# JARVIS Security Audit Report (Stages A–G)

## 1. Executive Summary

All intelligence, filesystem, multi-step orchestration, memory, desktop/browser automation, system monitoring, proactive suggestion, voice safety, and personality features added in Stages A–F were implemented directly on top of Jarvis's existing security boundaries (`FilesystemAccessManager`, `pii_firewall`, `ActionGuard`, `ToolOutcome` / `HONEST_OUTCOME_RULE`, and `UNTRUSTED_DATA_POLICY`). High-risk automation remains opt-in (`allow_input_automation = False` by default), external content stays wrapped as untrusted data, secrets are scrubbed prior to cloud egress, and uncertain voice commands never execute state-mutating actions automatically.

## 2. Filesystem Containment & Secret Redaction

- **Module**: `jarvis/filesystem_access.py`, `jarvis/db/migrations/versions/0007_p11_project_aliases.py`
- **Granular 9-Operation Separation**: `FilesystemAccessManager` separates `READ_OPS` (`list_directory`, `read_file`, `search_files`, `read_metadata`), `WRITE_OPS` (`create_file`, `write_file`, `edit_file`), `EXECUTE_OPS` (`execute_in_directory`), and `DELETE_OPS` (`delete_file`). Read permission never implies write, execute, or delete; write never implies execute or delete.
- **Path & Reparse Hardening**:
  - Canonical normalization blocks UNC paths, Windows device names (`_WINDOWS_RESERVED_NAMES`), drive roots, system directories, browser profiles, symlink/reparse traversal, and TOCTOU races immediately prior to I/O.
  - Sensitive credential directories (`_SENSITIVE_DIR_FRAGMENTS`, including `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`, `.config/gh`) are denied.
  - Executable/script write extensions (`_BLOCKED_WRITE_EXTENSIONS`, e.g. `.exe`, `.dll`, `.msi`, `.ps1`, `.bat`, `.js`) are blocked.
  - Temporary `"once"` grants bind to exact folder + operation + `task_id` and are consumed at task end via `consume_task_once_permissions`.
- **Project Aliases**: Aliases persisted in `project_aliases` (`0007_p11_project_aliases.py`) resolve via `resolve_alias` and remain subject to the exact same `FilesystemAccessManager.authorize()` checks — aliases never bypass folder permissions.
- **Untrusted Boundaries & Secret Scrubbing**: Extracted document/code payloads are wrapped via `wrap_untrusted` (`jarvis/security/untrusted.py`) and scrubbed through `redact_secrets` (`jarvis/security/pii_firewall.py`).

## 3. Outbound PII & Secret Egress Firewall

- **Module**: `jarvis/security/pii_firewall.py`
- **Capabilities**: `detect_secret()`, `redact_secrets()`, and `scrub_messages()` scan outbound model/tool messages before leaving localhost and mask 10 credential/token families (OpenAI/OpenRouter `sk-...`, Anthropic `sk-ant-...`, Hugging Face `hf_...`, GitHub `gh[pousr]_...`, Google `AIza...`, Tavily `tvly-...`, PEM private key blocks, `Bearer` headers, `scheme://user:password@host` URIs, and explicit password/API key/token assignments) plus optional email redaction (`mask_email=True`).

## 4. Desktop & Browser Action Security

- **Modules**: `jarvis/actions.py`, `jarvis/config.py`, `jarvis/api_v2.py`
- **Registry & Strict Validation**: All 19 desktop/browser actions are declared in `ACTION_REGISTRY` as `ActionSpec` entries with argument validators (`_ACTION_VALIDATORS`).
- **Owner Confirmation & Default-Off High-Risk Toggles**:
  - `ActionsSpec` keeps `allow_input_automation` disabled (`False`) by default and isolates `allow_screenshots`, `allow_media_controls`, and `allow_window_controls` under explicit Settings controls.
  - State-changing or sensitive actions pass through `requires_confirmation()` and `request_or_execute()` to mint single-use approval tokens resolved only by `/api/v2/actions/{token}/confirm` or `/api/v2/actions/{token}/reject`.
- **URL & SSRF Protections (`open_url`)**: Enforces `https://` URLs through `_validate_url_syntax()` to block loopback/private-network SSRF targets and honors `ActionsSpec.allowed_url_domains` when configured.
- **Browser Tier Enforcement (`browser_action`)**: Separates 3 security tiers where `side_effect` operations always require owner confirmation and `expose_credentials=True` requests are unconditionally blocked.

## 5. Voice Safety, Prompt Integrity & Proactive Non-Execution

- **Uncertain Voice Guard (`jarvis/translator.py`, `jarvis/assistant.py`)**: `transcript_requires_voice_clarification()` and `Assistant._voice_clarification_message()` intercept low-confidence, noise-corrupted, or script-mismatched STT transcripts before destructive/stateful command execution, prompting the user to repeat or confirm rather than guessing an action.
- **System Prompt Invariants (`jarvis/persona.py`)**: All 6 `PERSONALITY_PRESETS` (`professional`, `friendly`, `concise`, `technical`, `detailed`, `custom`) plus optional `custom_instructions` preserve `HONEST_OUTCOME_RULE` and `UNTRUSTED_DATA_POLICY` inside the cached `system_prompt()` prefix.
- **Non-Executing Proactive Engine (`jarvis/proactive.py`)**: `ProactiveSuggestionEngine.collect()` strictly emits informational HUD suggestions (`auto_execute=False`, `requires_user_action=True`) with dismissal cooldown (`cooldown_s`).
- **Memory Category Controls (`jarvis/memory/five_way.py`)**: `FiveWayMemoryService` enforces per-category enablement checks (`PERSONAL_CATEGORIES`, `is_category_enabled`) and supports scoped `clear_conversation_memory()`.

## 6. Known Limitations & Environment Constraints

- **Chrome DevTools E2E Gate**: Interactive Chrome DevTools Protocol automation (`browser` subagent) requires launching Google Chrome with remote debugging enabled (`DevToolsActivePort` was absent on the host Windows environment), so live CDP-driven browser clicks could not execute on this workstation. All backend security gates and static checks ran cleanly on Windows.

