# Evidence-Based Deep Research Engine (P2)

## Overview

The P2 Evidence-Based Deep Research Engine produces verified, grounded research reports from both web sources and approved local directories. It enforces prompt injection isolation, verbatim quote verification with character offset spans, and automatic cross-source contradiction detection.

## Core Capabilities

### 1. Verbatim Quote Offset Spans (`quote_spans`)

Every claim labeled as `verified_fact` must be backed by an exact quote found in a retrieved snapshot:
- `_locate_quote_span(content, quote, source_id)` scans the snapshot content using exact, case-normalized, and whitespace-collapsed regex matching.
- It records exact `start_offset` and `end_offset` integer coordinates in the claim's `quote_spans` list:
  ```json
  {
    "source_id": "DOC_01",
    "start_offset": 62,
    "end_offset": 121,
    "matched_text": "Every operation checks explicit grant scopes and paths."
  }
  ```

### 2. Provenance Tracking

Claims are classified with transparent provenance badges:
- `extracted_quote`: Directly quoted from a verified snapshot with matched offset coordinates.
- `synthesized_claim`: Summarized from supported source facts without verbatim quotation.
- `inference`: Derived conclusion or implication flagged explicitly for user review.

### 3. Cross-Source Contradiction Detection

- `_detect_contradiction_records(snapshots)` extracts statements from different canonical sources sharing content terms.
- It evaluates sentiment and polarity using `_POS_PATTERNS` (`enabled by default`, `is supported`) and `_NEG_PATTERNS` (`disabled by default`, `unsupported`, `deprecated`).
- Opposing statements sharing topic terms are surfaced in `research_contradictions` and highlighted in red in the Operations Center.

### 4. Freshness and Scanning Limitations

The engine computes explicit metadata for research integrity:
- `scanned_paths_count`: Total local files examined.
- `source_count` & `independent_source_count`: Total and unique canonical sources.
- `latest_source_timestamp`: Timestamp of most recent evidence.
- `limitations`: List of constraints (e.g., offline mode active, fewer than 2 independent sources, inaccessible restricted folders).

### 5. Local Folder Ingestion & Injection Defense

- Only approved folders authorized through `FilesystemAccessManager` are scanned.
- All untrusted local and web content is sanitized using `parse_document_untrusted()` and enclosed in `<jarvis_untrusted_data>` boundaries to prevent prompt injection attacks.

