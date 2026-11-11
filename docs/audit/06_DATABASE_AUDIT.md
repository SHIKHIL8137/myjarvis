# 06 — Database, Vector Store & Persistence Audit

> **Current Reality Check**: Verified via `pip list`, `sqlite3`, and filesystem scan: **PostgreSQL, Alembic/SQLAlchemy, and Vector Databases (`pgvector` / `qdrant-client` / `chromadb`) are NOT installed or implemented.** `psql` is not installed on the host. Current persistence relies exclusively on flat files (`JSONL`, `JSON`, `Markdown`) and one embedded `SQLite` database (`data/learning/knowledge/index.db`).

---

## 1. Current Persistence Inventory & Risk Assessment

| Store / Path | Engine / Format | Observed State | Read/Write Pattern | Critical Deficiencies |
| :--- | :--- | :--- | :--- | :--- |
| **Knowledge Index**<br>`data/learning/knowledge/index.db` | SQLite (`sources`, `versions`, `chunks`, `sqlite_sequence`) | Schema present; **0 rows** (`sources=0`, `versions=0`, `chunks=0`) | Fresh `sqlite3.connect` per query/upsert; loads all active `chunks` into Python memory for TF-IDF | No FTS5 index, no vector embeddings, ASCII-only tokenizer drops Malayalam, synchronous connection blocking async loop |
| **Personal Memory**<br>`data/memory/records.jsonl` | Append/rewrite JSONL + `backups/` | Flat file | Full file rewrite (`tmp` + `os.replace`) on add/update/confirm/delete; full scan per search | No relational constraints, O(N) search, mixes `"project"`/`"feedback"` records with `"preference"`/`"explicit"`, zero vector similarity |
| **Notes & Graph**<br>`notes/**/*.md` + `notes/.trash/` | Markdown files on disk | 30 active `.md` notes + 5 `.trash` files | `rglob("*.md")` scan with 1s debounce; numeric index IDs (`0..N-1`); O(N^2) link detection | List-index IDs shift when notes change; zero relational tags or backlinks index; no semantic search |
| **Conversation History**<br>`data/history.jsonl` | Append-only JSONL | `26,698 B` | Loads tail window into RAM; linear line scan across file for `search_disk` | Single global timeline (impossible to separate sessions, projects, or multi-agent conversations) |
| **Feedback Store**<br>`data/learning/feedback/records.jsonl` | JSONL | Small records file | Full file rewrite on approve/reject/delete | Conflates conversational style corrections with project coding experience/playbooks |
| **Datasets & Training Metadata**<br>`data/learning/datasets.json`, `registry.json`, `scheduler_state.json`, `training/*/config.json`, `progress.json` | Scattered JSON files | Present (`registry.json`, `scheduler_state.json`) | Independent file reads/writes across 4 classes | No ACID transactions linking `dataset_build` → `training_job` → `eval_report` → `model_version` |
| **Transcripts & Actions & Tasks**<br>`Translator.records`, `ActionExecutor`, `TaskManager` | **In-RAM only** (`OrderedDict`, `dict`) | Volatile | Lost cleanly/silently on server restart | Pending approvals, multi-step tasks, and recent STT transcript history vanish if server restarts |
| **Config & Secrets**<br>`config.json`, `data/settings.json`, `data/secrets.json` | JSON files | Plaintext JSON | Merged at startup + runtime overrides | API keys in `data/secrets.json` stored in **plaintext** (mitigated only by `.gitignore` + OS user ACLs; unsuitable for portable encrypted DR backups) |
| **Audit Logs**<br>`logs/external_audit.jsonl`, `data/learning/training/audit.jsonl` | Append JSONL | `0.04 MB` (`external_audit`) | Separate JSONL streams | No unified queryable audit trail across actions, tool runs, agent decisions, and network calls |

---

## 2. Target Storage Architecture Selection (Hardware-Aware)

### Evaluated Options on 15.35 GB RAM / Windows Host
1. **Relational Core (`PostgreSQL 16+` + `SQLAlchemy 2 async` + `asyncpg` + `Alembic`)**:
   - **Why**: Replaces brittle JSONL rewrites and volatile RAM state (`tasks`, `approvals`, `transcripts`, `agent_runs`) with ACID transactions, foreign-key integrity, concurrent multi-agent row-level writes, and deterministic `pg_dump` snapshots.
   - **Fallback / Dev Mode Guard**: Maintain repository interfaces (`ABC`/`Protocol`) so unit tests can still execute rapidly against in-memory/SQLite test doubles without requiring live Postgres when running isolated CI unit suites.

