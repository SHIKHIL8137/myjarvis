# JARVIS P10.22.1 — Tool Outcome Contract & File Modification Report

## 1. Task Runner Outcome Enforcement (FIX 2)
Previously, `BackgroundTaskRunner.run_once()` persisted `outcome` in `meta` but unconditionally set `task["status"] = "completed"` on any non-exceptional handler return.

Updated enforcement in [outcomes.py](file:///d:/projects/myjarvis/jarvis/security/outcomes.py), [task_runner.py](file:///d:/projects/myjarvis/jarvis/agents/task_runner.py), and [tasks.py](file:///d:/projects/myjarvis/jarvis/agents/tasks.py):
- `parse_task_outcome(...)` and `coerce_tool_outcome(...)` re-instantiate every dict/`ToolOutcome` through `ToolOutcome`, stripping unverified raw `verified=True` claims whenever passing `checks` are absent.
- Explicit mapping from `ToolOutcome.status` to `AssistantTaskRow.status`:
  - `done` (`verified=True`) -> `completed`
  - `unverified` -> `unverified` (never `completed`)
  - `started` -> `started` (never `completed`)
  - `failed` -> `failed` with explicit `error_info` (never `completed`)
  - `awaiting_approval` -> `awaiting_approval`
  - `cancelled` -> `cancelled`
- Handlers for side-effectful intents (`SIDE_EFFECTFUL_INTENTS`), side-effectful worker kinds (`SIDE_EFFECTFUL_WORKER_KINDS`), or `side_effect_expected=True` that omit an `outcome` receipt are automatically downgraded to `unverified`.
- Exhausted retries (`schedule_retry`) and timeouts persist failed `ToolOutcome` receipts in `meta`.

## 2. Task Success vs Artifact Export Separation
In [research.py](file:///d:/projects/myjarvis/jarvis/agents/research.py) (`DeepResearchEngine.run()` and `run_deep_research_background_task`) and [assistant.py](file:///d:/projects/myjarvis/jarvis/assistant.py):
- `DocumentService.generate(...)` errors or failed verification checks are isolated into `artifact_outcome` / `export_outcome`.
- When Deep Research completes cleanly but PDF/DOCX/MD export fails, `task["status"]` stays `completed` with the full Markdown report preserved in `task["result"]`, while `meta.artifact_outcome.status == "failed"`, `meta.partial_failure = True`, and `error_info` clearly reports the export failure.
- [AssistantHome.tsx](file:///d:/projects/myjarvis/apps/frontend/src/features/AssistantHome.tsx) renders `unverified` / `started` status badges with warning styling alongside distinct `outcome: ...` and `export: ...` pills and explanations inside the existing Central Task Manager panel.

## 3. Genuine File Modification Proof (FIX 4)
Updated `verify_file_modified(...)` ([outcomes.py](file:///d:/projects/myjarvis/jarvis/security/outcomes.py)), `SafeFilesystem.create_or_modify(...)` ([filesystem.py](file:///d:/projects/myjarvis/jarvis/filesystem.py)), and `ActionExecutor._run(...)` ([actions.py](file:///d:/projects/myjarvis/jarvis/actions.py)):
- Original file SHA-256 (`previous_sha256`) is mandatory; an `expected_substring` match without a prior hash returns `unverified` rather than `done`.
- Unchanged hashes (`digest == previous_sha256`) are rejected with `evidence["noop"] = True` (`failed` by default, or `unverified` when configured with `noop_status="unverified"`) even if `expected_substring` already existed in the file.
- `expected_after_sha256` detects external/concurrent file replacement between write and verification (`evidence["concurrent_modification"] = True`).
- Missing files fail cleanly (`missing target file`), and optional `root` containment checks enforce boundary safety.

