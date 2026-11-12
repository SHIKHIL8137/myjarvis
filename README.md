# Jarvis — Local-First Personal AI Assistant & Multi-Agent OS

Jarvis runs local-first on Windows (Ollama local LLM inference, `faster-whisper` multilingual STT, Piper TTS, `"Hey Jarvis"` wake word via openWakeWord, NLMS echo cancellation + safe half-duplex mic loop), hardened in P10 with Worktree QA/Security gates, PostgreSQL + native `pgvector` vector search, streaming Model Gateway with circuit breakers & central PII firewall, controlled Knowledge/Model Learning, React Owner Control Center, and complete 3-2-1 Disaster Recovery.

---

## 1. Starting Jarvis

### Prerequisites

- **Windows 10/11**, **Python 3.12+** (tested on Python `3.14.6`), **Node 20+** (`v24.16.0`), **Git**
- **Ollama** bound to `127.0.0.1:11434` (`OLLAMA_HOST=127.0.0.1:11434`) with default local models pulled:
  ```powershell
  ollama pull qwen3:4b-instruct
  ollama pull qwen3:4b
  ```
- Optional external CLIs for Dev Workforce / PG DB: `opencode`, `graphify`, `pg_dump`, `pg_restore`, `docker`

### Start & Stop Commands

1. **Start Local Ollama** (loopback only):
   ```powershell
   $env:OLLAMA_HOST = "127.0.0.1:11434"
   ollama serve
   ```
2. **Start Backend + Prebuilt Frontend (`http://127.0.0.1:4700/`)**:
   ```powershell
   .\start-jarvis.ps1      # starts FastAPI + React Control Center on 127.0.0.1:4700
   .\stop-jarvis.ps1       # stops Jarvis cleanly without stopping Ollama
   ```
3. **Rebuild Frontend (optional development)**:
   ```powershell
   Push-Location apps\frontend
   npm ci
   npm run build
   Pop-Location
   ```

---

## 2. Database Setup (SQLite Default or PostgreSQL + `pgvector`)

- **Default Local Mode**: Uses SQLite WAL at `data/jarvis.db` automatically when `JARVIS_DATABASE_URL` and `database.url` are empty.
- **PostgreSQL + `pgvector` Production Mode**:
  1. Start PostgreSQL with `pgvector` installed (either local PostgreSQL 18 + `vector` extension or `docker compose -f deploy/docker-compose.db.yml up -d`).
  2. Set the connection string in environment and run Alembic migrations:
     ```powershell
     $env:JARVIS_DATABASE_URL = "postgresql+psycopg://jarvis:<password>@127.0.0.1:5432/jarvis"
     .\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
     ```
  3. To transfer existing SQLite data non-destructively into PostgreSQL (`ID`, timestamps, namespaces, `project_id`, `expires_at`, and count verification), call `migrate_sqlite_to_postgres()` from `jarvis.memory.legacy_migrator`.

---

## 3. Model Gateway, Cloud Providers & Hardware Policy

- **Paid-model lock**: `providers.allow_cloud=false` and `providers.allow_paid_models=false` by default. Free cloud options (e.g., `openrouter:*:free`, Gemini free tier, Hugging Face router) take priority when Online Assisted Mode is enabled; `openai` and `anthropic` require explicit `providers.allow_paid_models=true`.
- **Secrets management**: Set API keys securely via the API/UI (`/api/internet/key`), stored DPAPI-encrypted at rest in gitignored `data/secrets.json`, or via env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `HF_TOKEN`).
- **Central PII/Secret Firewall**: Outbound external requests automatically redact API keys/credentials and block `confidential`/`restricted` project content unless explicitly authorized with an audited owner override.
- **GTX 1650 (4 GB VRAM) Resource Limits**: `ResourceGovernor` serializes concurrent local heavy inference (`max_local_concurrency=1`) and bounds queue sizes (`max_queue_size=16`).

---

