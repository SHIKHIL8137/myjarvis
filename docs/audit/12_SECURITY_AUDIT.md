# 12 — Security Architecture, Permissions & Hygiene Audit

> **Audit Scope**: Static code security scan across all 64 Python files and 6 custom JS modules, runtime `audit.py` + `netsec.py` + `filesystem.py` + `actions.py` review, and dependency/artifact hygiene inspection.

---

## 1. Static Security Scan Results (Observed Clean Baseline)

Static regex scan ran across `server.py`, `jarvis/**/*.py`, and `viewer/js/*.js` (excluding minified vendor bundle):
- `shell=True`: **0 occurrences** (both `actions.py:272,281` and `training_manager.py:184` pass `shell=False` / list argv).
- `os.system`: **0 occurrences**.
- `eval(` / `exec(`: **0 occurrences** in authored Python/JS.
- `verify=False` (TLS disablement): **0 occurrences** (`netsec.py` enforces `ssl.create_default_context()`).
- Unsafe deserialization (`pickle.load` / `yaml.load`): **0 occurrences**.
- SQL f-string interpolation: **0 occurrences** (`knowledge_pipeline.py` uses parameterized `?` queries).
- Outbound plaintext `http://` in active runtime calls: **0 occurrences** (only local `127.0.0.1:11434` Ollama and SVG XML namespace URI `http://www.w3.org/2000/svg` in `main.js:37`).
- Non-vendor `innerHTML`: **1 occurrence** (`viewer/js/md.js:49` after HTML escaping).
  - **Low-Severity Hardening Note (`md.js`)**: Ensure any future Markdown link renderer rejects non-`https://`/`http://` href schemes (`javascript:`, `data:`) in regex link substitution.

---

## 2. Existing Strong Security Controls (MUST PRESERVE)

1. **`sys.addaudithook` Outbound Kill-Switch (`jarvis/audit.py`)**:
   - Blocks non-loopback `socket.connect` & `socket.getaddrinfo` by default (`Local Mode`).
   - Requires explicit scoped `contextvars` context (`audit.allow_outbound(...)`) even in `Online Assisted Mode`.
2. **SSRF, DNS Rebinding & Redirect Pinning (`jarvis/netsec.py`)**:
   - HTTPS + port 443 only; blocks credentials in URL; blocks `.local`/`.internal`/`localhost`/private/link-local/CGNAT/reserved IP ranges; pins validated public IP directly inside `_PinnedAsyncBackend`; disables auto-redirects and strips sensitive headers across cross-host hops; enforces `max_fetch_bytes`.
3. **Filesystem Jail & Soft-Delete (`jarvis/filesystem.py`, `jarvis/notes.py`)**:
   - Path resolution via `.resolve()` + `.is_relative_to(root)`, excludes `.git`, `.venv`, `node_modules`, `.env`, `secrets.json`, `.ssh`, private keys (`*.pem`, `*.key`); soft-deletes notes to `notes/.trash/`.
4. **Dataset Poisoning & Safe Extraction (`dataset_manager.py`, `dataset_validator.py`)**:
   - Blocks zip-slip (`..`), blocks `.pkl/.pt/.exe/.dll/.ps1/.sh`; checks SHA-256; rejects prompt-injection smuggling (`<|im_start|>system`, `rm -rf`, `curl | bash`); runs `detect_secret()`.

---

## 3. Target Security Gaps for Agentic AI OS Expansion

