# P10.20 Model Routing & Provider Gateway Report

**Date:** 2026-11-12  
**Workspace:** `d:\projects\myjarvis`

---

## 1. Unified Multi-Provider Architecture

`ModelGateway` (`jarvis/gateway/gateway.py`) and provider adapters (`jarvis/gateway/providers.py`) coordinate local Ollama models alongside optional cloud/custom OpenAI-compatible endpoints through a single capability, privacy, secret-scrubbing, circuit-breaking gateway.

### Supported Providers
- Local runtime: `ollama` (primary default; `qwen3:4b-instruct`, `qwen2.5vl:3b`, `qwen3:4b` detected live on host)
- Cloud / OpenAI-compatible / Custom adapters: `openrouter`, `gemini`, `huggingface`, `openai`, `anthropic`, `custom` (configurable `custom_base_url` and `custom_model`)

---

## 2. Role-Based Model Selection & Fallback Chain

`ModelGateway.select_model_for_task(role)` (`jarvis/gateway/gateway.py:318`) maps tasks cleanly using Settings -> AI Providers & Routing:
- `fast` -> `providers.fast_model` (fallback to `boot_brain`)
- `advanced` -> `providers.advanced_model`
- `coding` -> `providers.coding_model`
- `embedding` -> `providers.embedding_model` (default `nomic-embed-text`)
- Fallback chain resolves `providers.fallback_provider` model and always falls back to local `boot_brain` when eligible.

`ModelGateway.inspect_candidate_eligibility()` & `test_route()` (`jarvis/gateway/gateway.py:206, 428`) evaluate each candidate against:
1. Streaming, tool calling, vision, and JSON mode capability support (`ProviderCapabilities`)
2. Estimated prompt + `max_tokens` context window fit (`estimate_prompt_tokens`)
3. Paid model authorization (`allow_paid_models`)
4. Per-provider circuit breaker state (`CLOSED` | `OPEN` | `HALF_OPEN`)
5. Privacy classification & offline mode gating.

---

## 3. Egress Firewall, Privacy Boundaries & DPAPI Secret Hygiene

- **Central Mandatory Firewall (`enforce_egress_firewall`):**
  - Blocks cloud egress automatically (`category="privacy_blocked"`) for `"confidential"` and `"restricted"` projects unless explicitly authorized via owner override.
  - Runs `scrub_messages()` + residual `detect_secret()` verification prior to external dispatch and logs firewall decisions to `AuditEventRow` (`category="egress_firewall"`).
- **Zero-Exposure Secret Storage (`jarvis/config.py`, `jarvis/api_v2.py`):**
  - Keys are stored encrypted on Windows using DPAPI via `POST /api/v2/gateway/providers/{provider}/key` and deleted via `DELETE /api/v2/gateway/providers/{provider}/key`.
  - Plaintext `api_key` values submitted to `PUT /api/v2/settings` or `PUT /api/config` are rejected with HTTP `422`.
  - `GET /api/v2/settings` exposes only boolean configuration indicators (`provider_keys`), never raw keys.
- **Offline Security Gate:**
  - Outbound cloud provider health checks and requests remain blocked (`category="offline_or_cloud_disabled"`) while `offline_enforced=True` or `internet.enabled=False` or `providers.allow_cloud=False`.

---

## 4. Runtime Verification Results

- **Fix Verified:** Fixed `ModelGateway.health_check_provider("ollama")` calling `self.brain.ollama.list_models()` -> now invokes `self.brain.health()` / `self.brain.installed()`.
- **Live Server Checks (`127.0.0.1:4720`):**
  - `POST /api/v2/gateway/providers/ollama/health` -> `{"ok": True, "provider": "ollama", "version": "0.40.16", "models_count": 3, "models": ["qwen3:4b-instruct", "qwen2.5vl:3b", "qwen3:4b"], "loaded": ["qwen3:4b-instruct"], "breaker": "CLOSED"}`
  - `POST /api/v2/gateway/route-test` (`role="fast"`, `project_privacy_level="private"`) -> selected `"local"`, `online_enabled: False`, all 7 breakers `"CLOSED"`
  - `POST /api/v2/gateway/route-test` (`role="advanced"`, `project_privacy_level="restricted"`) -> selected `"local_thinking"`
  - Live `/api/chat` (`5.31s`) and `/api/chat/stream` executed end-to-end against local `qwen3:4b-instruct`.
- **Automated Tests:** Covered by `test_scenario_f_settings_dpapi_custom_provider_and_permissions_api` (plaintext `422` rejection, DPAPI key save/delete without leak, custom base URL + role routing, offline `allow_cloud` block, Ollama health mock, online mocked cloud health probe).