## 4. Workforce Safety & Execution Restrictions

- **Mandatory Gates**: Coding runs execute inside isolated `git worktree` checkouts (`.jarvis_worktrees/<slug>`) with immutable `base_sha` tracking. Security scanning covers committed `base_sha..HEAD` diffs, staged/unstaged changes, untracked files, renames (`old_path`/`new_path`), deletions, mode/symlink changes, and binary hashes via streaming reads up to `2 MB` (oversized text files are explicitly blocked). Approvals pin `base_sha`, `head_sha`, `manifest_sha256`, `diff_hash`, and `approval_fingerprint`.
- **Real Cancellation**: Cancelling an active run terminates active subprocess trees and Docker containers, expires pending approvals, preserves worktrees (`cancelled_preserved`) unless cleanup is authorized, and blocks approval/merge resumption.
- **Sandbox Policy & Docker Image**: Build the pinned OpenCode container with `docker build -f deploy/opencode.Dockerfile -t jarvis-opencode:1.19.5 .` (`opencode-ai@1.19.5` verified via SHA-512 SRI digests, non-root `10001:10001`, `--read-only`, `--cap-drop=ALL`, `--pids-limit=256`). Network policy defaults to `offline` (`--network=none`), with configurable `model_gateway` (short-lived scoped `/api/v2/sandbox/v1/*` bearer tokens — zero host API keys injected), `restricted_egress` (domain-allowlist `RestrictedEgressProxy`), and owner-authorized `unrestricted`. See [`deploy/README.md`](deploy/README.md). On native Windows without Docker Desktop running, `ExecutionPolicy` refuses silent unisolated fallback when `sandbox_backend="docker"` unless `allow_host_fallback=true`.

## 4B. Agent Skills & Capabilities Manager

- **Central Registry (`SKILLS` tab & `/api/v2/skills/*`)**: Validates external skills before owner-approved installation (license allowlist, deterministic SHA-256 checksum, malicious script & policy-override scanner, runtime compatibility across `jarvis_native`, `opencode`, `claude_code`, `codex`), isolates files in `data/skills/installed/`, tracks version history with atomic rollback, enforces context-budgeted dynamic selection (`AgentCapabilityRouter`), strictly preserves system-policy precedence over all skill instructions, and separates `audited`, `approved`, `installed`, `enabled`, `assigned`, and `successfully_executed` states under the strict 6-class taxonomy (`approved`, `approved_with_restrictions`, `needs_manual_review`, `blocked`, `incompatible`, `do_not_install`).

## 4C. Experimental Laya Decision Engine Adapter (`DECISION` Tab)

