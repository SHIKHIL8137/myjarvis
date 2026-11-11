# 17 — Technical Risks, Dependencies & Hardware Constraints

---

## 1. Host Resource & Hardware Constraints (Measured Baseline)

| Resource | Measured Value | Architectural Implication |
| :--- | :--- | :--- |
| **System RAM** | `15.35 GB` total (**`1.05 GB` free** observed with desktop + IDE + server!) | **High Risk**: Heavy multi-container microservice stacks (separate Postgres + Qdrant + Redis + Vite + Electron + multiple Python workers + Ollama) will trigger severe Windows swap thrashing and voice latency spikes. **Mitigation**: Keep Modular Monolith + single Postgres (`pgvector`) instance + lightweight React browser tab + CPU-tiny embedding model (`nomic-embed-text` / `all-minilm` <= 300 MB). |
| **GPU VRAM** | NVIDIA GTX 1650 (`4.0 GB` VRAM) | Only **one** quantized 4B LLM (`qwen3:4b-instruct` Q4_K_M ~`2.6 GB`) + KV cache fits safely alongside Windows DWM/browser VRAM. Local PyTorch 4-bit QLoRA training cannot share VRAM with active Ollama inference (`training_manager.py` estimates & blocks safely). |
| **Python 3.14.6 on Windows** | Cutting-edge ABI (`cp314`) | Binary C/CUDA wheels (`bitsandbytes`, `webrtc_audio_processing`, `speexdsp`, some vector/ML packages) lack prebuilt `cp314` Windows wheels! Verify wheel availability (`psycopg`, `asyncpg`, `pgvector`, `pymupdf`, `reportlab`, `langgraph`, `ruff`) BEFORE locking `P2`/`P5`/`P6` upgrades — **fallback to Python `3.12` venv for heavy ML/DB components or isolated Docker worker if `cp314` wheels fail!** |

---

## 2. Dependency Matrix & Readiness Audit

### Currently Installed in `.venv`
- `fastapi 0.162.4`, `uvicorn 0.54.0`, `pydantic 2.14.3`, `httpx 0.28.1`, `httpcore 1.0.9`, `anyio 4.16.0`, `requests 2.34.2`
- `faster-whisper 1.2.1`, `ctranslate2 4.9.0`, `piper-tts 1.8.0`, `openwakeword 0.6.0`, `onnxruntime 1.31.1`, `sounddevice 0.5.6`, `numpy 2.5.4`, `scipy 1.18.1`, `scikit-learn 1.9.1`
- `huggingface_hub 2.4.0`, `PyYAML 6.0.3`, `pytest 9.2.0`

### Required New Dependencies by Migration Phase
| Phase | Package / Service | Purpose | Risk / Fallback |
| :--- | :--- | :--- | :--- |
| **P0** | `ruff`, `mypy`, `pip-audit` | Static quality, typing, CVE scanning | Low risk (or install cleanly with `pip` / `uv`) |
| **P2** | PostgreSQL `16+` + `pgvector` extension, `sqlalchemy[asyncio]>=2.0`, `asyncpg` (or `psycopg[binary]`), `alembic`, `pgvector` | Normalized ACID persistence + hybrid vector search | Verify `asyncpg`/`psycopg` `cp314` wheels; run Postgres via Docker (`docker` IS present on host) or official Windows installer |
| **P3** | (`httpx`/`httpcore` already installed) + `tenacity` (optional) | Multi-model SSE gateway + retries | Needs OpenRouter / Gemini / HF API keys configured by user at runtime |
| **P4** | **External CLIs already installed**: `opencode 1.19.5`, `graphify 0.9.80`, `git` | Coding workforce + code knowledge graph + worktrees | Windows `opencode.ps1` execution policy / `PYTHONUTF8=1` handling in `Popen`; monitor OpenCode CLI output schema changes across upgrades |
| **P5** | `pymupdf` (or `pypdf`), `reportlab`, `python-docx`, `python-pptx`, `openpyxl`, `pandas`, `cryptography`, `apscheduler` | Document read/export + reminders + DPAPI/AEAD | Verify `pymupdf`/`pandas` `cp314` wheels (`pypdf` + `reportlab` are pure Python fallbacks!) |
| **P6** | `langgraph`, `langgraph-checkpoint-postgres` | Durable multi-step agents & human interrupts | Confirm API stability across `langgraph` minor releases; isolate behind `orchestration/` port |
| **P8** | Node `v20+` / Vite / React / TypeScript (`node` & `npm` already installed!) | Modern typed frontend | Keep `viewer/` working until React bundle hits 100% parity & passes zero-CDN check |

---

## 3. Architectural & Operational Risks + Mitigations

| ID | Risk | Severity | Likelihood | Mitigation Strategy |
| :--- | :--- | :---: | :---: | :--- |
| **R1** | **Python 3.14 Wheel Incompatibility** for `pymupdf`, `asyncpg`, `pandas`, or `torch` | **HIGH** | High | Run wheel pre-check at start of `P0`; prioritize pure-Python libs (`pypdf`, `reportlab`, `psycopg` vs `asyncpg`), or adopt Python `3.12` venv for backend if `asyncpg`/`pymupdf`/`langgraph` native extensions fail on `3.14`. |
| **R2** | **RAM Exhaustion (`1.05 GB` free RAM)** when running Postgres + Ollama + Whisper + React Vite + OpenCode simultaneously | **HIGH** | High | Keep Ollama single-resident (`keep_alive` tuning), run Postgres cleanly capped (`shared_buffers=256MB`), avoid concurrent Whisper+OpenCode+Vite dev servers in prod (serve compiled React static bundle via FastAPI!). |
| **R3** | **Monolith Refactor Regressing Continuous Voice Timing** (`assistant.py` ↔ `voice.py`) | **HIGH** | Medium | Never alter `VoiceLoop` audio buffer / AEC state machine during `P1`; extract interfaces around existing voice queues and keep all 16 tests in `test_continuous_voice.py` + `test_speech.py` green at every commit. |
| **R4** | **Egress Secret Leak to Cloud Providers / OpenCode** | **CRITICAL** | Medium | Enforce outbound `pii_firewall` scan on every gateway/search request and strip sensitive env vars when spawning OpenCode subprocesses. |
| **R5** | **JSONL / SQLite → Postgres Data Migration Loss** | **HIGH** | Low | Keep dual-write or read-only backup bundle in `Tier 1` before running transactional importer; validate exact row counts & hashes (`06_DATABASE_AUDIT.md`). |
| **R6** | **Malayalam Retrieval Misses** (`[a-z0-9]+` tokenizer) | **MEDIUM** | High (Certain) | Fix `jarvis/retrieval.py` Unicode word tokenization immediately in `P1/P2` before adding vector embeddings. |
