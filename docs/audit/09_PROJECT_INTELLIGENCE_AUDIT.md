# 09 — AI Development Workforce, Graphify & OpenCode Integration Audit

> **Verified Host Capabilities**: Both target CLIs are already installed on this workstation:
> - **`opencode` `1.19.5`** (`opencode.ps1`): supports `opencode run --format json --dir <path> -m <provider/model>`, `opencode serve`, `opencode acp`, and `opencode attach`.
> - **`graphify` `0.9.80`** (`graphify.exe`): supports offline AST rebuild (`graphify update <path> --no-cluster`), community clustering (`graphify cluster-only <path> --backend=ollama`), traversal (`query`, `affected`, `path`, `explain`, `god-nodes`), and multi-repo `merge-graphs`.
> - **`git`** + **`docker`** also present on PATH.
> - **Current Gap**: None of `opencode`, `graphify`, or git write/worktree operations exist in `jarvis/filesystem.py` or `jarvis/actions.py`.

---

## 1. Current Project Inspection vs Required Project Intelligence

Currently `SafeFilesystem.inspect_project()` (`jarvis/filesystem.py`) only counts file extensions (`languages` dict) and inspects whether `package.json`, `pyproject.toml`, `README.md`, or `Dockerfile` exist. It has **no symbol call graph**, **no architectural dependency awareness**, **no multi-repo registry**, **no Git worktree isolation**, and **no coding agent harness**.

---

## 2. Multi-Project Registry & Workspace Isolation

1. **Project Registry (`projects` table)**:
   - Explicit onboarding required (never auto-scan arbitrary drives): registers `project_id`, `name`, `root_path`, `default_branch`, `test_cmd`, `lint_cmd`, `build_cmd`, and `graphify_dir`.
   - Enforces path-jail rules per active `project_id` so agents working on Project A cannot read/write Project B without explicit cross-project permission.
2. **Git Worktree Isolation (`worktrees` table)**:
   - Coding tasks **must never mutate the user’s active working tree directly**.
   - Before invoking OpenCode to implement/fix anything: create an isolated worktree under `<repo>/.jarvis_worktrees/<task_id>` (`git worktree add -b jarvis/<slug> <worktree_path> HEAD`).
   - Run OpenCode, tests, linters, and diff generation inside that isolated worktree.
   - Only merge/cherry-pick into target branch after user approves the inspected diff (`EXECUTE` permission tier).

---

## 3. Graphify Integration Architecture (`backend/integrations/graphify_adapter.py`)

Graphify replaces blind grep/token-heavy directory dumps with deterministic structural context before invoking any LLM or OpenCode worker:

| Workflow Moment | Exact CLI / Artifact Operation | Purpose & Cost Control |
| :--- | :--- | :--- |
| **On Project Onboarding / Commit** | `graphify update <project_root>` (pure AST/static, zero LLM cost!) + optional `--no-cluster` or `cluster-only --backend=ollama --max-concurrency=1` | Builds/updates `<project>/graphify-out/graph.json`, `GRAPH_REPORT.md` without calling paid cloud LLMs unless configured. |
| **Before Answering Architecture Questions** | `graphify god-nodes` + `graphify query "<question>" --budget 2000 --graph <path>` | Extracts exact high-centrality modules & bounded neighborhood subgraph within strict `2000`-token cap. |
| **Before Coding / Refactoring** | `graphify affected "<symbol_or_file>" --depth 2 --graph <path>` | Calculates downstream blast radius so Architect/Coder agents identify impacted tests and callers up front. |
| **Trace Dependency Chains** | `graphify path "A" "B"` / `graphify explain "X"` | Grounds cross-layer traces (`api` → `service` → `repo`) cleanly. |
| **Frontend Visualization** | Parse `graphify-out/graph.json` → serve via `/api/v2/projects/{id}/graph` | Can reuse the React `3d-force-graph` component to inspect both Personal Notes **and** Code Architecture graphs in 3D! |

