# P10.19 Security Report

**Date:** 2026-11-12  
**Scope:** Autonomous routing gates, project-scoped filesystem boundaries, LLM reasoning leak sanitization, offline enforcement, and host runtime state.

---

## 1. Security Controls Preserved & Hardened in P10.19

1. **Destructive & High-Risk Operation Approval Gates (`jarvis/decision/`, `jarvis/assistant.py`, `jarvis/agents/approvals.py`)**:
   - Fixed the `file_deletion` rule regex in `jarvis/decision/rules.py` (`files?` with optional modifiers) so inputs such as `"delete all files..."` cannot bypass classification as sensitive actions.
   - Owner approval gates remain mandatory before executing:
     - File/folder deletion (`file_deletion`)
     - Database restore or schema migration
     - `git push` / `git merge`
     - Production deployment actions
     - Destructive shell commands
     - Credential/secret access
     - Untrusted/high-risk skill execution
   - Verified by `tests/test_p10_19_autonomous_voice_os.py::test_req6_security_blocks_all_destructive_operations_pending_approval` (**PASSED**).

2. **Project-Scoped Filesystem Isolation (`jarvis/agents/orchestrator.py`)**:
   - During autonomous dev pipeline execution (`run_autonomous_dev_pipeline`), registered project inspection constructs a dedicated project-scoped `SafeFilesystem(FilesystemSpec(allowed_roots=[str(proj_root)]), default_base=proj_root)` rather than widening global filesystem roots.
   - Verified live path traversal rejection in `preflight.py`: `/api/files/read?path=C:/Windows/System32/drivers/etc/hosts` -> **HTTP 403** (`PASS`).

3. **LLM Reasoning / `<think>` Leak Prevention (`jarvis/brain.py`)**:
   - `ThinkFilter` strips `<think>...</think>`, unpaired closing `</think>`, and truncated fallback reasoning blocks across streaming chunk boundaries.
   - Verified in unit & live integration tests (`tests/test_brain.py`: 7 tests including `test_unpaired_closing_think_and_truncated_fallback_never_leak`; `tests/test_live.py::test_live_thinking_model_never_exposes_reasoning` against live Ollama) and live `preflight.py` (`/chat answer from notes` -> `[PASS] no reasoning leak in answer`).

4. **Local Mode & Loopback Network Isolation (`data/settings.json`, `drill.py`, `preflight.py`)**:
   - Confirmed `data/settings.json` has `"internet": {"enabled": false}`.
   - Confirmed `apps/frontend/dist/` and `viewer/` contain **0 external URLs** and **0 API keys/secrets** (`preflight.py` & `drill.py`).
   - Confirmed `config.json`, source files, `data/history.jsonl`, `.env`, and private notes return **404** from HTTP static routes (`preflight.py`).
   - Confirmed both Jarvis HTTP server (`127.0.0.1`) and Ollama (`127.0.0.1:11434`) bind strictly to loopback (`preflight.py`).
   - Live `drill.py` verification recorded:
     - Server-side connection log: `0` remote connections (`[]`)
     - OS-level `netstat -ano` samples of server PID `10352`: `[]` non-local hosts
     - `/api/learning/datasets/discover` blocked with HTTP `503` in Local Mode
     - Network watcher self-test: blocked outbound attempt to `api.anthropic.com:443` with `ConnectionError`.

---

## 2. Host Security & Isolation Caveats Observed

- **Docker Daemon Offline**: `docker info` failed with `npipe:////./pipe/dockerDesktopLinuxEngine` not found (`exit 1`); container-isolated OpenCode execution was not exercised live (two Docker hardening tests in `test_p10_11_hardening.py` and `test_p10_12_hardening.py` skipped; `AgentOrchestrator._can_execute_coder()` honestly reports runtime unavailability).
- **PostgreSQL Live Test DB Unconfigured**: `JARVIS_PG_TEST_URL` and `JARVIS_PG_SCRATCH_URL` are unset on this host (two live PostgreSQL/pgvector tests skipped).
- **Chrome DevTools Inactive**: `DevToolsActivePort` missing, preventing automated live browser DOM/XSS inspection beyond static bundle scans (`preflight.py` + `drill.py`) and FastAPI HTTP security tests.
