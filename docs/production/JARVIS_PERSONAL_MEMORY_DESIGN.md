# JARVIS PERSONAL MEMORY & CONTEXT ARCHITECTURE
**Version**: 2.0 (Production Standard)  
**Status**: Authoritative Architectural Standard  
**Component**: `jarvis.memory.five_way.FiveWayMemoryService` & `jarvis.memory.vectors.IsolatedVectorStore`  

---

## 1. Five Distinct Memory Scopes

JARVIS organizes persistent context into 5 segregated memory stores to prevent context pollution and memory poisoning:

```
+---------------------------------------------------------------------------------------------------+
| 1. WORKING MEMORY (Session & Turn Context)                                                        |
|    - Active conversation window (history_turns)                                                   |
|    - Current plan, active task ID, temporary tool outputs, and recent error states                |
|    - Table: conversation_messages, conversation_sessions                                          |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 2. SEMANTIC MEMORY (Persistent Facts & Preferences)                                               |
|    - Stable user preferences, project context, coding habits, non-sensitive profiles              |
|    - Explicit provenance, confidence score, user confirmation flag, superseding ID                |
|    - Table: personal_memories                                                                     |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 3. EPISODIC MEMORY (Past Interactions & Outcomes)                                                 |
|    - Completed tasks, decisions, git worktree runs, execution journals, and outcome contracts     |
|    - Table: assistant_tasks, agent_runs, command_history                                          |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 4. PROCEDURAL MEMORY (Lessons & Workflows)                                                        |
|    - Feedback lessons learned from user corrections and successful error recoveries               |
|    - Step-by-step verified execution routines and custom skills                                    |
|    - Table: experience_lessons, routines, skills                                                  |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 5. KNOWLEDGE REFERENCES (Indexed Notes & Documents)                                               |
|    - User Markdown notes, parsed PDFs, office documents, and Graphify codebase indexes            |
|    - Enclosed in <untrusted_content> during prompt synthesis with verifiable citations            |
|    - Table: notes, knowledge_chunks, vector_entries                                               |
+---------------------------------------------------------------------------------------------------+
```

---

## 2. Memory Record Attributes & Provenance

Every persisted semantic memory entry tracks 12 mandatory attributes:
1. `id`: Unique 32-character hexadecimal identifier.
2. `category`: One of `explicit`, `user_preferences`, `project_memory`, `command_memory`, `conversation_memory`.
3. `topic`: High-level subject index (e.g., `coding_style`, `default_project`).
4. `content`: Scrubbed memory text (never containing raw API keys, passwords, or tokens).
5. `source`: Provenance of memory (e.g., `user_explicit`, `task_outcome`, `system_inferred`).
6. `confidence`: Float between `0.0` and `1.0`.
7. `owner_scope`: Access control partition (default `default`).
8. `project_id`: Optional link to a specific project.
9. `privacy_level`: `public`, `internal`, `private`, `confidential`, or `restricted`.
10. `confirmed`: Boolean flag indicating explicit user confirmation vs. inferred draft.
11. `supersedes_id` / `superseded_by`: Traceable version lineage preventing duplicate or contradictory memories.
12. `created_at` / `updated_at` / `expires_at`: Precise timestamps with optional TTL.

---

## 3. Strict Memory Guardrails

1. **Secret Firewall**: Before storing any candidate memory, `detect_secret(text)` scans for credentials, private keys, and tokens. Any match triggers an immediate security rejection.
2. **Duplicate Prevention**: Vector cosine similarity check detects near-duplicate memories (`DuplicateMemoryError`) and prompts replacement via `supersedes_id`.
3. **No Transient Chatter Ingestion**: Casual small-talk ("Hello", "Thanks", "How are you?") is explicitly blocked from semantic memory persistence.
4. **User Correction & Deletion**:
   - Soft-delete with `deleted_at` timestamp.
   - Transparent vector deletion in `vector_entries`.
   - Complete export and auditability via `/api/v2/memory/personal`.