2. **Vector Store Decision: `pgvector` inside PostgreSQL (Recommended) vs Standalone `Qdrant`**:
   - **Why `pgvector` wins for Jarvis**:
     - Running PostgreSQL + `pgvector` uses **one database engine**, **one backup pipeline (`pg_dump` captures relational + vectors atomically in consistent LSN)**, and **one connection pool** — critical on a 15.35 GB RAM laptop running Ollama (`~2.5–3.5 GB`), Whisper (`~500 MB`), Piper, and React/IDE tooling.
     - Enables **single-query SQL hybrid search**: filter strictly by SQL columns (`store_type = 'project_knowledge'`, `project_id = :pid`, `sensitivity != 'secret'`, `approved = true`) joined directly with HNSW cosine distance + Postgres `tsvector` BM25 full-text search (PostgreSQL also handles custom/multilingual tokenizers cleanly unlike the current `[a-z0-9]+` regex!).
   - **When to reconsider Qdrant**: Only if corpus scales past `>5M` vectors or moves to dedicated server hardware; wrapping access behind a clean `VectorStore` port makes a future Qdrant adapter drop-in if needed.

---

## 3. Target PostgreSQL Schema Design (DDL Blueprint)

### 3.1 Personal Assistant & Workspace Tables
- `user_profiles (id UUID PK, name, timezone, locale, preferred_voice, preferences_jsonb, updated_at)`
- `conversation_sessions (id UUID PK, title, department, project_id FK NULL, active_agent, created_at, updated_at)`
- `conversation_messages (id UUID PK, session_id FK, role, agent_name, content, metadata_jsonb, token_count, created_at)` — indexed `(session_id, created_at)`
- `notes (id UUID PK, title, category, slug UNIQUE, body_md, wikilinks_jsonb, is_trashed, trashed_at, created_at, updated_at)` — replaces volatile integer IDs!
- `personal_memories (id UUID PK, category, scope ['global','temporary'], topic, title, content, source ['explicit','inferred','user_approved'], confirmed BOOL, confidence FLOAT, expires_at NULL, created_at, updated_at)`
- `tasks (id UUID PK, project_id FK NULL, parent_task_id FK NULL, kind, title, status, steps_jsonb, assigned_agent, created_at, completed_at)`
- `reminders (id UUID PK, title, due_at, recurrence_cron NULL, status, channel ['voice','ui','email'], created_at)`
- `voice_transcripts (id UUID PK, session_id FK NULL, original_text, corrected_text, detected_lang, target_lang, translated_text, confidence, audio_ref, created_at)`

### 3.2 Model Gateway & Decision Telemetry Tables
- `model_providers (id PK, name UNIQUE, provider_type, base_url, enabled, daily_token_cap, rpm_limit, metadata_jsonb)`
- `model_routes (id UUID PK, task_class, preferred_tier, provider_id FK, model_name, fallback_provider_id FK, fallback_model_name, enabled)`
- `llm_usage_logs (id UUID PK, session_id FK NULL, agent_run_id FK NULL, decision_layer INT, provider, model, prompt_tokens, completion_tokens, latency_ms, estimated_cost_usd, status, created_at)`

### 3.3 AI Development Workforce & Project Intelligence Tables
- `projects (id UUID PK, name UNIQUE, root_path UNIQUE, default_branch, tech_stack_jsonb, graphify_status, graphify_last_built_at, created_at)`
- `project_graphs (id UUID PK, project_id FK, git_commit_sha, node_count, edge_count, artifact_path, summary_jsonb, built_at)`
- `worktrees (id UUID PK, project_id FK, task_id FK NULL, branch_name, worktree_path UNIQUE, status ['active','merged','cleaned'], created_at)`
- `agent_runs (id UUID PK, session_id FK NULL, project_id FK NULL, agent_role, workflow_name, checkpoint_thread_id, state ['running','awaiting_approval','completed','failed','cancelled'], input_summary, output_summary, started_at, ended_at)`
- `agent_steps (id UUID PK, run_id FK, step_index, node_name, tool_name, input_jsonb, output_jsonb, status, duration_ms, created_at)`
- `approval_requests (id UUID PK, run_id FK NULL, action_type, risk_tier ['READ','WRITE','EXECUTE'], target_resource, preview_diff, status ['pending','approved','rejected','expired'], token_hash, expires_at, decided_by, decided_at)`

