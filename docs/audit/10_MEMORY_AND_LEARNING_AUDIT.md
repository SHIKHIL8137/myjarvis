# 10 — 5-Way Memory & Automatic Learning Architecture Audit

> **Core Invariant**: Memory, external knowledge, user corrections, project architecture, and training datasets **must remain strictly separated**. Currently Jarvis already avoids polluting model weights cleanly during RAG/feedback updates, **but** its storage layers partially conflate personal facts, project notes, conversation preferences, and coding context.

---

## 1. Current State vs Target 5-Way Memory Separation

### Where Overlap Exists Today
1. **`PersonalMemoryStore` (`MEMORY_CATEGORIES = ("explicit", "preference", "project", "temporary", "feedback")`)** currently stuffs `"project"` notes and `"feedback"` items inside `data/memory/records.jsonl` alongside personal facts.
2. **`NoteStore` (`notes/learning/`, `notes/personal/`, `notes/programming/`, `notes/technology/`)** duplicates storage for both user personal memories (`REMEMBER` writes simultaneously to `NoteStore` AND `PersonalMemoryStore`) and conceptual reference material (`technology/`, `programming/`).
3. **Multilingual Retrieval Gap**: `jarvis/retrieval.py::tokens()` compiles `re.compile(r"[a-z0-9]+")`. Meanwhile, `jarvis/learning/knowledge_pipeline.py::detect_script_language()` explicitly detects Malayalam (`0D00..0D7F`) and Devanagari (`0900..097F`), storing `language = "ml"` in SQLite `chunks`—yet keyword queries in Malayalam return **zero tokens** and zero hits across `NoteStore`, `PersonalMemoryStore`, and `KnowledgePipeline`!

---

## 2. Target 5-Way Strict Memory Taxonomy

| Memory Pillar | Data Owned | Persistence & Vector Namespace | Scope / Isolation Policy | Retrieval Rule |
| :--- | :--- | :--- | :--- | :--- |
| **1. Personal Memory** | User profile facts, communication style, routines, preferences, reminders, temporary focus | PostgreSQL `personal_memories` + `notes` + Vector `personal_memory` + `personal_documents` | Owned exclusively by user; optional TTL on `temporary` scope; confirmed flag before high-trust recall | Queried by **Personal Assistant** turns; never leaked into Dev external prompts |
| **2. Project Knowledge & Architecture** | Per-repo tech stacks, ADRs, Graphify structural graphs, API contracts, DB schemas, symbol indexes | PostgreSQL `projects` + `project_graphs` + Vector `project_knowledge` | **Hard partition by `project_id`** (`WHERE project_id = :active`) | Queried strictly when working inside that project; prevents Repo A rules contaminating Repo B |
| **3. Experience & Lesson Memory** | Approved user corrections (`FeedbackStore`), coding post-mortems, solved compiler/test bugs, tool failure lessons | PostgreSQL `experience_lessons` + Vector `experience_memory` | Divided by `scope ('assistant' | 'dev')` & optional `project_id`; requires `approved=true` or verified-fix proof | Injected as high-signal few-shot guardrails (`"Previously we hit X and solved via Y"`) |
| **4. External Knowledge Base** | Public docs, framework changelogs, HuggingFace/API manuals synced from allowlisted URLs | PostgreSQL `knowledge_sources`, `versions`, `chunks` + Vector `external_knowledge` | Tagged with `trust_classification`, `license`, immutable `version_num`, and instant rollback (`active=true/false`) | Always cited distinctly as external documentation (`[KB: source vN]`), never mistaken for user fact |
| **5. Fine-Tuning Datasets, Evals & Model Registry** | Validated/cleaned Alpaca/Messages JSONL snapshots, train/val/test splits, LoRA/GGUF weights, 12-cat eval reports, version pointers | Immutable disk artifacts (`artifacts/training/*`) + PostgreSQL (`dataset_builds`, `training_jobs`, `eval_reports`, `model_versions`) | Isolated offline-ready worker subprocess; hard compatibility check (`check_adapter_compatibility`); mandatory evaluation + human gate before promote | **Never mutates active runtime weights automatically**; instant rollback to `previous_version_id` or `baseline-v1` |

---

## 3. Enhancements Needed for Continuous Safe Learning

1. **Unicode-Aware Hybrid Retrieval**:
   - Replace `[a-z0-9]+` in `jarvis/retrieval.py` with Unicode word tokenization (`r"[^\W_]+"` or ICU/Postgres simple+char n-grams) paired with multilingual embeddings (`pgvector` HNSW cosine + Reciprocal Rank Fusion).
2. **Experience Extraction Loop (`P5–P8`)**:
   - Currently learning only captures explicit verbal user corrections (`is_correction_statement`).
   - Add an automated post-task extractor in LangGraph (`ExtractLessonNode`): after a Dev Workforce run recovers from a failing test/build, propose an `experience_lessons` candidate (`bug_fix` / `workflow_lesson`) with git diff summary and require lightweight confirmation (or auto-approve solely for project-local test-verified patterns without modifying model weights).
3. **Coding Benchmark Expansion in `ModelEvaluator`**:
   - Extend `DEFAULT_EVAL_SUITE` (currently 12 keyword-based cases in `jarvis/learning/evaluation.py`) with verifiable coding/tool-use & multi-step planning cases (SQL schema accuracy, JSON tool-call fidelity, safe shell refusal, Malayalam/English code-mixed QA).
4. **Remote Training Offload Readiness**:
   - Keep local GTX 1650 `4 GB` honest blocker checks (`training_manager.py` currently catches missing `torch/transformers/peft/bitsandbytes` cleanly), while defining a pluggable `TrainingBackend` port so validated builds can dispatch optionally to a remote GPU worker / HuggingFace AutoTrain when Online Mode + explicit approval are enabled.
