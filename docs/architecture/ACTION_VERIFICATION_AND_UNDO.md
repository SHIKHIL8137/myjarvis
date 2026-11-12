# Verified Action Execution, Permissions & Undo Journal (P5)

## Overview

Phase P5 implements verified action execution, zero-bypass filesystem security, and byte-for-byte reversible snapshots with strict external modification conflict detection.

## Verified Action Execution (`ToolOutcome`)

All desktop and filesystem operations produce structured `ToolOutcome` contracts:
- `tool_name`: Identifier of the executed tool (e.g., `keyboard_type`, `write_file`).
- `status`: Execution state (`started`, `done`, `simulated`, `failed`, `denied`).
- `verified`: Boolean indicating whether execution was independently verified on the host.
- `checks`: Array of discrete verification checks (e.g. `[{"name": "os_input_platform_verified", "passed": true}]`).
- `evidence`: Execution evidence dictionary (e.g. process ID, window handle, bytes written).

Stubs and unverified simulations never report `verified=True`.

## Zero-Bypass Document Parsing Security

The previous security bypass in `/v2/documents/parse` where file paths bypassed `FilesystemAccessManager` was eliminated:
1. Every requested document path is validated via `fs_access.authorize(path, "read_file")`.
2. Document content is decoded and sanitized using `parse_document_untrusted()`, ensuring malicious embedded scripts or macros cannot execute.

## UndoService & Reversible Snapshots

### 1. Snapshot Capture (`record_snapshot`)

Before modifying or deleting a file, `UndoService` captures:
- `before_bytes`: Raw original file bytes, base64-encoded in `snapshot_data["before_b64"]`.
- `before_sha256`: SHA-256 digest of original bytes.
- `after_sha256`: Expected SHA-256 digest after modification.
- `ttl_seconds`: Time-to-live (default 86,400 seconds / 24 hours).

### 2. External Modification Conflict Detection

Before executing an Undo rollback (`apply_undo`):
1. The engine checks current file content on disk.
2. If disk SHA-256 does NOT match `after_sha256`, an external process (or user edit) has modified the file since JARVIS touched it.
3. Undo is aborted immediately with `UndoConflictError` (HTTP 409), preventing data loss.

### 3. Byte-Exact Restoration

- On successful verification, original bytes are decoded from base64 and written using `Path.write_bytes()`, guaranteeing exact binary identity without newline mangling on Windows.
- The entry is marked as `status="undone"` to prevent double restoration.

