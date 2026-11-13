# JARVIS TARGET ARCHITECTURE SPECIFICATION
**Version**: 2.1 (Production Standard)  
**Status**: Authoritative Target Architecture  
**Scope**: Full Personal AI Assistant Architecture Across Backend, Frontend, Models, Runtimes, Connectors & Security  

---

## 1. System Overview & Core Philosophy

JARVIS is a local-first, dependable, secure, and extensible personal AI assistant. Rather than an ad-hoc collection of chat scripts and experimental subagents, JARVIS operates as a coherent, privacy-respecting AI operating system.

### Core Architectural Pillars
1. **Local-First & Resource Aware**: Optimized for bounded consumer hardware (e.g. Ryzen 5 5600H, GTX 1650 4GB VRAM, 16GB RAM) with local Ollama brains and graceful, explicitly-gated cloud fallback.
2. **Truthful & Evidence-Based**: Never fabricates agent counts, simulated execution progress, or fake successes. Every action state is verified against empirical evidence.
3. **Strict Domain Boundary Separation**: Decouples Provider Organizations, Models, Agent Applications, Runtime Instances, Agent Profiles, and Task Assignments.
4. **Least-Privilege Security**: Enforces granular filesystem permissions (read != write != execute != delete), path containment, prompt-injection defense, and owner approval gating.
5. **Durable & Resilient Execution**: Background tasks persist to SQLite/PostgreSQL with checkpoints, enabling steering, pausing, cancellation, and restart recovery.

---

## 2. The 16 Architectural Subsystems

```
+-----------------------------------------------------------------------------------+
|                        15. FRONTEND HUD & OPERATIONS CENTER                       |
|   (React 18 + Vite, Assistant Home, Operations Drawer, Timeline, System Health)   |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|             16. VOICE, DESKTOP & MULTIMODAL INTERFACES (AEC, STT, TTS)            |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                     2. CONVERSATION & INTERACTION LAYER                           |
|       (Direct Conversational Chat, Multi-Turn State, Language Intelligence)       |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                     5. INTENT UNDERSTANDING & PLANNING                            |
|     (Structured Request, Follow-Up Disambiguation, Complexity Gating, Routing)    |
+-----------------------------------------------------------------------------------+
        |                                                           |
        v                                                           v
+-----------------------------------+   +-------------------------------------------+
| 3. USER CONTEXT & MEMORY          |   | 4. KNOWLEDGE & WORKSPACE RETRIEVAL        |
| - Working Memory (session)        |   | - Filesystem Scanner & Parser             |
| - Semantic Memory (preferences)   |   | - PDF / Document Extraction               |
| - Episodic Memory (past tasks)    |   | - Code Graph Indexing (Graphify)          |
| - Procedural Memory (workflows)   |   | - Source Citation & Untrusted Boundary    |
| - Knowledge References (docs)     |   +-------------------------------------------+
+-----------------------------------+
        |
        v
+-----------------------------------------------------------------------------------+
|                          6. TASK & WORKFLOW ENGINE                                |
|  (Durable Tasks, Checkpoints, Background Runner, Steering, Crash Recovery)        |
+-----------------------------------------------------------------------------------+
        |                                                           |
        v                                                           v
+-----------------------------------+   +-------------------------------------------+
| 7. AGENT & RUNTIME REGISTRY       |   | 8. MODEL GATEWAY & PROVIDER REGISTRY      |
| - Agent Applications (OpenCode,   |   | - Provider Orgs (OpenRouter, OpenAI, ...) |
|   Claude Code, Antigravity)       |   | - Model Catalog & Context Limits          |
| - Runtime Instances (executables) |   | - Quota, Fallbacks & Circuit Breakers     |
| - Agent Instances (sessions)      |   | - PII Firewall & Secret Redaction         |
| - Agent Profiles (instructions)   |   +-------------------------------------------+
+-----------------------------------+
        |                                                           |
        +-----------------------------+-----------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
|                    9. CAPABILITY DISCOVERY & TASK ALLOCATION                      |
|       (Evidence-Based Capabilities, Hard Eligibility Filters, Ranking Factors)    |
+-----------------------------------------------------------------------------------+
        |                                                           |
        v                                                           v
+-----------------------------------+   +-------------------------------------------+
| 10. TOOL & CONNECTOR FRAMEWORK    |   | 11. ROUTINE SCHEDULER & PROACTIVE ASST    |
| - Email (draft vs send approval)  |   | - APScheduler (Cron, Interval, Once)      |
| - Calendar (.ics, availability)   |   | - Quiet Hours Enforcement                 |
| - Filesystem (9 granular ops)     |   | - Daily Morning Briefing                  |
| - Git Worktrees (isolated merges) |   | - End-of-Day Review                       |
+-----------------------------------+   +-------------------------------------------+
        |                                                           |
        +-----------------------------+-----------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
|                 12. ACTION EXECUTION, VERIFICATION & UNDO                         |
|   (Outcome Contracts: Requested -> Authorized -> Executed -> Verified -> Reverted)|
|   (Pre-Action Snapshot, SHA-256 Hashes, Conflict Detection, Safe Rollback)        |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
|                 13. SECURITY & PERMISSION ENFORCEMENT                             |
|  (Least Privilege, TOCTOU Checks, Path Normalization, Prompt Injection Defense)   |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
|                    14. EVENT BUS, OBSERVABILITY & AUDIT                           |
|       (SSE Event Stream, Correlation IDs, Structured Logs, Audit Ring)            |
+-----------------------------------------------------------------------------------+
```

