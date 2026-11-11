# P10.13 Test & Verification Report

Executed after final source, test, and frontend edits on `2026-11-11` (`Python 3.14.6` in `.venv`; isolated Laya test environment in `data/laya_iso_venv`). All reported results were observed directly; unconfigured external infrastructure (stopped Docker daemon and unset `JARVIS_PG_*` test/scratch URLs) remained skipped rather than simulated.

---

## 1. Automated Verification Summary

| Check | Command / Scope | Exit | Observed Result |
|---|---|---|---|
| **Full pytest regression suite** | `.\.venv\Scripts\python.exe -m pytest -v` | **0** | **`171 passed, 4 skipped, 0 failed`** in `236.57s` (`99` upstream deprecation warnings in `langsmith`/`sounddevice`). |
| **P10.13 Test Suite** | `tests/test_p10_13_laya_and_skills.py` (`4` tests) | **0** | **4/4 passed**: 7-skill strict taxonomy & assistant/skills lifecycle separation, Laya shadow/primary/fallback/timeout/bad-schema/concurrency/cache/circuit-breaker + `ml`/`manglish` detection, hard Phase 5 security override & negation safety, leakage-free dataset/calibration/approval/rollback & Decision Panel API + Vite bundle check. |
| **P10.12 Hardening Suite** | `tests/test_p10_12_hardening.py` | **0** | **3 passed, 1 skipped** (`test_p10_12_1_live_docker_opencode_integration` skipped: Docker daemon stopped); `/api/v2/workforce/runs` + `.opencode/skills/mattpocock-tdd/SKILL.md` materialization + metric increment now executed live in API test. |
| **Ruff Linter** | `ruff check` across all P10.13/P10.12 touched dirs & tests | **0** | **`All checks passed!`** |
| **Mypy Type Checker** | `mypy --follow-imports=silent` on all `jarvis/decision/*.py`, `api_decision.py`, `api_skills.py`, `skills/{recommended_audit,registry}.py` (`14` files) | **0** | **`Success: no issues found in 14 source files`** |
| **Frontend Build** | `Push-Location apps\frontend; npm run build; Pop-Location` | **0** | `tsc --noEmit && vite build`: `37` modules transformed, `dist/assets/index-Durt93Vc.js` (`188.70 kB`), `0` external URLs, built in `939ms`. |
| **Alembic Migration Roundtrip** | Temp SQLite DB: `upgrade head -> downgrade base -> upgrade head` | **0** | **`ALEMBIC_ROUNDTRIP=0,0,0`** (`0001` <-> `0002` <-> `0003`). |
| **Core `.venv` Integrity & Security** | `pip check` & `pip_audit --local` | **0** | `No broken requirements found.` and `No known vulnerabilities found`. |
| **Live Server + `preflight.py`** | `server.py` (`127.0.0.1:4700`) + `preflight.py` + live HTTP checks | **0** | **`SUMMARY: 33 pass, 0 fail, 1 warn`** (optional `torch, transformers, peft` absent in `.venv` as intended); voice roundtrip `11.2s`; `/api/v2/decision/status` (`enabled=False, mode="disabled"`) & `/api/v2/skills` (`catalog=7`) verified live HTTP `200`. |
| **Offline & SSRF Drill (`drill.py`)** | `.\.venv\Scripts\python.exe drill.py` | **0** | `static scan: 0 external references`, `NON-LOCAL HOSTS (server log): []`, `NON-LOCAL HOSTS (OS level): []`, watcher blocked `api.anthropic.com`, `DRILL PASSED`. |
| **Live Isolated Laya-Multilingual CPU Probes** | `data/laya_iso_venv` (`laya 0.4.31`, `torch 2.15.0+cpu`, `convaiinnovations/laya-multilingual` in `data/laya_hf_cache`) | **0** | Warm preload RSS: `42.9 MB` -> `1,652.4 MB` loaded -> `1,696.6 MB` post-inference; single-question warm latency `89.4 ms`; 6-question schema latency `1,693 ms` P50; held-out `test.jsonl` (`N=36`) benchmarked against Heuristic (`0.034 ms`, `61.1%` dept / `55.6%` intent) & live Ollama `qwen3:4b-instruct` (`5,401 ms`, `33.3%` dept / `52.8%` intent). Calibrated Laya (`0.35`) achieved `0` false abstentions & `66.7%` Manglish intent accuracy (`44.4%` overall intent accuracy). |

---

## 2. Exact Skipped Test Cases (`pytest -v`)

1. `tests/test_p10_11_hardening.py::test_p10_11_2_live_docker_sandbox_isolation` — Docker daemon not running on Windows host (`com.docker.service` stopped).
2. `tests/test_p10_11_hardening.py::test_p10_11_3_live_postgres_backup_and_scratch_restore` — `JARVIS_PG_TEST_URL` and `JARVIS_PG_SCRATCH_URL` not configured.
3. `tests/test_p10_12_hardening.py::test_p10_12_1_live_docker_opencode_integration` — Docker daemon not running on Windows host.
4. `tests/test_p10_pgvector_memory.py::test_live_postgres_pgvector_crud_hnsw_expiry_and_migration` — `JARVIS_PG_TEST_URL` not configured.

