# P10.13 Implementation Report — Laya Decision Engine Integration, P10.12 Source Reconciliation & Seven-Skill Audit

Executed in `d:\projects\myjarvis` on `main` above commit `6914676c3ad474e4452bce1f0b51ae40bcaaf364` (Windows 11, AMD Ryzen 5 5600H, NVIDIA GeForce GTX 1650 4 GB VRAM, 15.3 GB RAM, Python `3.14.6`).

---

## 1. Phase 0 Source-of-Truth Verification & Reconciliation

Detailed verification baseline recorded in [`P10_13_PHASE0_STATE_REPORT.md`](file:///d:/projects/myjarvis/P10_13_PHASE0_STATE_REPORT.md):
- **Git & Environment**: Verified clean `main` at `6914676` with active `.venv` on Python `3.14.6` (`.venv` free of `torch`, `transformers`, `peft`, `laya`).
- **Path Reconciliation**: Mapped expected audit paths (`jarvis/skills/executor.py`, `jarvis/recovery/restore_guard.py`, `jarvis/recovery/backup_service.py`, `jarvis/agents/{opencode,docker}_runner.py`, `docker/opencode/*`) accurately to the actual repository layout (`jarvis/skills/{compatibility,router}.py`, `jarvis/db/targets.py`, `jarvis/recovery/backup.py`, `jarvis/integrations/{opencode_adapter,git_worktree}.py`, `deploy/opencode.Dockerfile`).
- **Resolved Functional Discrepancies**:
  1. **`assistant_graph.py` & `start_assistant_workflow()`**: Selected skills previously stopped inside state without downstream consumption or metrics. Now [`assistant_graph.py`](file:///d:/projects/myjarvis/jarvis/agents/assistant_graph.py) records `[skills=...]` provenance in generated summaries and [`orchestrator.py`](file:///d:/projects/myjarvis/jarvis/agents/orchestrator.py) calls `self.skills.record_invocation_metric(...)` for assistant runs identically to workforce runs.
  2. **`tests/test_p10_12_hardening.py`**: Extended [`test_p10_12_3_4_5_skills_lifecycle_security_compat_routing_metrics_and_api`](file:///d:/projects/myjarvis/tests/test_p10_12_hardening.py#L441) to execute `/api/v2/workforce/runs` (with mocked `opencode_adapter.run_opencode_task` asserting `.opencode/skills/mattpocock-tdd/SKILL.md` materialization) and verify non-zero recorded `mattpocock-tdd` metrics.
  3. **`deploy/docker-compose.opencode.yml`**: Added `/home/jarvis/.config:rw,nosuid,size=16m` `tmpfs` mount aligning with `git_worktree.build_docker_run_cmd()`.

---

## 2. Phase 1 — Seven-Skill Audit Re-Verification & Lifecycle Separation

Full findings published in [`P10_13_SKILL_AUDIT_REPORT.md`](file:///d:/projects/myjarvis/P10_13_SKILL_AUDIT_REPORT.md).
- Re-cloned all 7 repositories read-only (`git -c core.hooksPath=NUL clone --depth 1`), inspected licenses, scripts, hooks, MCP configs, `SKILL.md` counts (`mattpocock/skills` corrected from `40` to **`39`**), and security findings.
- Enforced strict 6-class taxonomy in [`jarvis/skills/recommended_audit.py`](file:///d:/projects/myjarvis/jarvis/skills/recommended_audit.py): `approved`, `approved_with_restrictions`, `needs_manual_review`, `blocked`, `incompatible`, `do_not_install`.
- Exposed separate per-skill and per-catalog `lifecycle_states` flags (`audited`, `approved`, `installed`, `enabled`, `assigned`, `successfully_executed`) in [`jarvis/skills/registry.py`](file:///d:/projects/myjarvis/jarvis/skills/registry.py), [`jarvis/api_skills.py`](file:///d:/projects/myjarvis/jarvis/api_skills.py), and [`SkillsPanel.tsx`](file:///d:/projects/myjarvis/apps/frontend/src/features/SkillsPanel.tsx).

---

## 3. Phases 2–5 — Isolated Laya Adapter & Hard Security Boundaries

- **Isolated Runtime (`data/laya_iso_venv`)**: Installed `laya 0.4.31`, `torch 2.15.0+cpu`, `transformers 5.22.2`, `vegaml 0.16.1`, `onnxruntime 1.31.1`, `fastapi` without polluting core `.venv`.
- **Configuration ([`jarvis/config.py`](file:///d:/projects/myjarvis/jarvis/config.py))**: Added `LayaSpec` (`enabled=False`, `mode="shadow"`, `service_mode="http"`, `base_url="http://127.0.0.1:4710"`, `checkpoint="multilingual"`, `device="cpu"`, `timeout_ms=1500`, `max_concurrency=1`, `max_resident_models=1`, `confidence_threshold=0.70`, `abstain_enabled=True`, `circuit_breaker_failures=3`, `cache_enabled=True`) plus `JARVIS_LAYA_*` env overrides and loopback URL validation.
- **Provider-Independent Classifier Architecture**:
  - [`jarvis/decision/schemas.py`](file:///d:/projects/myjarvis/jarvis/decision/schemas.py): Canonical enums (`ALLOWED_DEPARTMENTS`, `ALLOWED_INTENTS`, `ALLOWED_COMPLEXITIES`, `ALLOWED_RISK_TIERS`, `ALLOWED_WORKFLOWS`), `JARVIS_LAYA_QUESTIONS`, strict `validate_classifier_payload()`, and security bypass key rejection (`ClassifierSchemaError`).
  - [`jarvis/decision/classifier.py`](file:///d:/projects/myjarvis/jarvis/decision/classifier.py): Abstract [`DecisionClassifier`](file:///d:/projects/myjarvis/jarvis/decision/classifier.py#L30), [`ExistingHeuristicClassifier`](file:///d:/projects/myjarvis/jarvis/decision/classifier.py#L140), and [`ExistingLLMClassifier`](file:///d:/projects/myjarvis/jarvis/decision/classifier.py#L150).
  - [`jarvis/decision/laya_adapter.py`](file:///d:/projects/myjarvis/jarvis/decision/laya_adapter.py): [`IsolatedLayaClassifier`](file:///d:/projects/myjarvis/jarvis/decision/laya_adapter.py#L22) supporting isolated loopback HTTP (`POST /internal/decision/classify`) & subprocess worker ([`laya_worker.py`](file:///d:/projects/myjarvis/jarvis/decision/laya_worker.py)), semaphore-bounded concurrency, `asyncio.wait_for` timeout, TTL/LRU cache, and circuit breaker.
  - [`jarvis/decision/laya_service.py`](file:///d:/projects/myjarvis/jarvis/decision/laya_service.py): Standalone loopback FastAPI server (`127.0.0.1:4710`, `--preload`, Windows `GetProcessMemoryInfo` RSS reporting, LRU resident model eviction).
  - [`jarvis/decision/router.py`](file:///d:/projects/myjarvis/jarvis/decision/router.py): Preserves Layer 1 `<5 ms` deterministic commands/stops; dispatches Layer 2 under `disabled | shadow | laya_primary | laya_with_heuristic_fallback` while keeping live voice & Ollama pipelines untouched unless explicitly routed.
- **Phase 5 Hard Security Policy Boundary ([`jarvis/decision/rules.py`](file:///d:/projects/myjarvis/jarvis/decision/rules.py))**: `detect_sensitive_operations()` & `enforce_security_boundary()` prevent any classifier (Laya, Heuristic, or LLM) from directly authorizing shell execution, file deletion, DB migrations/restores, Git merges, deployments, secret access, network policy overrides, skill installs, or privilege escalation while handling negated/explanatory prompts accurately.

---

## 4. Phases 6–9 — Dataset, Benchmark, Calibration & Decision UI

- **Versioned Leakage-Free Dataset ([`data/decision/datasets/v1/`](file:///d:/projects/myjarvis/data/decision/datasets/v1/test.jsonl), [`dataset.py`](file:///d:/projects/myjarvis/jarvis/decision/dataset.py))**: `train.jsonl` (`24`), `val.jsonl` (`16`), and strictly held-out `test.jsonl` (`36`) covering English, Malayalam script (`ml`), Malayalam-English code-mixed (`manglish`), ambiguous/abstain, negated, and sensitive prompts (`leakage_free=True`).
- **Live Comparative Benchmark ([`benchmark.py`](file:///d:/projects/myjarvis/jarvis/decision/benchmark.py), [`P10_13_LAYA_BENCHMARK_REPORT.md`](file:///d:/projects/myjarvis/P10_13_LAYA_BENCHMARK_REPORT.md))**: Evaluated `Heuristic`, live Ollama `qwen3:4b-instruct`, and real isolated `convaiinnovations/laya-multilingual` (CPU, raw `0.70` vs calibrated `0.35`).
- **Calibration, Approvals & Rollback ([`calibration.py`](file:///d:/projects/myjarvis/jarvis/decision/calibration.py))**: Tracks corrections (`corrections.jsonl`), prepares candidate policies strictly on `train+val`, requires single-use `ApprovalRequestRow` owner tokens before activation, and supports atomic rollback.
- **API & Frontend ([`jarvis/api_decision.py`](file:///d:/projects/myjarvis/jarvis/api_decision.py), [`DecisionPanel.tsx`](file:///d:/projects/myjarvis/apps/frontend/src/features/DecisionPanel.tsx), [`App.tsx`](file:///d:/projects/myjarvis/apps/frontend/src/App.tsx))**: Added `DECISION` tab and `/api/v2/decision/*` status, mode toggle, test router, correction logging, held-out benchmark runner, calibration preparation, activation, and rollback routes behind loopback + `X-Jarvis: 1` guards.