| Gap Area | Current Exposure | Required Target Control (`P1–P7`) |
| :--- | :--- | :--- |
| **Plaintext `data/secrets.json`** | API keys (`tavily`, `brave`, `serper`, `openai`, `anthropic`) saved as unencrypted JSON on disk (`config.py`). | Encrypt at rest via Windows DPAPI at runtime + passphrase-encrypted AEAD vault for DR exports. |
| **Outbound Egress Secret Redaction** | `detect_secret()` runs only on `PersonalMemoryStore` writes and training dataset validation, **NOT** on outbound LLM prompts or web search queries! | Mandatory **Egress DLP / Secret Firewall** scanning prompts, tool outputs, and web queries before leaving localhost via OpenRouter/HF/Gemini/OpenAI/Anthropic/Search. |
| **Single-Host Auth & CORS/CSRF Hygiene** | API has no session/CSRF token if exposed or accessed by malicious local browser tabs (`Origin` header unchecked if CORS enabled later). | Maintain loopback `127.0.0.1` binding strictly; validate `Host` / `Origin` headers on state-changing `POST`/`PUT`/`DELETE` routes; optional bearer/session token for headless multi-client access. |
| **Worktree & OpenCode Isolation** | Adding `opencode` and `git` means an agent could theoretically execute shell commands or read user `~/.ssh` if unjailed. | Restrict `opencode run --dir <worktree>`, pass stripped environment variables (no unrelated API keys), forbid `--auto` on destructive tools, run pre-merge secret & diff scanner. |
| **Dependency Manifest Defect** | `requirements.txt` contains a UTF-8 BOM (`\ufeff`) on line 1 and omits direct top-level imports (`pydantic`, `scipy`, `requests`, etc.) present only in `requirements.lock`. Also zero linters/typecheckers (`ruff`, `mypy`) or vulnerability scanners (`pip-audit`) configured. | Fix `requirements.txt` / split `pyproject.toml` dependencies cleanly, strip BOM, add `ruff`, `mypy`, `pip-audit` to CI/preflight. |
| **Tracked Secret / Junk Files** | `.playwright-mcp/` (`27` files, ~3.1 MB) and root prompt PDFs clutter repo. (Zero secrets in repo verified.) | Keep `.gitignore` covering `.playwright-mcp/`, `artifacts/`, `.worktrees/`, `.env*`, `data/secrets.json`. |

---

## 4. Target Unified 3-Tier Permission Matrix (`READ` / `WRITE` / `EXECUTE`)

Every tool call across Personal Assistant, Dev Workforce, OpenCode, Knowledge, and Training maps explicitly to one enforcement level persisted in `approval_requests`:

| Capability Domain | **READ (Auto-Allowed, Audited)** | **WRITE (Policy / Scoped Confirm)** | **EXECUTE / SENSITIVE (Mandatory Explicit Approval)** |
| :--- | :--- | :--- | :--- |
| **Personal Memory & Notes** | Search notes, read personal facts/memories, history search | Create/edit note inside workspace, save explicit fact (`remember:`) | Bulk delete/purge memories, export unmasked personal data, overwrite protected docs |
| **Documents & Workspace** | Read/parse PDF, DOCX, PPTX, XLSX inside allowed project/docs roots | Generate new PDF/DOCX/PPTX/XLSX into `output/documents/` | Overwrite existing user files outside `output/` or delete source files |
| **Productivity (Email / Cal)** | List reminders, read/summarize emails & calendar events | Create local reminder, save email **draft** (unsent) | **Send email**, reply externally, delete/modify external calendar invite |
| **Network & Models** | Query local Ollama, inspect health/status | Toggle cache TTL, download allowlisted dataset in Online Mode | Switch `Local Mode` → `Online Mode`, call paid cloud LLM exceeding soft budget, configure API keys |
| **Project Intelligence (Graphify / Files)** | `graphify query`, `graphify affected`, `graphify god-nodes`, AST inspect, `git status` / `git diff` | Incremental `graphify update` on registered project repo | Cross-project file access outside active `project_id` |
| **Dev Workforce (OpenCode / Git)** | Create read-only architectural plan, view worktree diffs | Edits **inside isolated Git worktree** + running unit tests/linters (`pytest`, `ruff`) | **Merge worktree into main branch**, `git reset`, DB schema migration (`alembic upgrade`), `opencode` host-wide execution, system/package install |
| **Learning & Fine-Tuning** | View metrics, list sources, validate/clean dataset locally | Prepare dataset build, queue `pending_approval` job | Start fine-tuning worker, promote candidate model to active, roll back registry pointer |
