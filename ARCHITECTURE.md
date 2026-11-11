# Architecture — Jarvis Modular Monolith & Multi-Agent AI OS (P10 Hardened)

One local-first FastAPI process (`127.0.0.1:4700`) orchestrates real-time voice I/O, the React + TypeScript offline UI (`apps/frontend/dist`, with legacy fallback at `/legacy`), local Ollama inference (`127.0.0.1:11434`), SQLAlchemy/Alembic persistence (PostgreSQL 18 + native `pgvector` or local SQLite WAL), LangGraph stateful multi-agent workflows, sandboxed Dev Workforce integrations (`graphify`, `opencode`, `git worktree`), controlled knowledge & model lifecycle pipelines, and 3-2-1 authenticated disaster recovery.

```text
React UI (apps/frontend/)  <--HTTP/SSE-->  jarvis/api.py + api_deps.py + api_v2.py
                                               |-- Assistant (assistant.py) -> EventBus (core/event_bus.py) + ResourceGovernor (core/resources.py)
                                               |-- Voice Pipeline: voice.py, stt.py, tts.py, aec.py, wakeword.py, translator.py
                                               |-- 3-Layer Decision Engine: decision/{rules,classifier,router}.py
                                               |-- Multi-Model Gateway: gateway/{gateway,providers,interface}.py + netsec.safe_stream_lines + brain.py
                                               |-- 5-Way Memory & DB: db/{engine,models,migrations} + memory/{embeddings,vectors,five_way,legacy_migrator}.py
                                               |-- Personal Assistant Dept: departments/assistant/{documents,reminders,productivity}.py
                                               |-- AI Dev Workforce Dept: departments/dev/workforce.py + integrations/{graphify_adapter,opencode_adapter,git_worktree}.py
                                               |-- LangGraph Agents & Approvals: agents/{orchestrator,assistant_graph,workforce_graph,checkpointer,approvals}.py
                                               |-- Learning & Registry: learning/{knowledge_pipeline,feedback,evaluation}.py + learning/training/
                                               |-- Security & Secrets: security/{secrets,pii_firewall}.py + audit.py + netsec.py
                                               `-- Disaster Recovery: recovery/backup.py + scripts/bootstrap.ps1
```

## Key Subsystems & P10 Hardening

1. **Dependency Injection & Resource Governance (`jarvis/api_deps.py`, `jarvis/core/resources.py`)**: All routers resolve `app.state.a` dynamically via FastAPI `Depends`; canonical domain errors map via [`jarvis/core/errors.py`](jarvis/core/errors.py). [`ResourceGovernor`](jarvis/core/resources.py) caps concurrent local Ollama heavy inference (`max_local_concurrency=1`) and queue depth to protect GTX 1650 4 GB VRAM and 16 GB system RAM.
2. **Real PostgreSQL + `pgvector` & Embeddings (`jarvis/db/`, `jarvis/memory/`)**:
   - [`VectorStore`](jarvis/memory/vectors.py) provides `SQLiteVectorStore` locally and `PgVectorStore` (native PostgreSQL `vector(dim)` columns, `HNSW`/`IVFFlat` indexes, database-side `<=>`/`<->`/`<#>` similarity operators, SQL expiry & JSONB metadata filtering without full-table Python scans).
   - [`EmbeddingProvider`](jarvis/memory/embeddings.py) decouples local Ollama (`/api/embed`), local Hugging Face, and explicitly versioned deterministic `hash-v1:256` fallback while rejecting mismatched vector dimensions or incompatible models.
   - [`FiveWayMemoryService`](jarvis/memory/five_way.py) enforces `project_id` isolation, vector+SQL expiry filtering, scheduled `purge_expired()`, duplicate detection without blind auto-merge, explicit supersession, audit trails, and configurable multi-factor ranking. [`migrate_sqlite_to_postgres`](jarvis/memory/legacy_migrator.py) migrates records/vectors non-destructively with count & dimension verification.
3. **3-Layer Decision & Streaming Gateway (`jarvis/decision/`, `jarvis/gateway/`)**:
   - Real chunk-by-chunk SSE streaming across OpenAI-compatible, Gemini (`+ tools`), Anthropic, and Ollama via `safe_stream_lines` in [`jarvis/netsec.py`](jarvis/netsec.py).
   - Dynamic capability registry, circuit breakers, exponential backoff with jitter, cancellation tokens, free-cloud preference & paid-model lock (`providers.allow_paid_models=false`), and mandatory central egress PII/secret + project privacy firewall (`ModelGateway.enforce_egress_firewall`).
4. **Hardened Development Workforce (`jarvis/agents/workforce_graph.py`, `jarvis/integrations/git_worktree.py`, `jarvis/departments/dev/workforce.py`)**:
   - Explicit 12-state DAG (`PLANNING` -> `RUNNING` -> `QA_PENDING`/`QA_FAILED` -> `SECURITY_REVIEW` -> `APPROVAL_PENDING` -> `APPROVED` -> `MERGING` -> `COMPLETED` / `BLOCKED` / `FAILED` / `CANCELLED`) with `validate_transition`. Failed QA or exhausted retries halt before approval; owner approval cannot override failed QA or security findings.
   - Unified change manifest scans tracked diffs, staged/unstaged files, untracked files, renames, binary markers, file modes, and symlinks without logging raw secret values. Approvals pin `manifest_sha256` and auto-invalidate on any subsequent edit.
   - [`ExecutionPolicy`](jarvis/integrations/git_worktree.py) enforces allowed/denied executables, directory bounds, filtered env vars, and optional Docker `--network=none` isolation, while honestly reporting Windows native subprocess isolation limitations.
5. **Controlled Learning & Registry (`jarvis/learning/`)**:
   - RAG knowledge updates (`KnowledgePipeline`) are separated from optional weight training (`TrainingManager`, off by default). Enforces license verification (`allowed_licenses`), prompt-injection rejection (`detect_poisoning`), `<retrieved_knowledge untrusted_data="true">` quarantine delimiters, global cross-source chunk deduplication, vector sync (`external_knowledge`), remote training bundle delegation (`prepare_remote_delegation_bundle`), mandatory 12-category evaluation before candidate promotion, `auto_promote=false` lock, and atomic rollback.
6. **3-2-1 Authenticated Disaster Recovery (`jarvis/recovery/backup.py`, `scripts/bootstrap.ps1`)**:
   - Supports SQLite online backup + PostgreSQL native `pg_dump`/`pg_restore`, vector/memory/workflow/project JSONL snapshots, optional Git repository bundles, AES-256-GCM DPAPI + `.jvault` encrypted archives, passphrase-keyed HMAC-SHA256 authenticity manifests, optional secondary & S3 destinations, retention rotation, and isolated scratch `verify_backup` drills reporting RPO, RTO, memory/vector probe health, and checkpoint recovery.