---

## 3. Strict Domain Concept Separation (Section 5 Standard)

| Domain Concept | Definition | Concrete Examples in JARVIS | What It Is NOT |
|---|---|---|---|
| **Provider Organization** | An organization or cloud service supplying model inference access | `openrouter`, `openai`, `anthropic`, `google`, `ollama` | NOT an agent application; NOT a model |
| **Model** | A specific parameter weight or API catalog identifier for inference | `qwen3:4b-instruct`, `gpt-4o-mini`, `claude-3-5-sonnet` | NOT a provider; NOT a runtime |
| **Agent Application** | An external software execution product or agent CLI | `opencode`, `claude_code`, `antigravity` | NOT a provider; NOT a model |
| **Runtime Instance** | A concrete executable on disk or managed daemon with verified version and auth | `D:\bin\opencode.cmd`, `C:\Users\...\claude.exe` | NOT an agent profile; NOT a model |
| **Agent Instance** | An active, isolated session spawned from a runtime instance | `Session(opencode_session_a1b2, worktree="...")` | NOT a static config file |
| **Agent Profile** | Reusable persona, instruction set, and permission scope definition | `fullstack_dev`, `architect`, `qa_engineer`, `researcher` | NOT an actual agent count; NOT an executable |
| **Assignment** | A binding between a task sub-step and a runtime, instance, model, or tool | `TaskAssignment(task_id="t1", runtime="opencode", model=...)` | NOT a permanent employee |

---

## 4. End-to-End Request Lifecycle

```
[User Utterance / Request]
           |
           v
1. Interaction Layer: Capture original text & language; preserve transcript
           |
           v
2. Decision Layer: Build StructuredRequest
   - Is it general chat / explanation?
     YES -> Route to direct_chat (stream answer directly; NO task row created)
     NO  -> Route to task planning
           |
           v
3. Memory & Context Retrieval:
   - Retrieve relevant semantic memories & project context
   - Strictly filter private/unrelated memories
           |
           v
4. Planning & Capability Discovery:
   - Decompose into steps with dependencies
   - Query RuntimeRegistry for eligible runtimes
   - Query ModelGateway for verified models
           |
           v
5. Permission & Policy Evaluation:
   - Read operations: auto-allowed if within authorized workspace
   - Modifying operations: follow workspace policy
   - Sensitive operations (send email, delete, merge): create ApprovalRequest
           |
           v
6. Durable Execution:
   - BackgroundTaskRunner executes step with checkpoints
   - Worktree isolation if editing repository files
           |
           v
7. Verification & Undo Snapshot:
   - Verify action outcome (file exists, test returns 0, event in calendar)
   - Store SHA-256 pre-action snapshot in UndoEntryRow
           |
           v
8. Learning & Response:
   - Persist confirmed facts into 5-way memory
   - Return clear, truthful action summary to user
```

---

## 5. Security Architecture & Threat Model Linkage
- **Untrusted Input Barrier**: All file contents, websites, external documents, and tool outputs are tagged as `UNTRUSTED_CONTENT` and prevented from escalating permissions.
- **Path Containment**: `FilesystemAccessManager` enforces canonical Windows path normalization, rejects drive roots (`C:\`), system folders (`C:\Windows`), UNC shares, and browser profiles.
- **Secret Redaction**: API keys and secrets live strictly in `data/secrets.json` (DPAPI/Fernet protected) or environment variables; never logged or exposed in frontend responses.

