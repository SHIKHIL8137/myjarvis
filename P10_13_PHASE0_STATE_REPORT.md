# P10.13 Phase 0 — Repository State & P10.12 Reconciliation Report

Verified prior to implementing any P10.13 source changes (`2026-11-11 23:27 +05:30`).

## 1. Repository, Git & Python Environment Ground Truth

- **Repository Root**: `d:\projects\myjarvis`
- **Git Branch & HEAD**: `main` at commit `6914676c3ad474e4452bce1f0b51ae40bcaaf364` (`P10.12: skills system, deploy targets, sandbox proxy`, 37 files changed, +4361/-93 lines).
- **Working Tree Status**: Clean (`git status --short` empty).
- **Active Virtual Environment**: `D:\projects\myjarvis\.venv\Scripts\python.exe` (**Python `3.14.6`**).
- **Installed ML Packages**: `onnxruntime 1.31.1`, `faster-whisper 1.2.1`, `piper-tts 1.8.0`, `scikit-learn 1.9.1`, `huggingface_hub 2.4.0`, `tokenizers 0.23.3`. `torch`, `transformers`, `peft`, `laya`, and `vegaml` are **not** installed in `.venv`.
- **External Service Reality**: Docker CLI present (`29.6.1`), `com.docker.service` stopped on host; PostgreSQL (`postgresql-x64-18`) running, `JARVIS_PG_TEST_URL` and `JARVIS_PG_SCRATCH_URL` unset.
- **Baseline Checks Before P10.13 Edits**:
  - `pytest tests/test_p10_12_hardening.py tests/test_ai_os_memory_decision_gateway.py -v`: **`8 passed, 1 skipped`** (`test_p10_12_1_live_docker_opencode_integration` skipped because Docker daemon is not running).
  - `ruff check` on `jarvis/skills`, `api_skills.py`, `db/targets.py`, `gateway/sandbox_proxy.py`, `recovery/backup.py`, `integrations/{git_worktree,opencode_adapter}.py`, `tests/test_p10_12_hardening.py`: **All checks passed**.
  - `mypy --follow-imports=silent` on 14 P10.12 core Python modules: **0 issues**.

## 2. Expected-vs-Actual File Path Matrix

| Expected Checklist Path | Actual Status in Repo | Actual Implementing File(s) & Notes |
|---|---|---|
| `jarvis/skills/metadata.py` | **EXISTS** (109L) | Canonical `SkillManifest`, `normalize_skill_id`, `parse_skill_markdown`, `compute_directory_sha256` |
| `jarvis/skills/security.py` | **EXISTS** (139L) | `inspect_skill_security`, `enforce_system_policy_precedence` |
| `jarvis/skills/compatibility.py` | **EXISTS** (150L) | `inspect_runtime_compatibility`, `adapt_skill_for_runtime`, `SkillCompatibilityError` |
| `jarvis/skills/validator.py` | **EXISTS** (134L) | `validate_skill_package`, `SkillValidationError` |
| `jarvis/skills/registry.py` | **EXISTS** (675L) | `SkillRegistryService` (DB + isolated `data/skills/installed/` lifecycle & metrics) |
| `jarvis/skills/router.py` | **EXISTS** (180L) | `AgentCapabilityRouter` (`select_skills_for_task`, `materialize_for_worktree`) |
| `jarvis/skills/executor.py` | **MISSING** | Guidance adaptation & prompt wrapping handled across `compatibility.py`, `router.py`, and graph nodes |
| `jarvis/skills/recommended_audit.py` | **EXISTS** (203L) | Static Python catalog of the 7 audited repositories (`RECOMMENDED_SKILL_AUDIT_CATALOG`) |
| `jarvis/api_skills.py` | **EXISTS** (235L) | FastAPI `/api/v2/skills*` subrouter |
| `jarvis/recovery/restore_guard.py` | **MISSING** | Implemented inside `jarvis/db/targets.py` (243L) + `jarvis/recovery/backup.py` |
| `jarvis/recovery/backup_service.py` | **MISSING** | Implemented inside `jarvis/recovery/backup.py` (1350L, `DisasterRecoveryManager`) |
| `jarvis/agents/opencode_runner.py` | **MISSING** | Implemented in `jarvis/integrations/opencode_adapter.py` (201L, `run_opencode_task`) |
| `jarvis/agents/docker_runner.py` | **MISSING** | Implemented in `jarvis/integrations/git_worktree.py` (`ExecutionPolicy`, `build_docker_run_cmd`) & `dev/workforce.py` |
| `docker/opencode/Dockerfile` | **MISSING** | Implemented at `deploy/opencode.Dockerfile` (89L) + `deploy/docker-compose.opencode.yml` (23L) |
| `docker/opencode/entrypoint.sh` | **MISSING** | No separate `entrypoint.sh`; container uses inline `CMD ["opencode", "--version"]` |
| `P10_12_IMPLEMENTATION_REPORT.md` | **EXISTS** (65L) | Committed in `6914676` |
| `P10_12_TEST_REPORT.md` | **EXISTS** (31L) | Committed in `6914676` |