> **Windows Safety Guard**: Ensure UTF-8 encoding (`PYTHONUTF8=1`) and `shell=False` subprocess execution with strict timeout caps (`120s` update, `20s` query) + explicit tracking of `graph_build_sha` so stale graphs trigger incremental `graphify update`.

---

## 4. OpenCode Integration Architecture (`backend/integrations/opencode_adapter.py`)

Treat OpenCode (`1.19.5`) as Jarvis’s **delegated coding execution harness**, not an unmonitored shell:

### Execution Modes Evaluated
1. **Supervised One-Shot JSON Runner (`opencode run`) — Recommended Default (`P4`)**:
   - Command: `opencode run "<task_prompt>" --dir <worktree_path> --format json -m <selected_model> [--agent <profile>]` (with `--auto` **DISABLED by default** or strictly sandboxed to worktree).
   - Streams structured JSON events line-by-line into `agent_steps` DB + SSE stream so Jarvis UI displays real-time file reads, edits, diffs, and reasoning steps.
2. **Managed Headless Session Server (`opencode serve`) — Optional Upgrade (`P6+`)**:
   - Spawns `opencode serve --hostname 127.0.0.1 --port <ephemeral>` for multi-turn coding sessions where Jarvis attaches/resumes `--session <id>` iteratively without cold-booting Node/OpenCode per QA feedback cycle.

### Strict Sandboxing Around OpenCode
- **Working Directory Jail**: `--dir` ALWAYS points to the task's isolated Git worktree, never user `~/` or live production folders.
- **Env Filtering**: Spawn OpenCode subprocess with minimal allowlisted environment variables (never inherit unrelated personal keys or `data/secrets.json` values not meant for coding).
- **No Unattended Remote Push**: `git push`, `rm -rf`, DB drops, and system package installs remain disabled/blocked inside OpenCode tool permissions; Jarvis controls final `git diff`, test execution, and merge confirmation.

---

## 5. End-to-End 12-Step Supervised Coding Workflow

1. **Request Intake & Decision**: User asks via voice/chat: *"Add validation and unit tests for `/api/reminders` in project `myjarvis`"*. Fast Decision Engine routes to `dev_workforce`.
2. **Project Graph Sync**: Verify Git HEAD vs `project_graphs.git_commit_sha`; run incremental `graphify update` if changed.
3. **Impact & Context Retrieval**: Query Graphify (`query`, `affected`, `god-nodes`) + `project_knowledge` + `experience_memory` (past bug fixes/conventions in this repo).
4. **Architecture & Plan**: `architect_agent` produces structured `ImplementationSpec` (target files, schema impact, test plan) and checks approval if scope crosses risky boundaries.
5. **Worktree Creation**: Create isolated branch & worktree `.jarvis_worktrees/<task_id>`.
6. **Delegated Implementation**: `coder_agent` translates `ImplementationSpec` + Graphify blast radius into scoped prompt for OpenCode (`opencode run --dir <worktree> --format json`).
7. **Live Telemetry**: Jarvis parses OpenCode JSON events → updates `agent_steps` + streams live progress to React Workforce panel.
8. **Deterministic Verification (`qa_verifier_agent`)**: Jarvis (NOT just OpenCode's self-report) independently executes repo test suite (`pytest` / `vitest` / `ruff` / `mypy` / `tsc`) inside the worktree and captures exact exit codes & outputs.
9. **Retry Loop (Bounded)**: If tests/linters fail, feed structured failure logs back to `coder_agent` / OpenCode (max 2 automatic repair iterations).
10. **Security & Diff Audit (`devops_reviewer_agent`)**: Compute `git diff`, scan for hardcoded secrets (`detect_secret`), unsafe patterns (`shell=True`, `verify=False`), and unintended file edits.
11. **Human Approval Gate (`EXECUTE`)**: LangGraph pauses at `interrupt()`; UI presents diff summary, passing test output, and Graphify impact to user for `[Merge Branch] / [Apply to Main] / [Reject]`.
12. **Merge, Cleanup & Experience Capture**: On approval, fast-forward/merge branch, remove worktree, re-index Graphify, and save distilled post-mortem/solution pattern into `experience_memory`.
