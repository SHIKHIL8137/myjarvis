# Jarvis — Local-First Personal AI Assistant & Multi-Agent OS

Jarvis runs local-first on Windows (Ollama local LLM inference, `faster-whisper` multilingual STT, Piper TTS, `"Hey Jarvis"` wake word via openWakeWord, NLMS echo cancellation + safe half-duplex mic loop), with optional explicit **Online Assisted Mode**, **Multi-Model Gateway**, **5-Way Memory + Learning Pipeline**, **Dev Workforce (Graphify + OpenCode + Git Worktrees)**, and **3-2-1 Disaster Recovery**.

```powershell
.\start-jarvis.ps1      # starts the backend on http://127.0.0.1:4700/
.\stop-jarvis.ps1       # stops it cleanly (Ollama stays untouched)
```

## Key Modes & Guarantees

- **Local Mode (default)**: `sys.addaudithook` blocks all non-loopback sockets; Ollama binds to `127.0.0.1:11434`.
- **Online Assisted Mode (explicit opt-in)**: IP-pinned HTTPS (`netsec.py`), redirect & SSRF blocking, outbound secret/PII firewall, and external audit logging.
- **Two Departments**:
  - **Personal Assistant**: Voice conversation, multilingual translation, 5-way memory separation, PDF/DOCX/PPTX/XLSX generation & parsing, reminders/email/calendar ports.
  - **AI Development Workforce**: Multi-project registry, Graphify architecture graphs, OpenCode delegated execution inside isolated Git worktrees, QA test verification, and `EXECUTE` approval gates.

## Quick Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe preflight.py
.\offline-test.ps1
```

See [docs/audit/01_EXECUTIVE_SUMMARY.md](docs/audit/01_EXECUTIVE_SUMMARY.md) for the full architecture & P0–P9 migration specification.
