# 18 — Migration Verification & Final Acceptance Criteria

> Every migration phase (`P0`–`P9`) is gated by concrete, automated, and human-verifiable pass criteria below. No phase closes on unexecuted or mocked-only proof when hardware/live verification applies.

---

## 1. Architecture & Code Quality Gates (`P0`, `P1`, `P9`)
- [ ] **Zero BOM & Complete Packaging**: `requirements.txt` / `pyproject.toml` passes `pip check` on clean install with zero missing direct imports (`pydantic`, `scipy`, `requests` resolved explicitly).
- [ ] **Lint & Type Safety**: `ruff check` passes with `0` errors; `mypy` passes cleanly across `backend/jarvis/{config,api,gateway,decision,security,memory,agents}/`.
- [ ] **DI & Clean Boundaries**: `Assistant` monolith (`903L`) retired; no router closes over mutable globals; circular import check (`python -c`) exits `0`; `GET` endpoints perform zero disk write side-effects.
- [ ] **Legacy Compatibility**: All 132 baseline tests remain green (updated cleanly where refactored) + zero regression in voice state machine, Whisper STT, Piper TTS, and NLMS AEC.

## 2. Persistence & 5-Way Memory Separation Gates (`P2`, `P5`)
- [ ] **Reversible Migrations**: `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` executes cleanly on PostgreSQL `16+` with `pgvector`.
- [ ] **Zero-Loss Data Import**: `scripts/migrate_file_stores_to_pg.py` imports all `notes/*.md`, `data/memory/records.jsonl`, `data/history.jsonl`, `feedback/records.jsonl`, `index.db`, `registry.json` with 100% checksum & row-count match.
- [ ] **Strict Isolation**: Integration test proves Project A Graphify/knowledge chunks **never** appear in Project B queries or Personal Assistant general recall; temporary personal memories respect TTL expiration.
- [ ] **Multilingual Hybrid Recall**: Malayalam (`മലയാളം`), English, and code-mixed queries retrieve relevant chunks from `pgvector` + Unicode lexical search (fixing the `[a-z0-9]+` bug).

## 3. Fast Decision & Multi-Model Gateway Gates (`P3`)
- [ ] **Decision Latency**: Layer-1 deterministic controls (`stop`, `pause mic`, `confirm action`, `/mode`) resolve in `< 5 ms`; Layer-2 structured JSON classifier resolves cleanly under `500 ms` warm local Ollama.
- [ ] **Provider Switching & Fallback**: Live test verifies Ollama + at least one configured Online adapter (OpenRouter / Gemini / HuggingFace / OpenAI / Anthropic) streaming tokens with token/latency logged to `llm_usage_logs` and automatic fallback on simulated HTTP `429/503`.
- [ ] **Strict Offline Guarantee**: In `Local Mode`, all cloud gateway calls fail closed immediately (`NetworkSecurityError`) with zero outbound non-loopback packets (`sys.addaudithook`).

## 4. Personal Assistant & Agent Workforce Gates (`P4`, `P5`, `P6`)
- [ ] **Document Engine**: Agent generates real opening PDF (`reportlab`), DOCX (`python-docx`), PPTX (`python-pptx`), and XLSX (`openpyxl` with formula sanitization) artifacts inside `output/documents/`, and parses uploaded PDF/DOCX/XLSX into `personal_documents`.
- [ ] **Graphify + OpenCode + Worktree E2E**: On a sample registered repo:
  1. Incremental `graphify update` succeeds offline (`< 30s`).
  2. Architect queries `graphify query` + `graphify affected`.
  3. OpenCode executes fix strictly inside `.jarvis_worktrees/<task_id>` (main working tree stays clean `git status`!).
  4. Jarvis QA verifier executes tests & captures exit code `0`.
  5. LangGraph pauses via `interrupt()` awaiting `EXECUTE` merge approval; surviving simulated server restart (`uvicorn` restart) → user approves in UI → worktree merges cleanly & cleans up.

## 5. Security, Disaster Recovery & UI Gates (`P7`, `P8`, `P9`)
- [ ] **Encrypted Secrets & Egress Firewall**: Zero plaintext secrets stored on disk or inside DR backups; deliberate injection of fake `sk-...` / `AIza...` / `hf_...` token into chat context or retrieved file is redacted before leaving via external Search or Cloud Gateway tests.
- [ ] **Verified 3-2-1 Restore Drill**: `jarvis-dr backup` creates signed `manifest.json` + encrypted secrets vault + `db.dump` + artifacts; `jarvis-dr verify` restores into clean scratch Postgres instance passing hash & row-count verification without errors.
- [ ] **React Frontend Parity & Offline Invariant**: Vite production build passes TypeScript check (`tsc --noEmit`), Playwright smoke flows (Chat streaming, Voice mode toggle, 3D Galaxy render staying mounted across turns, Workforce Approval Queue, Memory 5-way tabs, DR view), and automated zero-CDN inspection (`test_viewer_has_no_cdn_or_external_urls`).
- [ ] **Hardware Footprint Check (`15.35 GB` RAM / `4 GB` VRAM)**: Sustained end-to-end voice + Postgres + React session operates smoothly without OOM kills or unrecovered mic/Ollama crashes.
