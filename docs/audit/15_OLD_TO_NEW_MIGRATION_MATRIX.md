# 15 — Old-to-New File Migration Matrix

> Maps every existing repository file and subsystem to its target location in `14_TARGET_ARCHITECTURE.md` with explicit action tag (`KEEP`, `MOVE`, `SPLIT`, `MERGE`, `REPLACE`, `DEPRECATE`, `REMOVE`), dependencies, and regression risk.

---

## 1. Backend Modules (`jarvis/**` → `backend/jarvis/**`)

| Current Path | Action | Destination Path(s) | Migration Scope & Notes | Risk |
| :--- | :--- | :--- | :--- | :--- |
| `server.py` | **MOVE** | `apps/backend/main.py` | Keep thin `server.py` root launcher shim until docs & `.ps1` scripts switch | Low |
| `jarvis/config.py` | **SPLIT** | `backend/jarvis/config/settings.py`<br>`backend/jarvis/security/secrets.py` | Separate settings schema/loading from secret encryption (DPAPI + AEAD export) | Med |
| `jarvis/assistant.py` | **SPLIT** | `backend/jarvis/core/event_bus.py`<br>`backend/jarvis/orchestration/turn_service.py`<br>`backend/jarvis/voice/coordinator.py`<br>`backend/jarvis/departments/*` | Break 903L God Class apart; keep temporary facade during `P1–P4` so existing tests pass continuously | High |
| `jarvis/api.py` | **SPLIT** | `backend/jarvis/api/app.py`<br>`backend/jarvis/api/deps.py`<br>`backend/jarvis/api/routers/{chat,voice,system,internet,notes}.py` | Replace closure capture with FastAPI `Depends()`; add explicit `response_model`s & canonical error handler | Med |
| `jarvis/api_workspace.py` | **SPLIT** | `backend/jarvis/api/routers/{voices,memory,files,actions}.py` | Wire via `Depends`; offload blocking FS ops | Med |
| `jarvis/api_learning.py` | **SPLIT** | `backend/jarvis/api/routers/{knowledge,learning,training}.py` | Remove GET read side-effect (`scheduler.evaluate_triggers` inside `/overview`) | Med |
| `jarvis/commands.py` | **DEPRECATE → REMOVE** | Deleted after `P3` | Redundant 7L pass-through to `intent.py` | Low |
| `jarvis/intent.py` | **SPLIT** | `backend/jarvis/decision/rules.py`<br>`backend/jarvis/departments/*/handlers.py` | Separate Layer-1 regex triggers from service business logic | Med |
| `jarvis/brain.py` | **SPLIT / REPLACE** | `backend/jarvis/gateway/{interface,gateway,filters}.py`<br>`backend/jarvis/gateway/providers/*.py` | Preserve `ThinkFilter` + Ollama logic; replace `openai:`/`anthropic:` inline conditionals with extensible provider registry | Med |
| `jarvis/persona.py` | **MOVE / EVOLVE** | `backend/jarvis/prompts/composer.py` | Parameterize per department/agent role + strict 5-way provenance headers | Low |
| `jarvis/audit.py` | **KEEP / ENCAPSULATE** | `backend/jarvis/security/egress_guard.py` | Preserve `sys.addaudithook` invariant; wrap global mutable lists in injectable `AuditService` + DB sink | Med |
| `jarvis/netsec.py` | **KEEP / EXTEND** | `backend/jarvis/security/http_client.py` | Keep IP-pinned `httpcore` + SSRF checks; add `safe_stream()` for SSE cloud providers | Med |
| `jarvis/search.py` | **KEEP / MOVE** | `backend/jarvis/integrations/web_search.py` | Keep 5 search providers; persist robots/rate telemetry safely | Low |
| `jarvis/filesystem.py` | **KEEP / EXTEND** | `backend/jarvis/tools/filesystem.py` | Keep allowlisted jail; add worktree/project scopes + handoff binary formats to doc parsers | Low |
| `jarvis/actions.py` | **SPLIT / PERSIST** | `backend/jarvis/security/permissions.py`<br>`backend/jarvis/orchestration/tasks.py` | Persist `PendingApproval` & `TaskRecord` in PostgreSQL; remove duplicate note CRUD; integrate `EXECUTE` gate with LangGraph | Med |
| `jarvis/notes.py` | **EVOLVE / MERGE** | `backend/jarvis/departments/assistant/notes_service.py` | Replace volatile integer list IDs with persistent UUIDs in Postgres (+ optional `.md` sync) | Med |
| `jarvis/memory_store.py` | **SPLIT** | `backend/jarvis/memory/repositories/personal.py`<br>`backend/jarvis/security/pii_firewall.py` | Move `detect_secret()` to shared outbound/storage security module; move storage from JSONL to Postgres + pgvector | Med |
| `jarvis/history.py` | **REPLACE** | `backend/jarvis/memory/repositories/conversation.py` | Replace JSONL single-thread log with multi-session Postgres `conversation_sessions`/`messages` | Med |
| `jarvis/retrieval.py` | **KEEP / AUGMENT** | `backend/jarvis/memory/hybrid_retriever.py` | Fix ASCII-only regex (`ml` script bug!) and fuse with `pgvector` HNSW cosine similarity | Low |
| `jarvis/voice.py` | **SPLIT** | `backend/jarvis/voice/{loop,vad,audio_devices}.py` | Separate hardware stream capture/recovery from VAD & orchestrator callbacks | High |
| `jarvis/stt.py` | **KEEP / MOVE** | `backend/jarvis/voice/stt.py` | Keep multilingual Whisper & resampling logic verbatim | Low |
| `jarvis/tts.py` | **MOVE / CLEANUP** | `backend/jarvis/voice/{tts,speaker}.py` | Keep `StreamingSpeechBuffer`, Piper pool (bounded), add LRU cleanup on `tts_cache/` | Low |
| `jarvis/aec.py` | **KEEP / MOVE** | `backend/jarvis/voice/aec.py` | Preserve NLMS + far-end buffer cleanly | Low |
| `jarvis/wakeword.py` | **KEEP / MOVE** | `backend/jarvis/voice/wakeword.py` | Preserve verbatim | Low |
| `jarvis/translator.py` | **MOVE / PERSIST** | `backend/jarvis/voice/translator.py` | Persist `TranscriptRecord` history to Postgres `voice_transcripts` table instead of RAM-only deque | Low |
| `jarvis/sysinfo.py` | **KEEP / MOVE** | `backend/jarvis/core/sysinfo.py` | Keep cached RAM/CPU/GPU probe cleanly | Low |
| `jarvis/learning/knowledge_pipeline.py` | **SPLIT / EVOLVE** | `backend/jarvis/departments/assistant/knowledge.py`<br>`backend/jarvis/memory/repositories/knowledge.py` | Migrate SQLite `index.db` -> Postgres + `pgvector`; keep chunking/versioning/rollback semantics | Med |
| `jarvis/learning/feedback.py` | **REPLACE STORAGE** | `backend/jarvis/memory/repositories/experience.py` | Split assistant feedback vs coding post-mortems in Postgres `experience_lessons` | Low |
| `jarvis/learning/evaluation.py` | **KEEP / EXPAND** | `backend/jarvis/learning/evaluation.py` | Keep 12-category safety/regression gate; add coding & tool-calling cases | Low |
| `jarvis/learning/training/{training_manager,training_worker,training_scheduler,training_config,training_audit,dataset_*,adapter_manager,model_registry,rollback}.py` | **MOVE / HARDEN** | `backend/jarvis/learning/...` | Inject paths via DI rather than module globals; persist jobs/models/builds in Postgres; track worker PIDs cleanly | Low |

