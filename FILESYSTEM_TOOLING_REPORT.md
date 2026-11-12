# Filesystem Tooling & Project Intelligence Report (Stage A)

## Summary
Jarvis now provides permission-gated local filesystem tools, structured document parsing, project aliases, and automatic credential redaction across both the Brain tool loop and direct natural-language commands.

## Implemented Capabilities
- **Permission & Path Safety (`jarvis/filesystem_access.py`)**:
  - `FilesystemAccessManager` enforces per-folder and per-operation scopes (`list_directory`, `search_files`, `read_file`, `read_document`, `get_project_structure`, `find_files_by_extension`, `read_metadata`, `write_file`, `delete_file`) with canonical path containment (`canonicalize_safe_path`), reparse/symlink rejection (`_has_reparse_or_symlink`), drive/FS root blocking (`_is_drive_or_fs_root`), and sensitive file detection (`_detect_sensitive_path` — `.env`, SSH/private keys, secret files).
  - Supports multi-encoding text decoding (`_decode_text_bytes`), binary file protection, and structured document extraction via `execute_read_document`.
- **Project Aliases (`jarvis/db/models.py`, `jarvis/db/migrations/versions/0007_p11_project_aliases.py`, `jarvis/api_v2.py`)**:
  - Persisted `ProjectAliasRow` mappings managed through `list_aliases`, `upsert_alias`, `delete_alias`, and `resolve_alias`.
  - `resolve_alias` guards against matching explicit filesystem paths (`C:\...`, `\\...`, `/...`) while tokenizing natural-language queries with punctuation cleanly (e.g. `ERP_V2,`).
  - Exposed via `/api/v2/filesystem/aliases` (`GET`, `POST`, `DELETE`) and `/api/v2/filesystem/policy` (`GET`, `PUT`), plus the **Project Aliases** tab in `apps/frontend/src/components/OperationsDrawer.tsx`.
- **PII & Secret Firewall (`jarvis/security/pii_firewall.py`)**:
  - Redacts credentials in files, directory outputs, and embedded URI credentials before content enters LLM context or tool outputs.
- **Brain Integration (`jarvis/assistant.py`, `jarvis/brain.py`, `jarvis/decision/classifier.py`, `jarvis/persona.py`)**:
  - Integrated multi-round read-only FS tool definitions (`fs_get_project_structure`, `fs_list_directory`, `fs_search_files`, `fs_find_files_by_extension`, `fs_read_file`, `fs_read_document`, `fs_read_metadata`) so Jarvis inspects approved local repositories and documents before answering.

## Verification
- Verified via `tests/test_stage_a_filesystem_intelligence.py` and `tests/test_p10_20_manager_and_fs_security.py`.

