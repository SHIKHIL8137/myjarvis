# JARVIS P10.22.1 — Research Claim & Snapshot Integrity Report

## 1. Claim Verification Separation (FIX 3)
Updated `verify_claims_against_snapshots`, `ResearchClaim`, `VerificationReport`, and `DeepResearchEngine` in [research.py](file:///d:/projects/myjarvis/jarvis/agents/research.py):

- **Explicit Per-Claim Fields**: Every `ResearchClaim` tracks `citation_valid`, `quote_verified`, `evidence_supports_claim`, `evidence_contradicts_claim`, `independent_source_count`, `assessment_method`, and `verification_status` (`verified` | `supported_reported` | `disputed` | `inference` | `unverified`).
- **Evidence Excerpt Requirement**: Claims classified as `verified_fact` without a verbatim normalized quotation cannot remain `verified_fact`; if cited content supports the claim topically/numerically, the revision pass downgrades them to `reported_claim` (`supported_reported`), and if support is missing, downgrades them to `unverified`.
- **Deterministic Support Check (`_assess_deterministic_support`)**: Checks content-token overlap, numeric/percentage consistency, and polarity alignment strictly inside cited snapshots—preventing valid-but-irrelevant citations (or unrelated matching substrings) from counting as verified.
- **Bounded Semantic Assessor (`claim_assessor_fn`)**: Can flag unsupported claims or contradictions within bounded untrusted payloads, but can never override failed citation, quote, numeric, or deterministic support checks.
- **Contradictions & Duplicate URLs**:
  - Cross-source polarity conflicts across distinct canonical URLs set `evidence_contradicts_claim = True`, `contradicted = True`, mark affected claims `disputed`, set `VerificationReport.overall_status = "disputed"`, and prevent unconditional `passed = True`.
  - Duplicate URLs sharing the same canonical URL do not inflate `independent_source_count` or create artificial cross-source contradictions.
- **Structured Markdown Output**: `render_markdown` explicitly surfaces `Citation valid`, `Quotation verified`, `Evidence supports claim`, `Evidence contradicts claim`, `Disputed & Contradicted Claims`, `Inferences`, `Claim Unverified`, `Duplicate URL Notice`, and `Snapshot Integrity` sections.

## 2. Snapshot Integrity Verification on Reload & Recovery (FIX 5)
Implemented `verify_checkpoint_snapshots(task_id, checkpoint, data_dir=...)` in [research.py](file:///d:/projects/myjarvis/jarvis/agents/research.py) and invoked it in both:
- `DeepResearchEngine.run()` (checkpoint reload prior to investigation and pre-verification disk re-check)
- `AssistantTaskService.recover_interrupted_on_boot()` ([tasks.py](file:///d:/projects/myjarvis/jarvis/agents/tasks.py)) during startup recovery

Guarantees enforced:
1. Paths outside `data/tasks/{task_id}/pages` are rejected before reading.
2. Missing digests or missing snapshot files on disk are rejected without falling back to unverified `snippet` text.
3. Raw-byte SHA-256 is recomputed and compared against stored digests; modified or truncated snapshots are rejected without overwriting or silently repairing the tampered file on disk.
4. Valid snapshots remain preserved so execution continues with explicit `integrity_warnings` only for surviving verified sources; claims relying strictly on rejected/corrupted snapshots become `unverified`.