- **Isolated Sidecar (`disabled` by default)**: Runs [`laya`](https://github.com/NandhaKishorM/laya) (`convaiinnovations/laya-multilingual` or `vegaml`) strictly inside an isolated virtualenv (`data/laya_iso_venv`) over loopback (`127.0.0.1:4710`, `POST /internal/decision/classify`) so heavy ML dependencies (`torch`, `transformers`, `vegaml`) remain isolated from core `.venv`:
  ```powershell
  $env:HF_HOME = "d:\projects\myjarvis\data\laya_hf_cache"
  .\data\laya_iso_venv\Scripts\python.exe -m jarvis.decision.laya_service --host 127.0.0.1 --port 4710 --checkpoint multilingual --device cpu --preload
  ```
- **Safe Rollout & Security**: Supports `disabled`, `shadow` (default when enabled), `laya_primary`, and `laya_with_heuristic_fallback` modes with timeouts, circuit breakers, strict enum schema validation, and hard security policy overrides (classifiers cannot authorize shell commands, file deletion, DB migrations/restores, git merges, deployments, credentials, network overrides, skill installs, or privileges). See `P10_13_LAYA_BENCHMARK_REPORT.md` for measured CPU benchmark tradeoffs before enabling beyond `shadow`.

---

## 5. Learning Pipeline & Model Registry

- **Knowledge Base (RAG)** and **Model Weight Training** are separate subsystems:
  - Remote documentation/Hugging Face knowledge sources require Online Assisted Mode, allowlisted hosts, verified permissive licenses (`allowed_licenses`), and prompt-injection screening (`detect_poisoning`), and are wrapped in `<retrieved_knowledge untrusted_data="true">` delimiters.
  - Fine-tuning defaults to `automatic_training=false`, `require_training_approval=true`, `evaluation_required=true`, `auto_promote=false`, and `force_spawn_even_if_blocked=false`. Missing required packages/CUDA cannot be bypassed via API override; hardware limit overrides require explicit audited `override_reason` + `actor`; duplicate/concurrent jobs are blocked (`max_concurrent_jobs=1`), and the worker refuses silent FP32 fallback for 4-bit/8-bit QLoRA. Low-VRAM systems can export a remote training spec via `TrainingManager.prepare_remote_delegation_bundle()`.

---

## 6. Environment Variable Reference

| Variable | Purpose |
|---|---|
| `JARVIS_DATABASE_URL` | Optional SQLAlchemy DB URL (`postgresql+psycopg://...` or `sqlite:///...`) |
| `JARVIS_PROD_DATABASE_URL` / `JARVIS_DEV_DATABASE_URL` | Registered remote production / dev PostgreSQL targets protected by `DatabaseTargetRegistry` |
| `JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL` | Optional live PostgreSQL + `pgvector` and scratch restore DB URLs for `@pytest.mark.pg` integration tests |
| `OLLAMA_HOST` | Loopback Ollama bind address (`127.0.0.1:11434`) |
| `JARVIS_LAYA_ENABLED` / `JARVIS_LAYA_MODE` / `JARVIS_LAYA_BASE_URL` | Optional isolated Laya decision classifier settings (`false`, `shadow`, `http://127.0.0.1:4710`) |
| `OPENROUTER_API_KEY` / `GEMINI_API_KEY` / `HF_TOKEN` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Optional provider API keys (also storable in DPAPI-encrypted `data/secrets.json`) |

---

## 7. Backup & Complete New-Laptop Recovery

1. **Create & Verify a Backup** (UI **RECOVERY** tab or `/api/v2/recovery/backup` + `/api/v2/recovery/verify`): captures SQLite/PostgreSQL dump (`pg_dump` via temporary `PGPASSFILE`), vector snapshots, 5-way memory, workflow checkpoints, project registry, encrypted `secrets.vault.enc`, `settings.json`, documents, knowledge DB, and optional Git bundles + tracked diffs + untracked files inside an AES-256-GCM `.jvault` archive with HMAC-SHA256 verification. Explicitly distinguishes `database_and_app_state_only` vs `full_recovery_complete` (`repositories_included`). PostgreSQL verification refuses targeting the live production DB and treats any non-zero `pg_restore --exit-on-error` exit or missing `pgvector`/row mismatch as failure.
2. **Recover on a New Laptop**:
   ```powershell
   git clone <repo-url> myjarvis
   cd myjarvis
   .\scripts\bootstrap.ps1 -RestoreBundle "D:\Backups\jarvis-tier2_warm-YYYYMMDD-HHMMSS.zip.jvault" -Passphrase "<passphrase>"
   ```
   Pass `-PostgresUrl "postgresql+psycopg://jarvis:<pw>@127.0.0.1:5432/jarvis"` if restoring into PostgreSQL. After bootstrap completes, inspect recoverable workflows/approvals in the **WORKFORCE** tab before resuming.

---

## 8. Verification & Troubleshooting

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe preflight.py
.\.venv\Scripts\python.exe drill.py
.\offline-test.ps1
```

### Common Issues

- **PostgreSQL / `pgvector` unavailable**: Leave `JARVIS_DATABASE_URL` unset to run SQLite WAL locally, or ensure `CREATE EXTENSION IF NOT EXISTS vector;` succeeds in PostgreSQL.
- **Ollama out of memory (GTX 1650 4 GB)**: Unload unused models (`ollama ps` / switch brain), keep `max_local_concurrency=1`, or offload heavy reasoning to an explicit free cloud model in Online Assisted Mode.
- **Cloud provider rate limit (429/503)**: `ModelGateway` automatically applies exponential backoff + jitter, trips circuit breakers temporarily, and falls back to the configured local model.
- **OpenCode or Graphify unavailable**: Install missing CLI binaries cleanly (`opencode`, `graphify`) or inject a custom runner during tests; read-only Graphify queries gracefully report missing artifacts without corrupting repos.
- **Backup verification mismatch / tamper error**: Do **not** restore unverified archives onto live DBs; inspect `manifest.json` file checksums and confirm the exact recovery passphrase.
- **Interrupted workflow or failed training**: Inspect recoverable runs in **WORKFORCE** (`workforce_graph` pauses cleanly at DB checkpoints) and failed worker diagnostics under `data/learning/training/jobs/<job_id>/worker.log`.

### Intelligence, Filesystem, Orchestration, Memory, Desktop & System Operations

Jarvis includes an integrated Stage A–F local personal assistant pipeline built directly atop its FastAPI backend, SQLite/PostgreSQL migrations (`0007_p11_project_aliases`, `0008_p11_command_history`), and dark HUD frontend:

- **Permission-Gated Filesystem & Project Aliases (`jarvis/filesystem_access.py`)**: Named folder aliases (`project_aliases`), project structure trees, document parsing (`.pdf`, `.docx`, `.xlsx`, `.pptx`, text/code), secret scrubbing via `pii_firewall`, and untrusted-content boundaries.
- **Task Classification, Multi-Step Execution & Continuity (`jarvis/agents/multistep.py`, `jarvis/agents/command_history.py`)**: Classifies complex requests into sequential checkpointed multi-step plans with persisted command history, step-by-step progress, and replay/continuity commands.
- **Adaptive 5-Way Memory (`jarvis/memory/five_way.py`, `jarvis/config.py`)**: Per-category memory governance (`PERSONAL_CATEGORIES`, enable/disable toggles in Settings & Memory Drawer) and scoped `clear_conversation_memory`.
- **Desktop & Browser Automation (`jarvis/actions.py`, `jarvis/api_v2.py`)**: 19-action `ACTION_REGISTRY` covering Win32 window enumeration/focus, media keys, screenshots, and SSRF-hardened `open_url` / 3-tier `browser_action`, with single-use confirmation tokens and high-risk input automation disabled by default (`allow_input_automation = False`).
- **System Telemetry, Proactive Engine & HUD (`jarvis/sysinfo.py`, `jarvis/proactive.py`, `apps/frontend/src/components/OperationsDrawer.tsx`)**: CPU/RAM/GPU/Disk/Battery/Network/Process snapshots, threshold alerts, non-executing proactive HUD cards (`auto_execute=False`), voice clarification guard on low-confidence/script-mismatched speech, and 6 personality presets (`professional`, `friendly`, `concise`, `technical`, `detailed`, `custom`).
- **Domain Reports**: See [FILESYSTEM_TOOLING_REPORT.md](FILESYSTEM_TOOLING_REPORT.md), [MULTI_STEP_EXECUTION_REPORT.md](MULTI_STEP_EXECUTION_REPORT.md), [MEMORY_INTEGRATION_REPORT.md](MEMORY_INTEGRATION_REPORT.md), [DESKTOP_AUTOMATION_REPORT.md](DESKTOP_AUTOMATION_REPORT.md), [SYSTEM_MONITORING_REPORT.md](SYSTEM_MONITORING_REPORT.md), [SECURITY_AUDIT.md](SECURITY_AUDIT.md), and [JARVIS_IMPLEMENTATION_REPORT.md](JARVIS_IMPLEMENTATION_REPORT.md).