### 3.4 5-Way Memory, Knowledge & Fine-Tuning Tables
- `knowledge_sources (id PK, name, kind, location, topic, license, trust_classification, active_version, content_hash, sync_status, last_synced_at)`
- `knowledge_versions (id UUID PK, source_id FK, version_num, content_hash, doc_version, retrieved_at, byte_size)`
- `experience_lessons (id UUID PK, scope ['assistant','dev'], project_id FK NULL, category ['correction','bug_fix','architecture_decision','workflow_lesson'], trigger_context, outcome_before, lesson_after, approved BOOL, source_run_id FK NULL, created_at)`
- `dataset_builds (id PK, build_key UNIQUE, sources_jsonb, target_format, train_count, val_count, test_count, leakage_clean BOOL, sha256, artifact_dir, created_at)`
- `training_jobs (id PK, dataset_build_id FK, base_model, hf_model_id, quantization, status, pid INT NULL, est_vram_gb, approved_by, started_at, ended_at)`
- `eval_reports (id UUID PK, baseline_version_id, candidate_version_id, score_baseline, score_candidate, regressions INT, safety_failures INT, decision, report_jsonb, created_at)`
- `model_versions (id PK, parent_version_id FK NULL, base_model, artifact_type, tag_or_path, sha256, status, eval_report_id FK NULL, created_at)`

### 3.5 Security, Audit & Disaster Recovery Tables
- `audit_events (id UUID PK, actor, category ['network','filesystem','action','agent','auth','training'], decision ['allow','deny','pending'], target, details_jsonb, created_at)`
- `backup_manifests (id UUID PK, tier ['tier1_hot','tier2_warm','tier3_cold'], scope, sha256_manifest, encrypted BOOL, storage_uri, size_bytes, verified_restore BOOL, created_at)`

---

## 4. Vector Store Collections (`pgvector` Isolated Namespaces)

Using a single partitioned `vector_embeddings (id UUID PK, namespace, owner_id, project_id NULL, doc_id, chunk_index, content, metadata_jsonb, embedding vector(768), content_tsv TSVECTOR, updated_at)` (or 5 distinct HNSW-indexed tables/views):

| Vector Namespace / Collection | Purpose | Mandatory Filter Metadata | Isolation & Hygiene Rule |
| :--- | :--- | :--- | :--- |
| `personal_memory` | Long-term facts, user preferences, routines | `user_id`, `category`, `confirmed=true` | Never queried by untrusted external datasets or cross-project dev prompts unless explicitly relevant |
| `project_knowledge` | Code symbols, Graphify community summaries, ADRs, READMEs | `project_id`, `git_branch`, `file_path`, `symbol_type` | **Strictly scoped** to active `project_id`; preventing cross-project leakage |
| `experience_memory` | Post-mortems, solved errors, approved correction lessons | `scope` (`dev`/`assistant`), `project_id?`, `approved=true` | Only `approved=true` and secret-scrubbed records enter index |
| `external_knowledge` | Docs synced by `KnowledgePipeline` (`public_docs`, user manuals) | `source_id`, `version_num`, `trust_classification`, `active=true` | Rollback toggles `active=true/false` cleanly per version |
| `personal_documents` | Extracted chunks from ingested PDF/DOCX/PPTX/XLSX & user notes | `doc_id`, `doc_type`, `sensitivity` | Excludes files matching secret/credential scanners |

**Local Embedding Strategy (4 GB VRAM constraint)**: Run a tiny CPU or Ollama embedding model (`nomic-embed-text` / `all-minilm` on CPU/ONNX, `~100–300 MB` RAM) rather than loading a GPU embedding model alongside `qwen3:4b-instruct`, keeping GPU VRAM dedicated cleanly to inference.

---

## 5. Migration Path from Current JSONL / SQLite

1. Add `repositories/` port interfaces (`MemoryRepo`, `NoteRepo`, `HistoryRepo`, `KnowledgeRepo`, `TrainingRepo`, `AgentRunRepo`, `AuditRepo`) while existing JSONL/SQLite implementations become `LegacyFileAdapters` during P1.
2. Provision PostgreSQL 16 + `pgvector` and Alembic migrations in P2.
3. Build an idempotent CLI migrator `scripts/migrate_file_stores_to_pg.py` that:
   - Reads `notes/**/*.md` → inserts into `notes` generating stable UUIDs + preserving original `path`/`slug`.
   - Reads `data/memory/records.jsonl`, `data/history.jsonl`, `data/learning/feedback/records.jsonl`, `data/learning/datasets.json`, `data/learning/registry.json`, and `data/learning/knowledge/index.db` inside a single DB transaction, verifies row counts + hashes, backs up originals to `backups/pre_pg_migration/`, and switches active repositories via config flag.
