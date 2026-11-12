# P10.20 Permission & Audit Trail Report

**Date:** 2026-11-12  
**Workspace:** `d:\projects\myjarvis`

---

## 1. Permission Lifecycle State Machine

Scoped filesystem permissions and approvals operate across two persisted tables in `jarvis/db/models.py`:

- **Requests (`filesystem_permission_requests` / `FilesystemPermissionRequestRow`):**
  - Created via `FilesystemAccessManager.create_permission_request(...)` when a user turn or task targets an ungranted host directory.
  - Captures exact `requested_path`, `canonical_path`, `requested_operations`, `not_requested_operations`, `sensitive_flags`, `access_duration`, and serializes `deferred_action` for seamless post-approval execution.
  - Status lifecycle: `pending` -> `approved` | `denied` | `expired` | `consumed`.
- **Grants (`filesystem_permissions` / `FilesystemPermissionRow`):**
  - Created either from owner request decision (`POST /api/v2/filesystem/requests/{request_id}/decide`) or explicit configuration in *Settings -> Permissions & Privacy* (`POST /api/v2/filesystem/permissions`).
  - Supports edit (`PUT /api/v2/filesystem/permissions/{permission_id}`) and instant revocation (`POST /api/v2/filesystem/permissions/{permission_id}/revoke`).
  - Status lifecycle: `active` -> `consumed` (`allow_once`) | `revoked` | `expired`.

---

## 2. Owner-Only Governance & UI Transparency

- Only recognized owner actors (`OWNER_ACTORS = frozenset({"owner", "user", "local_ui", "settings_ui", "desktop_owner"})`) can approve requests (`decide_request`) or manage direct grants.
- Any attempt with `decided_by="llm"` or other non-owner identity fails immediately with HTTP `403` (`FilesystemSecurityError`), leaving the request untouched and emitting a `"rejected_non_owner"` audit event.
- Inline permission cards (`AssistantHome.tsx`) display:
  - Target canonical folder path
  - Requested operations vs. explicitly excluded operations
  - Sensitive path warnings (`sensitive_flags`)
  - Four bounded choices: `Allow once`, `Allow for this session`, `Allow always (read-only)`, `Deny`.

---

## 3. Database & Network Security Audit Trail

- Every filesystem evaluation and state change records an `AuditEventRow` (`category="filesystem_access"`) in SQLite/PostgreSQL:
  - Decisions recorded: `allowed`, `blocked`, `denied_ungranted`, `rejected_non_owner`, `granted`, `revoked`, `modified`, `consumed`.
  - Queryable from UI and API via `GET /api/v2/filesystem/audit?limit=80`.
- Socket egress auditing (`jarvis/audit.py`) remains strictly separated from `_fs_audit_ring` (`jarvis/filesystem_access.py`):
  - Verified live via `drill.py` (`GET /api/drill/connections?since=...` returned HTTP `200`, `NON-LOCAL HOSTS (server log): []`, `NON-LOCAL HOSTS (OS level): []`, and `WATCHER SELF-TEST: pass - caught and blocked ['api.anthropic.com']`).
- **Audit Log Observations in Tests & Live Server:**
  - `test_scenario_e_security_boundaries_attacks_and_audit_log` confirmed `blocked`, `denied_ungranted`, and `rejected_non_owner` entries persisted in DB alongside HTTP `200` on `/api/drill/connections?since=0`.
  - Live `127.0.0.1:4720` check verified 15 recent audit events persisted during the isolated `allow_once` host-folder turn, decision, deferred execution, and grant consumption.

