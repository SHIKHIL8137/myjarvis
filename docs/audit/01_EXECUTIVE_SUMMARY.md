# 01 — Executive Architecture & Refactoring Assessment

> **Audit Mode**: Strictly Assessment & Planning Only — **zero existing source code, configs, tests, or database files modified.**
> **Audit Date**: `2026-11-11` | **Target Workspace**: `d:\projects\myjarvis` | **Owner Review**: Required before P0 begins.

---

## 1. Executive Verdict

Jarvis has evolved into a genuinely functional, locally-hardened voice & knowledge assistant: **all 132 automated tests pass (`170.26s`, including live Ollama and real microphone/speaker hardware checks)**, outbound network security (`sys.addaudithook` + IP-pinned HTTPS in `netsec.py`) is unusually thorough, and voice/echo handling (`voice.py` + `aec.py` + multilingual `stt.py`) works smoothly.

However, because features were layered rapidly across phases onto the original minimal FastAPI + JSONL prototype, the system has reached a **structural inflection point**. Expanding into a two-department AI Operating System (Personal Assistant + Document Suite + PostgreSQL/`pgvector` + Multi-Model Gateway + LangGraph Multi-Agent Workforce + OpenCode/Graphify + 3-2-1 Disaster Recovery) **cannot cleanly scale on top of the current `Assistant` God Class (`903L`), monolithic `intent.py` regex chain (`586L`), scattered flat JSONL/SQLite files, and vanilla DOM frontend.**

**Core Architectural Recommendation**: Transition incrementally via **Strangler-Fig Refactoring (`P0–P9`)** into a **Modular Monolith** backed by **PostgreSQL 16 + `pgvector`** and **LangGraph**, keeping `server.py` single-process speed and zero-CDN local security while delegating heavy coding/graph tasks to sandboxed `opencode` (`1.19.5`), `graphify` (`0.9.80`), and `git worktree` subprocesses. Microservices are explicitly **rejected** due to `15.35 GB` RAM (`1.05 GB` free under desktop load) and `4 GB` GTX 1650 VRAM limits.

---

## 2. Measured Baseline Snapshot (Verified `2026-11-11`)

| Dimension | Measured Reality |
| :--- | :--- |
| **Test Suite Health** | **`132 passed` in `170.26s`** (`pytest -v`, exit `0`, 0 failures, 0 skips across 13 files) |
| **Frontend JS Syntax** | **`6/6` custom ES modules pass `node --check`** (`1,359` custom JS lines + React-unconverted `index.html`/`style.css`) |
| **API Surface** | **81 endpoints / 90 HTTP operations** across `api.py` (36 ops), `api_workspace.py`, `api_learning.py` |
| **Backend Codebase** | `41` modules in `jarvis/` (largest: `assistant.py` 903L, `voice.py` 832L, `tts.py` 602L, `intent.py` 586L, `api.py` 572L, `search.py` 569L, `knowledge_pipeline.py` 531L) |
| **Persistence Stores** | Flat files (`data/*.jsonl`, `*.json`, `notes/*.md` [30 notes + 5 `.trash`]) + empty SQLite `index.db` (`0` rows). **0 PostgreSQL / 0 Vector DB.** |
| **Runtime Models & Services** | Ollama `0.40.16` on `127.0.0.1:11434` (`qwen3:4b-instruct`, `qwen2.5vl:3b`, `qwen3:4b`); local Whisper/Piper/Wake weights (`1.14 GB`) |
| **Target CLIs Ready on Host** | **`opencode 1.19.5`**, **`graphify 0.9.80`**, `git`, `node`, `npm`, `docker` installed cleanly; `psql`, `ruff`, `mypy` missing |
| **Pre-Audit Workspace State** | Pre-existing uncommitted edits present on `assistant.py`, `brain.py`, `test_models_and_voices.py`, `learning.js` (untouched; covered cleanly by 132/132 passing tests); stale untracked captures in `.playwright-mcp/` (`27` files, ~3.1 MB) |

---

## 3. Top 10 Critical Findings

1. **`Assistant` God Class (`jarvis/assistant.py`, 903L)**: Owns 20+ stateful subsystems directly, mixing voice queue draining, RAG context assembly, streaming LLM tool loops, and training orchestration with zero dependency inversion.
2. **Router Closure Bug (`jarvis/api.py`)**: `create_workspace_router(app.state.a)` & `create_learning_router(app.state.a)` capture `app.state.a` at startup rather than resolving via FastAPI `Depends()`.
3. **Zero PostgreSQL / Vector DB / LangGraph**: No `psycopg`, `asyncpg`, `sqlalchemy`, `alembic`, `pgvector`, `qdrant-client`, or `langgraph` exists in `.venv` or codebase; RAM-only states (`TaskManager`, `PendingApproval`, `TranscriptStore`) vanish on restart.
4. **Multilingual Retrieval Defect (`jarvis/retrieval.py`)**: Regex `re.compile(r"[a-z0-9]+")` strips all Malayalam Unicode tokens (`0D00..0D7F`), causing zero lexical retrieval matches for Malayalam queries even though `knowledge_pipeline.py` detects Malayalam script!
5. **Dual Unsynchronized Personal Memory (`notes/` vs `records.jsonl`)**: `REMEMBER` dual-writes without transaction consistency; `NoteStore` assigns unstable integer list-index IDs (`/api/notes/{note_id}`) that shift whenever notes are added/removed.
6. **GET Endpoint Disk Side-Effect**: `GET /api/learning/overview` calls `training_scheduler.evaluate_triggers(...)`, writing `scheduler_state.json` and appending to `audit.jsonl` every 5 seconds while the UI drawer is open.
7. **Flat Decision & Gateway Limitations**: Regex-only `intent.py` (`586L`) lacks Layer-2 structured LLM intent classification & Layer-3 model capability routing; `Brain` lacks streaming cloud support, OpenRouter/HuggingFace/Gemini/OpenCode adapters, token budgets, and rate governance.
8. **Missing Security Pieces for External / Agentic Era**: `data/secrets.json` stores keys in plaintext JSON; `detect_secret()` guards memory/training ingestion but **not** outbound prompts (`OpenRouter`/`Gemini`/Web Search); static code scan is otherwise clean (`0` `shell=True`, `verify=False`, or unsafe `eval`).
9. **Missing Personal Assistant & Dev Workforce Engines**: Zero PDF/DOCX/PPTX/XLSX generation or parsing; zero email/calendar/reminder ports; zero Graphify/OpenCode/Git-worktree orchestration despite `graphify 0.9.80` and `opencode 1.19.5` already being installed locally.
10. **Packaging & Doc Drift**: `requirements.txt` contains a UTF-8 BOM (`\ufeff`) and omits direct imports (`pydantic`, `scipy`, `requests`) present only in `requirements.lock`; `README.md` (lines 38–41) & `DOCUMENTATION_ANALYSIS.md` contain obsolete pre-upgrade statements (`OLLAMA_HOST=0.0.0.0`, "no web research").

