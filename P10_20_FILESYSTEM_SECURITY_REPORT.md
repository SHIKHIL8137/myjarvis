# P10.20 Filesystem Security Report

**Date:** 2026-11-12  
**Workspace:** `d:\projects\myjarvis`

---

## 1. Threat Model & Default-Deny Boundary

`FilesystemAccessManager` (`jarvis/filesystem_access.py`) governs all assistant access to host directories outside the built-in Jarvis `notes/` knowledge store.

- **Default Posture:** No host directory is readable, writable, executable, or deletable until an explicit grant exists in `filesystem_permissions` (`FilesystemPermissionRow`).
- **Granular Operation Separation:**
  - `READ_OPS`: `("list_directory", "read_file", "search_files", "read_metadata")`
  - `WRITE_OPS`: `("create_file", "write_file", "edit_file")`
  - `EXECUTE_OPS`: `("execute_in_directory",)`
  - `DELETE_OPS`: `("delete_file",)`
  - `ALL_OPS = READ_OPS + WRITE_OPS + EXECUTE_OPS + DELETE_OPS`
  - Approval of any operation subset strictly isolates unrequested capabilities (`not_requested_operations` are never implicitly unlocked).

---

## 2. Canonicalization, Reparse/Symlink & Sensitive Path Controls

Every pre-check (`authorize`), grant creation (`grant_direct` / `decide_request`), and actual read/search/write execution performs canonical path normalization (`canonicalize_safe_path`) and re-checks at point-of-use (TOCTOU mitigation):

1. **Path Traversal & Windows Device/UNC Hardening:**
   - Rejects `..` traversal, malformed/empty paths, and Windows reserved device names (`_WINDOWS_RESERVED_NAMES`: `con`, `prn`, `aux`, `nul`, etc.).
2. **Symlink & Junction Escape Blocking:**
   - Uses `_has_reparse_or_symlink` to block symlinks and directory junctions pointing inside or outside granted roots while preserving safe Windows cloud/OneDrive placeholder files where appropriate.
   - Covered in `tests/test_p10_20_manager_and_fs_security.py::test_scenario_e_security_boundaries_attacks_and_audit_log` (blocks outside secret symlink targets with `symlink_blocked` / `symlink_escape_blocked`).
3. **Protected OS, Credential & Browser Profile Exclusions (`_detect_sensitive_path`, `_SENSITIVE_DIR_FRAGMENTS`):**
   - Unconditionally rejects protected OS directories (`C:\Windows\System32`, etc.), credential directories, browser profiles, and `.git` sensitive metadata (`protected_system_dir`, `protected_credential_or_browser_dir`, `git_sensitive_metadata`), logged as `"blocked"` in `AuditEventRow`.
4. **Sensitive Filename Filtering (`_SENSITIVE_FILENAMES`, `sensitive_file_policy`):**
   - Filters credential-bearing filenames (`.env`, SSH private keys, secret stores) when listing, searching, or reading under `sensitive_file_policy` (`block` | `warn` | `metadata_only`), returning `sensitive_file_blocked` when set to `block`.
5. **Executable/Script Write Guard (`_BLOCKED_WRITE_EXTENSIONS`):**
   - Restricts dangerous executable/script extensions on write paths unless permitted by extension policy.

---

## 3. Scope Enforcement & Hardened Policies

- **`allow_once` (Single-Task Bound):**
  - Scoped to `task_id`; automatically marked `consumed` after execution (or task cancellation via `POST /api/v2/tasks/{task_id}/cancel`).
  - Subsequent access attempts fail with HTTP `403` / `denied_ungranted`.
- **`always_read_only` Strict Policy Guard:**
  - Calling `decide_request(..., "always_read_only")` grants strictly `list(READ_OPS)` and raises `FilesystemSecurityError(code="read_only_policy_violation")` (`HTTP 403`) if any requested operation lies outside `READ_OPS` (`WRITE_OPS`, `EXECUTE_OPS`, or `DELETE_OPS`).
- **Owner-Only Approval (`OWNER_ACTORS`):**
  - Approvals from non-owner actors (e.g., `decided_by="llm"` or `"agent"`) are rejected with `HTTP 403` and audited with decision `"rejected_non_owner"`.
- **Audit Isolation Fix (`jarvis/filesystem_access.py` & `jarvis/audit.py`):**
  - Filesystem audit memory ring (`_fs_audit_ring`) is isolated from `jarvis.audit._events` (outbound socket ring buffer), and `audit.since()` uses safe dictionary getters so filesystem auditing never interferes with `/api/drill/connections`.

---

## 4. Verification Summary

- `test_scenario_a_b_c_host_folder_permission_lifecycle`: **PASSED**
- `test_scenario_d_read_write_separation_and_always_read_only_guard`: **PASSED**
- `test_scenario_e_security_boundaries_attacks_and_audit_log`: **PASSED** (traversal, reserved names, OS dirs, `.env`/SSH keys, symlink escape, non-owner approval rejection, `AuditEventRow` persistence, `/api/drill/connections` coexistence)
- Live HTTP test on isolated `127.0.0.1:4720`:
  - Unapproved temp folder file read and `C:\Windows\System32\drivers\etc\hosts` blocked with `HTTP 403`.
  - Approved `allow_once` search executed once, returned `invoice_2026_04.txt`, and transitioned grant to `consumed`.

