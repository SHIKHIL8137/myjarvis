# JARVIS P10.22 — Tool Outcome Verification Report

## 1. Verification Contract ([outcomes.py](file:///d:/projects/myjarvis/jarvis/security/outcomes.py))
`ToolStatus` enforces six explicit execution states:
- `done` (`verified=True`, `ok=True`): allowed strictly when at least one post-condition check ran and passed.
- `unverified` (`verified=False`, `ok=False`): automatic fallback whenever a tool ran without verifiable post-conditions or cannot inspect target state (for instance, missing `docker` binary or missing prior hash on modification).
- `started` (`verified=False`, `ok=False`): asynchronous launch where completion cannot be confirmed (for instance `open_app` / `open_file`).
- `awaiting_approval` (`verified=False`, `ok=False`): blocked awaiting explicit owner confirmation.
- `failed` / `cancelled` (`verified=False`, `ok=False`).

Constructing `ToolOutcome(status="done", checks=[])` deterministically downgrades `status` to `"unverified"` with `verified=False` inside `__post_init__`.

## 2. Deterministic Post-Condition Verifiers
- `verify_file_created`: validates optional root containment, file existence, minimum byte count, readability, and SHA-256 hash.
- `verify_file_modified`: compares against `previous_sha256` or `expected_substring`; downgrades to `unverified` if neither baseline diff nor expected substring was provided.
- `verify_file_deleted`: confirms path absence on disk.
- `verify_document_generated`: verifies file creation, matching extension (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.md`), and non-empty extraction via `parse_document(...)`.
- `verify_git_state`: runs read-only `git rev-parse HEAD`, `--abbrev-ref HEAD`, and `git status --porcelain` (returns `unverified` if `git` binary is unavailable).
- `verify_docker_state`: inspects container/image state via Docker CLI (returns `unverified` cleanly when Docker daemon or CLI is unavailable rather than claiming completion).
- `verify_process_state`: distinguishes `started_only=True` (`status="started"`) from completed exit codes.

## 3. Wired Subsystems
- **ActionExecutor** ([actions.py](file:///d:/projects/myjarvis/jarvis/actions.py)): every filesystem, command, and app-launch action returns structured `outcome` and `verified` fields (`open_app` and `open_file` report `status="started"` instead of falsely returning `status="completed"`/`ok=True`).
- **DocumentService** ([documents.py](file:///d:/projects/myjarvis/jarvis/departments/assistant/documents.py)): added Markdown (`.md`) generation and post-generation `verify_document_generated` verification on every PDF, DOCX, PPTX, XLSX, and Markdown export.
- **Dev Workforce Merge Gate** ([workforce_graph.py](file:///d:/projects/myjarvis/jarvis/agents/workforce_graph.py)): `human_merge_gate` attaches `verify_git_state` after worktree merges.
- **Persona Rule** ([persona.py](file:///d:/projects/myjarvis/jarvis/persona.py)): `HONEST_OUTCOME_RULE` prohibits reporting `unverified` or `started` outcomes as completed.

## 4. Verification Scope
- Unit & integration verified with real filesystem, document parsing, temporary Git repository, `ActionExecutor`, and mocked Docker CLI runners in [test_p10_22_p01_trust_and_outcomes.py](file:///d:/projects/myjarvis/tests/test_p10_22_p01_trust_and_outcomes.py).
- Live host Docker daemon checks remain skipped in test runs when Docker Desktop is stopped on the host.