---

## 4. Deliverable Index (`docs/audit/`)

| Report | Topic |
| :--- | :--- |
| [01_EXECUTIVE_SUMMARY.md](file:///d:/projects/myjarvis/docs/audit/01_EXECUTIVE_SUMMARY.md) | Executive verdict, measured baseline, top 10 findings, index |
| [02_EXISTING_ARCHITECTURE.md](file:///d:/projects/myjarvis/docs/audit/02_EXISTING_ARCHITECTURE.md) | System diagram, chat/voice/security flows, monolith vs microservice decision |
| [03_SOURCE_FILE_INVENTORY.md](file:///d:/projects/myjarvis/docs/audit/03_SOURCE_FILE_INVENTORY.md) | File-by-file inventory across all 64 `.py`, UI, configs, scripts, docs, runtime dirs |
| [04_BACKEND_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/04_BACKEND_AUDIT.md) | All 81 paths / 90 API ops, DI flaws, error inconsistencies, sync blocking issues |
| [05_FRONTEND_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/05_FRONTEND_AUDIT.md) | `viewer/` audit & incremental React + TS + Vite migration preserving WebGL 3D galaxy |
| [06_DATABASE_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/06_DATABASE_AUDIT.md) | Current flat-file/SQLite DDL audit + proposed PostgreSQL & `pgvector` schemas |
| [07_MODEL_GATEWAY_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/07_MODEL_GATEWAY_AUDIT.md) | 3-Layer Fast Decision Engine + multi-provider Gateway (Ollama/OpenRouter/HF/Gemini/OpenCode) |
| [08_AGENT_SYSTEM_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/08_AGENT_SYSTEM_AUDIT.md) | Assistant & Dev agents, LangGraph checkpoints/interrupts, PDF/DOCX/PPTX/XLSX engine |
| [09_PROJECT_INTELLIGENCE_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/09_PROJECT_INTELLIGENCE_AUDIT.md) | Multi-project registry, Graphify `0.9.80`, OpenCode `1.19.5`, Git worktrees, 12-step dev flow |
| [10_MEMORY_AND_LEARNING_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/10_MEMORY_AND_LEARNING_AUDIT.md) | Strict 5-way separation (Personal/Project/Experience/KB/Training) & multilingual fix |
| [11_DISASTER_RECOVERY_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/11_DISASTER_RECOVERY_AUDIT.md) | 3-2-1 backup tiers, DPAPI/AEAD secret vault, SHA-256 manifests, restore drill |
| [12_SECURITY_AUDIT.md](file:///d:/projects/myjarvis/docs/audit/12_SECURITY_AUDIT.md) | Threat model, static scan, egress secret firewall, `READ/WRITE/EXECUTE` matrix |
| [13_TEST_REPORT.md](file:///d:/projects/myjarvis/docs/audit/13_TEST_REPORT.md) | Live `pytest -v` (`132 passed`), `node --check`, `pip check`, CLI & missing-tool report |
| [14_TARGET_ARCHITECTURE.md](file:///d:/projects/myjarvis/docs/audit/14_TARGET_ARCHITECTURE.md) | Modular Monolith directory layout & data-flow diagram |
| [15_OLD_TO_NEW_MIGRATION_MATRIX.md](file:///d:/projects/myjarvis/docs/audit/15_OLD_TO_NEW_MIGRATION_MATRIX.md) | File-by-file `KEEP/MOVE/SPLIT/MERGE/REPLACE/DEPRECATE/REMOVE` matrix |
| [16_IMPLEMENTATION_ROADMAP.md](file:///d:/projects/myjarvis/docs/audit/16_IMPLEMENTATION_ROADMAP.md) | Phased `P0–P9` engineering roadmap |
| [17_RISKS_AND_DEPENDENCIES.md](file:///d:/projects/myjarvis/docs/audit/17_RISKS_AND_DEPENDENCIES.md) | Python 3.14 wheel risks, 16GB/4GB RAM/VRAM budget, package matrix |
| [18_ACCEPTANCE_CRITERIA.md](file:///d:/projects/myjarvis/docs/audit/18_ACCEPTANCE_CRITERIA.md) | Hard quantitative & functional gates per phase before production sign-off |
