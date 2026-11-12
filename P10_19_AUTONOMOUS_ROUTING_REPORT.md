# P10.19 Autonomous Routing Report

**Date:** 2026-11-12  
**Primary Modules:** `jarvis/assistant.py`, `jarvis/decision/schemas.py`, `jarvis/decision/rules.py`, `jarvis/decision/classifier.py`, `jarvis/decision/router.py`, `jarvis/agents/orchestrator.py`, `jarvis/graph_live.py`, `jarvis/intent.py`

---

## 1. Routing Architecture Overview

Jarvis processes natural-language text and voice commands through a unified deterministic + planned execution pipeline:

1. **UI Navigation Directives (`Assistant._match_ui_command`)**:
   - Checked before deterministic OS/app commands so phrases like `"open approvals drawer"` emit a `ui_action` SSE event to toggle contextual `.hud-drawer` panels without opening external apps or invoking the LLM.
2. **Fast Deterministic Intents (`commands.route` / `jarvis/intent.py`)**:
   - Immediate safe local commands (volume, media, system shortcuts, explicit commands).
3. **Structured Decision Classification (`DecisionEngine` in `jarvis/decision/`)**:
   - Classifies requests into explicit intents including `dev_workforce`, `filesystem_ops`, `web_research`, `document_export`, `reminder_manage`, `memory_manage`, `system_status`, `docker_ops`, `recovery_ops`, `sensitive_action_request`, `security_audit`, `clarification_needed`, and `general_chat`.
   - `classifier.py` prioritizes `doc_m` (`DOC_EXPORT_RE`) and `is_rem` (`REMINDER_RE`) ahead of `is_docker`, `is_recovery`, `is_sys`, and `is_dev` so requests like `"export recovery report pdf"` or `"remind me to check docker"` hit the right department workflow.
   - `rules.py` handles multi-word modifiers accurately (including `file_deletion` with `files?` so `"delete all files..."` is caught cleanly as sensitive/destructive).
4. **Live Workflow Execution (`Assistant._execute_planned_workflow`)**:
   - Decisions do not stop at response metadata labels — they invoke real backend services.

---

## 2. Intent-to-Execution Matrix

| Intent | Executed Backend Path | Event Telemetry Emitted | Approval Gate / Safety |
| :--- | :--- | :--- | :--- |
| `dev_workforce` | `AgentOrchestrator.run_autonomous_dev_pipeline` | `tool.invoked`/`tool.executed` (`"workforce_pipeline"`) + `workflow.*` + `skill.executed` (only if registry skills match) | Project-scoped `SafeFilesystem`, cancellation token, opens `"approvals"` or `"workforce"` drawer, honest CLI/Docker blocker reporting |
| `filesystem_ops` | `Assistant._extract_fs_query` + `SafeFilesystem.inspect_project` / `.search` | `tool.invoked` / `tool.executed` (`"filesystem_search"`) | Allowlisted roots only (`FilesystemPermissionError` / HTTP `403` outside) |
| `web_research` | Offline KB fallback `self.knowledge.search(text, k=3)` when `online=False`; falls through to `Brain` + `WebSearchService` when `online=True` | `tool.invoked` / `tool.executed` (`"knowledge_search"`) in Local Mode | Honors `internet.enabled=false` & network watcher |
| `document_export` | `self.orchestrator.start_assistant_workflow(goal=text, task_kind="document_export", ...)` | `tool.invoked` / `tool.executed` (`"doc_export"`) | Generated via assistant workflow (`pdf`/`docx`/`pptx`/`xlsx`) |
| `reminder_manage` | `self.orchestrator.reminders.list_reminders()` or `start_assistant_workflow(..., task_kind="reminder", ...)` | `tool.invoked` / `tool.executed` (`"reminders"`) | SQLite-backed persistence |
| `memory_manage` | `self.memory_store.list_all(...)` + `self.orchestrator.memory.list_personal_memories()` | `tool.invoked` / `tool.executed` (`"memory_store"`) + `ui: {"open_drawer": "memory"}` | Excludes pending/unapproved records (`include_pending=False`) |
| `system_status` | `await self.status()` (`ram`, `gpu`, `cpu_pct`, `brain`, `ollama`, `voice`, `tasks`) | `tool.invoked` / `tool.executed` (`"system_monitor"`) + `ui: {"open_drawer": "system"}` | Read-only telemetry |
| `docker_ops` | `git_worktree.is_docker_available()` + `subprocess.run(["docker", "ps", ...])` (`shell=False`) | `tool.invoked` / `tool.executed` (`"docker_inspect"`) | Reports daemon unavailability honestly |
| `recovery_ops` | `self.orchestrator.recovery.list_manifests()` + `list_database_targets()` | `tool.invoked` / `tool.executed` (`"recovery_status"`) | Read-only status; production restores remain strictly gated by owner approval |
| `sensitive_action_request` | Routed through `DecisionRouter` / `ApprovalService` gate (`file_deletion`, push/merge, restore, deploy, destructive shell, credentials, untrusted skills) | Approval request / block | Hard block until explicit owner confirmation |
| `general_chat` | Local Ollama (`qwen3:4b-instruct`) via `Brain` + note/memory retrieval (`ThinkFilter` sanitized) | `assistant.state` -> `done` | Reasoning tags stripped before SSE/TTS |

---

## 3. Event Semantics Verification

Implemented in `jarvis/graph_live.py` and verified by `tests/test_p10_19_autonomous_voice_os.py::test_req5_event_semantics_separate_tools_skills_and_workflows`:
- **`tool.invoked` / `tool.executed`** (`department="tools"`): recorded whenever internal helper tools (`filesystem`, `knowledge`, `sysinfo`, etc.) execute.
- **`skill.executed`** (`department="skills"`): emitted **only** when an installed `SkillRegistry` skill actually runs in a workflow.
- **`workflow.started` / `workflow.completed` / `workflow.failed`**: emitted by `workflow_event()` around multi-step dev/assistant pipelines.

---

## 4. Verification Summary

- **Automated routing & pipeline tests**:
  - `tests/test_p10_19_autonomous_voice_os.py::test_req1_autonomous_routing_executes_real_workflows` — PASSED
  - `tests/test_p10_19_autonomous_voice_os.py::test_req2_multi_agent_dev_pipeline_lifecycle_cancel_timeout_and_blocker` — PASSED
  - `tests/test_p10_19_autonomous_voice_os.py::test_req5_event_semantics_separate_tools_skills_and_workflows` — PASSED
  - `tests/test_p10_19_autonomous_voice_os.py::test_req3_single_screen_ui_directives_and_status_honesty` — PASSED
- **Live Ollama routing check (`preflight.py` / `drill.py` on `127.0.0.1:4719`)**:
  - `"How much VRAM do I need to run a local language model?"` routed cleanly through retrieval + `qwen3:4b-instruct`, grounded in graph nodes `[22, 23, 3]`, with `0` `<think>` reasoning leaks.
  - During `test_continuous_full_two_turn_lifecycle_and_auto_restart` hardening, we confirmed `"check system status"` now deterministically executes the P10.19 `system_status` monitoring workflow rather than hitting the general chat LLM (that test's Turn 2 general-chat prompt was aligned to `"explain quantization briefly"`).
