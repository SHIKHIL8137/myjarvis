# Architecture — Jarvis Modular Monolith & Multi-Agent AI OS

One local-first FastAPI process (`127.0.0.1:4700`) orchestrates real-time voice I/O, the React + TypeScript offline UI (`apps/frontend/dist`, with legacy fallback at `/legacy`), local Ollama inference (`127.0.0.1:11434`), SQLAlchemy/Alembic persistence (PostgreSQL 18 or SQLite WAL), LangGraph stateful multi-agent workflows, sandboxed Dev Workforce integrations (`graphify`, `opencode`, `git worktree`), and 3-2-1 encrypted disaster recovery.

```text
React UI (apps/frontend/)  <--HTTP/SSE-->  jarvis/api.py + api_deps.py + api_v2.py
                                               |-- Assistant (assistant.py) -> EventBus (core/event_bus.py)
                                               |-- Voice Pipeline: voice.py, stt.py, tts.py, aec.py, wakeword.py, translator.py
                                               |-- 3-Layer Decision Engine: decision/{rules,classifier,router}.py
                                               |-- Multi-Model Gateway: gateway/{gateway,providers,interface}.py + brain.py
                                               |-- 5-Way Memory & DB: db/{engine,models,migrations} + memory/{five_way,vectors,legacy_migrator}.py
                                               |-- Personal Assistant Dept: departments/assistant/{documents,reminders,productivity}.py
                                               |-- AI Dev Workforce Dept: departments/dev/workforce.py + integrations/{graphify_adapter,opencode_adapter,git_worktree}.py
                                               |-- LangGraph Agents & Approvals: agents/{orchestrator,assistant_graph,workforce_graph,checkpointer,approvals}.py
                                               |-- Security & Secrets: security/{secrets,pii_firewall}.py + audit.py + netsec.py
                                               `-- Disaster Recovery: recovery/backup.py + scripts/bootstrap.ps1
```

## Key Subsystems

1. **Dependency Injection (`jarvis/api_deps.py`, `jarvis/api_workspace.py`, `jarvis/api_learning.py`, `jarvis/api_v2.py`)**: All routers resolve `app.state.a` dynamically via FastAPI `Depends`. Canonical domain exceptions map through [`jarvis/core/errors.py`](jarvis/core/errors.py).
2. **5-Way Separated Persistence (`jarvis/db/`, `jarvis/memory/`)**: Strict isolation across `personal_memory`, `project_knowledge` (scoped by `project_id`), `experience_memory` (`assistant` vs `dev`), `external_knowledge`, and `personal_documents`, indexed with Unicode-aware lexical + dense hybrid search (`jarvis/retrieval.py`, `jarvis/memory/vectors.py`).
3. **3-Layer Decision & Multi-Model Gateway (`jarvis/decision/`, `jarvis/gateway/`)**:
   - **Layer 1**: Sub-5ms deterministic controls & slash commands.
   - **Layer 2**: Heuristic + structured JSON intent classifier.
   - **Layer 3**: Capability/offline-aware router across Ollama (`127.0.0.1:11434`) and explicitly gated OpenRouter, HuggingFace, Gemini, OpenAI, Anthropic, and OpenCode providers—protected by outbound PII/Secret Firewall ([`jarvis/security/pii_firewall.py`](jarvis/security/pii_firewall.py)), rate limits, daily token budgets, and usage logging.
4. **LangGraph Multi-Agent Workflows & Safe Dev Execution (`jarvis/agents/`, `jarvis/integrations/`)**:
   - Assistant workflow handles document export (PDF, DOCX, PPTX, XLSX), reminders, and approval-gated email drafts.
   - Workforce 12-step DAG syncs Graphify AST graphs, executes OpenCode strictly inside isolated Git worktrees (`.jarvis_worktrees/`), verifies via test runner (`pytest`/`ruff`), security-scans diffs, and pauses durably at LangGraph `interrupt()` backed by SQL checkpointing until human merge approval.
5. **Security & 3-2-1 Disaster Recovery (`jarvis/security/`, `jarvis/recovery/`)**:
   - Provider API keys encrypted via Windows DPAPI at rest (`data/secrets.json`) and portable `AES-256-GCM` PBKDF2 vaults inside ZIP + SHA-256 `manifest.json` DR bundles with isolated scratch verify drills.
