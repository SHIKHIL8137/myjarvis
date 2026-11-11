# P10 Test & Verification Report

Generated on 2026-11-11 on target Windows 11 host (`AMD Ryzen 5 5600H`, `NVIDIA GeForce GTX 1650 4 GB`, `15.3 GB RAM`, `Python 3.14.6`, `Ollama 0.40.16`, `Node v24.16.0`).
Every command and measurement below was actually executed after the final code changes. No mocked, skipped, or unrun live service test is presented as passing live.

---

## 1. Commands Executed & Observed Outcomes

| Command | Outcome | Result Summary |
|---|---|---|
| `.\.venv\Scripts\pytest.exe -v` | **Exit 0** | **156 passed, 1 skipped, 0 failed** in `187.01 s` (`0:03:07`), `99` third-party warnings (`langsmith`, `sounddevice`). |
| `.\.venv\Scripts\python.exe preflight.py` (with `server.py` running) | **Exit 0** | **33 pass, 0 fail, 1 warn** (`torch`/`transformers`/`peft` optional LoRA pkgs absent); voice budget `11.2 s` (`hear 1.3s`, `think 9.5s`, `speak 0.4s`); `drill.py` `0` non-local hosts, `0` external URLs in `viewer/` & `apps/frontend/dist/`, watcher blocked `api.anthropic.com`. |
| `.\.venv\Scripts\ruff.exe check ...` (P10 & modified modules/tests) | **Exit 0** | **All checks passed!** |
| `.\.venv\Scripts\mypy.exe --follow-imports=silent ...` (58 P0–P10 files) | **Exit 0** | **Success: no issues found in 58 source files**. |
| `.\.venv\Scripts\pip.exe check` & `.\.venv\Scripts\pip-audit.exe --local` | **Exit 0** | `No broken requirements found.` and `No known vulnerabilities found`. |
| Alembic `upgrade head -> downgrade base -> upgrade head` (temp DB) | **Exit 0** | `ALEMBIC_P10_ROUNDTRIP=0,0,0` (`0001_initial_schema` <-> `0002_p10_hardening`). |
| `apps/frontend`: `npm run build` (`tsc --noEmit && vite build`) | **Exit 0** | `35 modules transformed`, `dist/assets/index-DUIc-MgF.css` (`3.87 kB`), `dist/assets/index-DaALhkLZ.js` (`173.15 kB`), built in `783 ms`. |

---

## 2. Detailed Coverage by P10 Domain

### Workforce Safety (`tests/test_p10_workforce_safety.py` + `tests/test_ai_os_agents_workforce_dr.py`)
- `test_state_machine_rejects_invalid_transitions`: Enforces `QA_FAILED -> MERGING` and `PLANNING -> COMPLETED` raise `ValueError`. (**PASSED**)
- `test_failed_qa_and_exhausted_retries_never_reach_approval_or_merge`: Confirms retries stop cleanly (`attempts=2`), state becomes `FAILED` (`state="failed"`), zero approvals created, main repo unchanged. (**PASSED**)
- `test_untracked_env_secret_and_staged_binary_manifest_blocks_merge`: Confirms untracked `.env` containing API secret blocks merge (`BLOCKED` / `blocked_security`) without leaking the raw secret value into findings, and binary files are detected. (**PASSED**)
- `test_post_review_modification_invalidates_approval_and_blocks_merge`: Modifies worktree after security review -> owner resume blocks merge cleanly (`BLOCKED`). (**PASSED**)
- `test_worktree_isolation_collision_uncommitted_cleanup_and_sandbox_policy`: Refuses accidental overwrite (`ensure_unique=False`), enforces unique concurrent worktrees (`ensure_unique=True`), blocks cross-task worktree access, blocks path traversal cleanup, requires explicit authorization before cleaning dirty worktrees, blocks `powershell`, and reports `ExecutionPolicy.capabilities()` honestly. (**PASSED**)

### PostgreSQL, pgvector, Embeddings & Vector Performance (`tests/test_p10_pgvector_memory.py` + scale benchmark)
- `test_embedding_validation_normalization_and_dim_mismatch_block`: L2 normalization check + `EmbeddingCompatibilityError` on dimension mismatch. (**PASSED**)
- `test_memory_expiry_filtering_in_vector_and_sql_and_transactional_purge`: Confirms expired vectors never return from vector search or `search_personal_memory`, and `purge_expired()` removes SQL + vector rows. (**PASSED**)
- `test_memory_lifecycle_duplicates_supersession_audit_and_ranking`: Verifies create, `DuplicateMemoryError` on same topic/category without auto-merging different topics, update, archive, restore, supersede, soft delete, `AuditEventRow` logs, and multi-factor ranking breakdown. (**PASSED**)
- `test_pgvector_sql_builder_operators_and_sqlite_scale_benchmark`: Verifies parameterized `<=>` / `<->` / `<#>` PostgreSQL query generation with SQL `WHERE` expiry/JSONB filters + SQLite->SQLite schema/count-verified migration (`1000` vectors). (**PASSED**)
- `test_live_postgres_pgvector_crud_hnsw_expiry_and_migration` (`@pytest.mark.pg`): **SKIPPED** (`JARVIS_PG_TEST_URL` not configured).