---

## 2. Frontend, Scripts, Configs & Tests

| Current Path | Action | Destination Path(s) | Migration Scope | Risk |
| :--- | :--- | :--- | :--- | :--- |
| `viewer/index.html`, `viewer/style.css`, `viewer/js/*.js` | **REPLACE incrementally** | `apps/frontend/src/**` (keep `legacy/viewer/` until P8 parity) | Re-architect imperative DOM into React + TS + Vite; isolate `galaxy.js` inside memoized WebGL island | Med |
| `viewer/vendor/*` | **KEEP / MOVE** | `apps/frontend/public/vendor/` | Maintain strict zero-CDN offline bundling | Low |
| `requirements.txt`, `requirements.lock` | **REPLACE** | `pyproject.toml` + pinned lockfiles | Strip BOM, fix missing direct dependencies (`pydantic`, `scipy`, `requests`), add optional `[postgres, agents, docs, dev, train]` groups | Low |
| `config.json` | **KEEP / MOVE** | `configs/default.json` | Add gateway, Postgres/pgvector, LangGraph, OpenCode/Graphify, DR configuration blocks | Low |
| `start-jarvis.ps1`, `stop-jarvis.ps1`, `offline-test.ps1` | **KEEP / MOVE** | `scripts/windows/` | Update entrypoint paths + Postgres readiness check | Low |
| `preflight.py`, `drill.py`, `bench_*.py`, `benchmarks/*` | **KEEP / MOVE** | `scripts/verify/` & `benchmarks/` | Extend `preflight.py` for DB/OpenCode/Graphify/DR checks | Low |
| `tests/*.py` | **KEEP / REORGANIZE** | `tests/{unit,integration,security,e2e}/` | Preserve all 132 existing test scenarios cleanly; split `test_continuous_voice.py` (1048L) into modular files | Low |
| `.playwright-mcp/`, `*.pdf` in root | **CLEANUP / MOVE** | Archive PDFs → `docs/archive/`; delete temp `.playwright-mcp/` captures & ignore | Keeps repo root tidy | Low |
