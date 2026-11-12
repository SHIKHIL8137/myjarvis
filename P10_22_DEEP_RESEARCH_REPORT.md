# JARVIS P10.22 — Multi-Stage Verified Deep Research Report

## 1. Six-Stage Pipeline ([research.py](file:///d:/projects/myjarvis/jarvis/agents/research.py))
`DeepResearchEngine` executes a bounded, resumable pipeline:
1. **PLAN (`step_plan`)**: generates `<= 6` deduplicated subqueries (`MAX_SUBQUERIES = 6`), incorporating active `steer_notes`, expected source types, and key claims.
2. **INVESTIGATE (`step_investigate_i`)**:
   - In **Online Assisted Mode**, queries `WebSearchService.search(...)` and retrieves full readable text via `WebSearchService.fetch_page(url, max_chars=None)` ([search.py](file:///d:/projects/myjarvis/jarvis/search.py)), falling back to search snippets when individual pages block retrieval.
   - Persists every source snapshot deterministically under `data/tasks/{task_id}/pages/{sha256}.txt` written via raw UTF-8 bytes and verified on disk with `verify_file_created(..., expected_sha256=digest)`.
3. **REVIEW (`step_review`)**: identifies cross-source support vs negation divergences (`_detect_contradictions`) and schedules bounded follow-up queries only if source coverage is thin and query budget remains.
4. **WRITE (`step_write`)**: wraps source payloads with `wrap_untrusted(...)` ([untrusted.py](file:///d:/projects/myjarvis/jarvis/security/untrusted.py)) before invoking any LLM `synthesizer_fn` or grounded deterministic fallback, categorizing claims as `verified_fact`, `reported_claim`, `inference`, or `unverified`.
5. **VERIFY (`verify_claims_against_snapshots`)**: checks every non-inference claim against saved snapshots using NFKC Unicode and smart-punctuation/whitespace normalization (`normalize_for_quote_match`), flagging `missing_citations`, `invalid_citations`, `quote_mismatches`, `duplicate_urls`, and contradictions.
6. **REVISE (`<= 2` cycles, `MAX_REVISION_CYCLES = 2`)**: prunes invalid source references and downgrades unsupported or quote-mismatched claims to `unverified` with explicit `[unverified]` markers—never fabricating citations.

## 2. Mode & Privacy Controls
- **Local Mode**: makes zero outbound HTTP/socket search or fetch calls (`mode="local_kb_only"`), using only local indexed `KnowledgePipeline` hits and clearly disclosing offline limitations.
- **Privacy Isolation**: `DeepResearchEngine`, `assistant_graph.gather_context` ([assistant_graph.py](file:///d:/projects/myjarvis/jarvis/agents/assistant_graph.py)), and `Assistant.stream_turn` ([assistant.py](file:///d:/projects/myjarvis/jarvis/assistant.py)) omit `PersonalMemoryStore`, personal `NoteStore`, conversation history, email, and host filesystem tools during web/deep research runs.
- **Verified Artifacts & Integration**: exports Markdown, PDF, or DOCX via `DocumentService.generate(...)` with `verify_document_generated` verification, runnable synchronously via `Assistant._execute_planned_workflow` / `assistant_graph`, asynchronously via `BackgroundTaskRunner` (`worker_kind="deep_research"`), or queued through `POST /api/v2/research` ([api_v2.py](file:///d:/projects/myjarvis/jarvis/api_v2.py)).

## 3. Verification Scope
- All stages, snapshot persistence, Unicode quote matching, citation rejection/downgrade, contradiction surfacing, untrusted injection neutralization, Local Mode KB-only fallback, canary memory isolation, and crash-resume recovery were verified in [test_p10_22_p03_deep_research.py](file:///d:/projects/myjarvis/tests/test_p10_22_p03_deep_research.py) using deterministic fake search/KB providers and real temp SQLite + filesystem stores.