#### Measured Vector Benchmark (`SQLiteVectorStore`, dim=256, `Ryzen 5 5600H`, NVMe SSD)
- **1,000 vectors**: `upsert_batch = 485.72 ms`, filtered + expiry `search(k=5) = 18.67 ms`, 4-thread concurrent search `= 123.76 ms` (`5` hits).
- **10,000 vectors**: `upsert_batch = 5596.59 ms`, filtered + expiry `search(k=5) = 208.59 ms`, 4-thread concurrent search `= 1387.13 ms` (`5` hits).
- **100,000 vectors & live PostgreSQL+pgvector HNSW**: **NOT VERIFIED** (live PostgreSQL connection string/Docker PG container not running).

### Disaster Recovery (`tests/test_p10_dr_gateway_learning.py` + `tests/test_ai_os_agents_workforce_dr.py`)
- Encrypted `.jvault` archive + AES-256-GCM secrets vault + passphrase-keyed HMAC-SHA256 manifest tamper detection + secondary Tier-3 destination + retention pruning + isolated `verify_backup` (verifying SQLite `PRAGMA integrity_check`, memory retrieval, vector probe, checkpoints, projects, RPO/RTO metrics) + full `restore_to_directory()` onto a clean machine path: **PASSED**.
- Live `pg_dump` / `pg_restore` against PostgreSQL 18 server: **NOT VERIFIED** (`pg_dump.exe` / `pg_restore.exe` detected at `C:\Program Files\PostgreSQL\18\bin\`; live PG credentials unavailable).

### Model Gateway, Firewall & Concurrency (`tests/test_p10_dr_gateway_learning.py` + `tests/test_brain.py` + `tests/test_live.py`)
- Genuine multi-chunk SSE streaming (`safe_stream_lines` + `httpx.MockTransport`), paid-model lock (`allow_paid_models=false`), centralized non-bypassable secret redaction & `confidential` privacy blocking, `CircuitBreakerState` trip to `OPEN` + local Ollama fallback, cancellation event, `ResourceGovernor` concurrency cap (`max_local_concurrency=1`) & queue overflow protection (`ResourceQueueFullError`), and `Brain` thinking-budget fallback on live Ollama `qwen3:4b`: **PASSED**.

### Learning & Frontend Control Center (`tests/test_p10_dr_gateway_learning.py` + `tests/test_learning_and_training.py`)
- License allowlist rejection (`unknown`), prompt-injection blocking (`detect_poisoning`), global cross-source chunk dedup (`dedup_skipped`), `external_knowledge` vector sync, `<retrieved_knowledge untrusted_data="true">` quarantine formatting, `auto_promote=false` guard, remote delegation export spec, `/api/v2/*` memory/worktree/gateway/recovery routes + `X-Jarvis: 1` CSRF `403` enforcement, and zero-CDN React Vite production build: **PASSED**.

---

## 3. Failed, Skipped & Unavailable Infrastructure Summary

- **Failed Tests (final post-edit suite)**: **0**
- **Skipped Tests**: **1** (`tests/test_p10_pgvector_memory.py::test_live_postgres_pgvector_crud_hnsw_expiry_and_migration`, requires `JARVIS_PG_TEST_URL`)
- **Unavailable Infrastructure / External Blocked Checks**:
  1. **Live PostgreSQL + `pgvector`**: Set `$env:JARVIS_PG_TEST_URL = "postgresql+psycopg://jarvis:<pw>@127.0.0.1:5432/jarvis"` (or start Docker Desktop + `docker compose -f deploy/docker-compose.db.yml up -d`) and run `.\.venv\Scripts\pytest.exe -m pg -v`.
  2. **Live Docker Sandbox**: Start Docker Desktop and configure `workforce.sandbox_backend="docker"`.
  3. **Live Chrome DevTools UI click-through**: Enable Chrome remote debugging at `chrome://inspect/#remote-debugging` (`DevToolsActivePort`) and inspect `http://127.0.0.1:4700/` and `/legacy`.
  4. **Physical Wi-Fi Disconnect (`.\offline-test.ps1`)**: Not executed inside active AI coding session to preserve connection; run manually with Airplane Mode ON.