## 3. Code-vs-Report Functional Discrepancies Identified

1. **`assistant_graph.py` & `start_assistant_workflow()` ignore selected skills & skip metric recording**:
   - `P10_12_IMPLEMENTATION_REPORT.md` §2.3 states `AgentCapabilityRouter` sits between `DecisionEngine` and `workforce_graph.py` / `assistant_graph.py` and records invocation metrics.
   - Reality in code: `jarvis/agents/assistant_graph.py` L46-58 populates `selected_skills` and `skill_prompt_block` inside `gather_context()`, **none** of `export_doc`, `create_rem`, `draft_email`, or `summarize` uses `skill_prompt_block`, and `AgentOrchestrator.start_assistant_workflow()` (`jarvis/agents/orchestrator.py` L159-172) **never calls** `self.skills.record_invocation_metric(...)` (only `start_workforce_workflow()` does).
2. **`tests/test_p10_12_hardening.py` L441 comment overclaims test coverage**:
   - Comment says `# Assign TDD skill -> select-preview -> workforce run -> verifies metric recorded`, but lines 442-452 stop after `/api/v2/skills/select-preview` and never dispatch `/api/v2/workforce/runs` in that test function.
3. **Seven-skill audit taxonomy & separated lifecycle state gap**:
   - `recommended_audit.py` uses custom `policy_verdict` strings (`approve_recommended`, `approve_selective`, `approve_guidance_only_quarantined`, `do_not_install_framework`, `optional_documentation`) instead of P10.13's required strict classification (`approved | approved_with_restrictions | needs_manual_review | blocked | incompatible | do_not_install`).
   - Static catalog items do not expose separated per-skill boolean/state fields for `audited`, `approved`, `installed`, `enabled`, `assigned`, `successfully_executed` alongside DB rows.
4. **`DecisionEngine` integration scope**:
   - `DecisionEngine` (`jarvis/decision/router.py`) is currently called only by `POST /api/v2/decision/route` (`jarvis/api_v2.py` L176); `Assistant.stream_turn()` does not invoke `DecisionEngine`. Furthermore, Layer 2 lacks a pluggable `DecisionClassifier` interface, timeout/concurrency/circuit-breaker wrapper, strict schema validation, and Laya adapter.
5. **Compose / Report minor mismatches**:
   - `deploy/docker-compose.opencode.yml` omits the `/home/jarvis/.config` `tmpfs` mount that `git_worktree.build_docker_run_cmd()` adds dynamically (`git_worktree.py` L292).
   - `P10_12_TEST_REPORT.md` L16 refers to "all 24 P10.12 modified/created files", whereas commit `6914676` touched `37` files (`26` Python files plus frontend/deploy/markdown).

## 4. Safe Remediation Plan for P10.13

- Preserve all working core voice, RAG, Ollama, 5-way memory, DR, and LangGraph workforce paths.
- Re-audit all 7 external skill repos via fresh read-only shallow clones, reconcile classifications under the strict 6-state taxonomy, wire `assistant_graph` skill metric recording & prompt block propagation, fix Compose/test discrepancies, and add honest end-to-end API/orchestrator tests.
- Research and test `laya` in an isolated Python virtualenv first before integrating `DecisionClassifier` (`ExistingHeuristicClassifier`, `ExistingLLMClassifier`, `LayaClassifier`) behind fail-closed timeouts, circuit breakers, strict security boundaries, held-out multilingual/Manglish benchmarks, calibration controls, and the Decision Engine React dashboard panel.

