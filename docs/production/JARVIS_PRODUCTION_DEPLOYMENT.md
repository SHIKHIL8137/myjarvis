# JARVIS Production Deployment, Operational Runbook & Disaster Recovery

**Target Environment**: Windows 11 / Windows 10 (Single User Local-First)  
**Host Target**: AMD Ryzen 5 5600H (6C/12T), NVIDIA GeForce GTX 1650 (4 GB GDDR6), 16 GB DDR4 RAM  
**Repository Working Directory**: `D:\projects\myjarvis`  
**Current Revision**: Production Ready  
**Date**: November 13, 2026  

---

## 1. System Architecture & Boundaries

JARVIS runs as a **single-user, local-first personal AI assistant**. All networking is bound strictly to loopback (`127.0.0.1` / `localhost`). External network traffic occurs strictly via approved, audited outbound HTTPS channels inside **Online Assisted Mode**.

### Component Ports & Protocols
| Service | Host / Port | Transport | Purpose |
| :--- | :--- | :--- | :--- |
| **FastAPI Backend Core** | `127.0.0.1:4700` | HTTP/1.1 & SSE | Assistant OS REST APIs, real-time event streaming (`/api/stream`) |
| **Ollama Local LLM Gateway** | `127.0.0.1:11434` | HTTP/1.1 | Local quantized model inference (`qwen3:4b-instruct`, `qwen3:4b`) |
| **Vite Frontend Dev/Static** | `127.0.0.1:4700` (or `5173`) | Web / WebGL | React 19 UI, Hologram Core, Brain Explorer Canvas, Operations Drawer |
| **PostgreSQL (Optional)** | `127.0.0.1:5432` | TCP (pgvector) | Optional scratch/eval storage when `JARVIS_PG_TEST_URL` is set |

---

## 2. Prerequisites & Environment Setup

### 2.1 Host Dependencies
- **Python**: 3.11+ (Python 3.14.6 supported and verified)
- **Node.js**: Node 20+ (Node 22 / npm 10+ verified)
- **Ollama**: Installed and running on Windows host (`http://127.0.0.1:11434`)
  - Required local weights: `ollama pull qwen3:4b-instruct`
  - Optional thinking model: `ollama pull qwen3:4b`
  - Embeddings: `ollama pull nomic-embed-text`
- **Speech Weights**:
  - Faster-Whisper: Checkpoint downloaded locally to HuggingFace hub cache (`base`, `small`, `small.en`)
  - Piper TTS: ONNX models located in `models/piper/` (e.g. `en_GB-alan-medium.onnx`)
  - Silero VAD: ONNX model in `models/silero_vad.onnx`

### 2.2 Python Virtual Environment
Always activate the workspace virtual environment:
```powershell
Set-Location -Path "D:\projects\myjarvis"
.\.venv\Scripts\Activate.ps1
```

---

## 3. Configuration & Secrets Handling

### 3.1 Safe Configuration Hierarchy
1. Base configuration: `config.json` (version controlled, zero secrets allowed).
2. Runtime overrides: `data/settings_overrides.json` (auto-persisted by `/api/v2/settings` PUT operations).
3. Secrets & API Keys:
   - Stored encrypted on disk using **Windows DPAPI** (`CryptProtectData`) at `data/secrets.dpapi`.
   - Never stored in plaintext files, logs, or git.
   - Keys supported: `openrouter`, `openai`, `anthropic`, `gemini`, `huggingface`, `tavily`, `brave`, `serper`.

### 3.2 Offline vs. Online Assisted Mode
- **Local Mode (Default)**:
  - Total outbound network blocking enforced by `jarvis/netsec.py` and `jarvis/audit.py`.
  - All LLM turns route to local Ollama models.
  - Zero telemetry or external API egress.
- **Online Assisted Mode**:
  - Activated via UI toggle or `/api/internet/mode` (`{"enabled": true}`).
  - Outbound HTTPS strictly restricted to allowlisted hosts (`api.openai.com`, `openrouter.ai`, `router.huggingface.co`, `generativelanguage.googleapis.com`, `docs.python.org`, etc.).
  - Egress subject to PII scrubbing firewall (`scrub_outbound_secrets`) and outbound DNS/IP validation.

---

## 4. Database Lifecycle & Schema Management

### 4.1 SQLite with Write-Ahead Logging (WAL)
- Production database path: `data/jarvis.db`.
- Automatic engine configuration (`jarvis/db/engine.py`):
  - `PRAGMA journal_mode = WAL;` (prevents concurrency deadlocks during voice/turn streaming)
  - `PRAGMA synchronous = NORMAL;` (durable, high-throughput transactions)
  - `PRAGMA busy_timeout = 5000;` (resilient lock acquisition)

### 4.2 Alembic Migrations
Migrations are maintained under `jarvis/db/migrations/versions/`:
- `0001_initial_schema.py`
- `0002_p10_hardening.py`
- `0003_p10_12_skills_and_targets.py`
- `0004_p10_20_filesystem_and_tasks.py`
- `0005_p10_22_durable_tasks.py`
- `0006_voice_pipeline_upgrade.py`
- `0007_p11_project_aliases.py`
- `0008_p11_command_history.py`
- `0009_p1_task_engine_fields.py`
- `0010_p4_routines_and_proactive.py`
- `0011_p5_undo_journal.py` (Current Head)

To check migration status:
```powershell
.\.venv\Scripts\alembic.exe current
```
To upgrade schema:
```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

---

## 5. Startup & Graceful Shutdown

### 5.1 Service Startup
Run the unified server daemon:
```powershell
Set-Location -Path "D:\projects\myjarvis"
.\.venv\Scripts\python.exe server.py
```
To start frontend in development/HMR mode (optional, production build is served directly by backend on port 4700):
```powershell
npm run dev --prefix apps/frontend
```

### 5.2 Health Checks
Verify server health via HTTP:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:4700/api/models" -Method GET
Invoke-RestMethod -Uri "http://127.0.0.1:4700/api/v2/system/overview" -Method GET
Invoke-RestMethod -Uri "http://127.0.0.1:4700/api/v2/runtimes" -Method GET
```

### 5.3 Graceful Shutdown
Send `SIGINT` (Ctrl+C) or `SIGTERM`. The shutdown handler ensures:
1. Active LLM generation streams complete or abort cleanly.
2. Background task checkpoints are flushed to SQLite.
3. Audio capture and TTS output streams are released.
4. Database connections are closed.

---

## 6. Backup & Disaster Recovery

### 6.1 Automated Backup Service
The built-in backup engine (`jarvis/recovery/backup.py`) creates compressed snapshot manifests of:
- `data/jarvis.db`
- `data/settings_overrides.json`
- `notes/`
- `config/`

Run backup on demand:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:4700/api/v2/recovery/backup" -Method POST -Headers @{"X-Jarvis"="1"}
```

### 6.2 Manual Disaster Recovery
If `data/jarvis.db` becomes corrupted:
1. Stop `server.py`.
2. Locate latest backup archive in `data/backups/`.
3. Restore `jarvis.db` from backup snapshot.
4. Run `.\.venv\Scripts\alembic.exe current` to verify head state `0011_p5_undo_journal`.
5. Restart `server.py`.

