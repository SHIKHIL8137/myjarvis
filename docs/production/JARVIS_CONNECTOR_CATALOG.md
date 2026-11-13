# JARVIS CONNECTOR CATALOG & INTEGRATION CONTRACT SPECIFICATION
**Version**: 2.0 (Production Standard)  
**Status**: Authoritative Connector Architecture  
**Scope**: External & Local Integration Boundaries across Calendar, Email, Filesystem, Desktop, and Developer Runtimes  

---

## 1. Connector Design Principles

1. **Truthful Connection Status**: JARVIS never claims an external service or protocol is connected unless its credentials and handshake have been verified.
2. **Read vs. Draft vs. Execute Separation**:
   - **Read**: Autonomous if within authorized workspace / grant.
   - **Draft / Prepare**: Allowed in sandbox or local outbox (`data/outbox/`).
   - **Send / Modify / Delete**: Requires explicit owner authorization (e.g. CSRF token or approval request).
3. **Graceful Local-Only Degradation**: Missing connectors or offline state never prevents JARVIS from booting or executing local assistant features.

---

## 2. Integrated Connector Catalog

### A. Calendar Connector (`LocalCalendarService`)
- **Connector ID**: `connector.calendar.local`
- **Protocol**: Local RFC 5545 iCalendar (`.ics`) file storage + indexed metadata.
- **Location**: `data/calendar/`
- **Supported Operations**:
  - `list_events()` [READ]
  - `create_event(title, start_iso, duration_minutes, description)` [WRITE]
  - `detect_conflicts(start_iso, duration_minutes)` [READ]
  - `check_availability(start_iso, duration_minutes)` [READ]
  - `update_event(event_id, ...)` [WRITE]
  - `cancel_event(event_id)` [DELETE - Reversible]
- **Health State**: Always Healthy (local file storage).
- **Audit Events**: `calendar.event_created`, `calendar.event_updated`, `calendar.event_cancelled`.

### B. Email & Outbox Connector (`EmailDraftService`)
- **Connector ID**: `connector.email.local_outbox`
- **Protocol**: Local draft queuing + offline dispatch simulation or SMTP/IMAP bridge.
- **Location**: `data/outbox/`
- **Supported Operations**:
  - `create_draft(to, subject, body)` [WRITE - Automatic]
  - `list_drafts()` [READ]
  - `search_drafts(query)` [READ]
  - `summarize_inbox()` [READ]
  - `dispatch_approved_draft(draft_id)` [EXECUTE - Strict Owner Approval Required]
- **Security Policy**: Drafting is decoupled from dispatching. External email dispatching is blocked in Local Mode unless `online_assisted` is enabled and owner approval is granted.

### C. Filesystem Access Connector (`FilesystemAccessManager`)
- **Connector ID**: `connector.filesystem.local`
- **Protocol**: Canonical OS filesystem with TOCTOU enforcement.
- **Supported Operations (9 Granular Operations)**:
  - `list_directory` [READ]
  - `read_file` [READ]
  - `search_files` [READ]
  - `read_metadata` [READ]
  - `create_file` [WRITE]
  - `write_file` [WRITE]
  - `edit_file` [WRITE]
  - `execute_in_directory` [EXECUTE]
  - `delete_file` [DELETE]
- **Security Boundaries**: Path traversal prevention, UNC share blocking, system directory denial (`C:\Windows`, `C:\Program Files`, `AppData\Local\Google\Chrome`), automatic SHA-256 pre-action undo snapshots.

### D. OpenCode Coding Agent Connector (`OpenCodeAdapter`)
- **Connector ID**: `connector.agent.opencode`
- **Protocol**: Headless CLI execution with JSON event line streaming (`opencode run --format json`).
- **Binary**: `C:\Users\shikh\AppData\Roaming\npm\opencode.cmd`
- **Execution Isolation**: Supervised execution inside isolated Git worktrees (`data/worktrees/`) or optional Docker containers.
- **Status**: Discovered & Verified (`auth_verified`).

### E. Claude Code Connector (`ClaudeCodeAdapter`)
- **Connector ID**: `connector.agent.claude_code`
- **Protocol**: Anthropic Claude Code CLI harness.
- **Binary**: `C:\Users\shikh\.local\bin\claude.exe`
- **Status**: Discovered & Verified (`auth_verified`).

---

## 3. Connector Lifecycle & Fault Tolerance Matrix

| Connector | Disconnected Behavior | Degraded Mode | Recovery Action |
|---|---|---|---|
| **Calendar** | Reads local `.ics` files | Operates entirely locally | Create `.ics` file locally |
| **Email** | Queues drafts as `approved_queued_offline` | Drafts preserved in JSON | User reviews and approves in Approvals tab |
| **OpenCode CLI** | Falls back to internal AST code analysis | Read-only repository audit | Run `npm install -g opencode-ai` |
| **Cloud Model Gateway** | Falls back to Ollama local brain | Fast instruct models | Switch to Local Mode or provide API key |

