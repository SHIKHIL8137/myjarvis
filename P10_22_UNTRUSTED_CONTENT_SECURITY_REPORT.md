# JARVIS P10.22 — Untrusted Content Security Report

## 1. Security Threat Addressed
External web snippets, fetched webpage HTML/text, local knowledge chunks, and uploaded/parsed documents previously flowed into prompts without delimiter spoofing protection, and online research turns combined private personal memories/notes alongside untrusted web payloads.

## 2. Boundary Hardening ([untrusted.py](file:///d:/projects/myjarvis/jarvis/security/untrusted.py))
- **Per-block Cryptographic Nonce**: `wrap_untrusted(...)` generates a hex `data-boundary` nonce (`secrets.token_hex(6)`) matched explicitly on both `<jarvis_untrusted_data ... data-boundary="<nonce>" data-trust="untrusted">` and `</jarvis_untrusted_data data-boundary="<nonce>">`.
- **Delimiter & Role Spoof Neutralization**: `sanitize_untrusted_text(...)` strips ASCII control characters (except `\t`, `\n`, `\r`) and rewrites any embedded `<system>`, `</system>`, `<assistant>`, `<developer>`, `<jarvis_untrusted_data>`, `<retrieved_knowledge>`, `<jarvis_skill>`, `<tool_call>`, or `<instruction>` tags into `[ESCAPED_DELIMITER:...]` markers before wrapping.
- **Idempotency & Truncation Safety**: Calling `wrap_untrusted` on an already wrapped block normalizes without nesting duplicate boundaries; `max_chars` truncates strictly inside the payload while keeping citation metadata attributes (`source_type`, `source_id`, `url`, `title`, `sha256`) and closing tags intact.
- **Explicit Policy Injection**: `UNTRUSTED_DATA_POLICY` is embedded in both `PERSONA` and `ONLINE_PERSONA` ([persona.py](file:///d:/projects/myjarvis/jarvis/persona.py)) and in web tool responses returned by `Assistant._execute_tool` ([assistant.py](file:///d:/projects/myjarvis/jarvis/assistant.py)).

## 3. Integration Points
- **Knowledge Pipeline**: `KnowledgePipeline.format_untrusted_knowledge` ([knowledge_pipeline.py](file:///d:/projects/myjarvis/jarvis/learning/knowledge_pipeline.py)) preserves the backward-compatible `<retrieved_knowledge source_id="..." trust="..." location="..." data-boundary="<nonce>" untrusted_data="true">` envelope while applying `sanitize_untrusted_text(...)` and attribute escaping (`_escape_attr`) so closing-tag/role spoofing is neutralized, while retaining `detect_poisoning(...)` filtering.
- **Document Parsing**: `parse_document_untrusted` ([documents.py](file:///d:/projects/myjarvis/jarvis/departments/assistant/documents.py)) computes SHA-256 hashes and provides `untrusted_block` envelopes for parsed PDF/DOCX/PPTX/XLSX/text documents.
- **Prompt Assembly & Memory Isolation**: `user_message` and `build_messages` ([persona.py](file:///d:/projects/myjarvis/jarvis/persona.py)) wrap `web_results`, `knowledge_hits`, and `untrusted_documents`, and enforce `isolate_untrusted_context=True` during `Assistant.stream_turn` web-research turns, `assistant_graph.gather_context` web/deep-research runs, and `DeepResearchEngine` executions so private personal memories, notes, and history are excluded.

## 4. Verification Status
- Verified via unit tests in [test_p10_22_p01_trust_and_outcomes.py](file:///d:/projects/myjarvis/tests/test_p10_22_p01_trust_and_outcomes.py) and [test_p10_22_p03_deep_research.py](file:///d:/projects/myjarvis/tests/test_p10_22_p03_deep_research.py), alongside existing security tests in [test_internet_security.py](file:///d:/projects/myjarvis/tests/test_internet_security.py) and [test_p10_20_manager_and_fs_security.py](file:///d:/projects/myjarvis/tests/test_p10_20_manager_and_fs_security.py).
- Note: Protection against live LLM jailbreak compliance remains prompt-level boundary enforcement combined with structural isolation (no model fine-tuning or external adversarial red-team service was run).

