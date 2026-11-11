# Automatic Learning, Knowledge & Fine-Tuning Audit

Audited against live repository state and Python environment on `2026-11-11` (`Ryzen 5 5600H`, `GTX 1650 4 GB VRAM`, `15.3 GB RAM`, `Python 3.14.6`, `Ollama 0.40.16`).

---

## 1. Existing Functionality Found

| Capability | Existing Implementation | Reusable Foundations |
|---|---|---|
| Knowledge Storage & Retrieval | `jarvis/notes.py` + `jarvis/retrieval.py` | Live mtime file indexing, atomic `.tmp` writes, TF-IDF lexical scoring, soft trash. |
| Approved Online Fetch & Extraction | `jarvis/netsec.py` + `jarvis/search.py` | Domain allowlisting, SSRF IP pinning, `robots.txt`, max-bytes cap, `_TextExtractor` HTML-to-text parser, `huggingface_hub` client installed in `.venv` (`2.4.0`). |
| Transcript Correction History | `jarvis/translator.py` (`TranscriptStore`) | Clean dataclass pattern separating immutable `original_transcript` from `corrected_transcript` (pattern to mirror for dataset immutability and feedback corrections). |
| Telemetry & Hardware Inspection | `jarvis/sysinfo.py` + `preflight.py` | Native Windows CPU/RAM (`GlobalMemoryStatusEx`), GPU/VRAM (`nvidia-smi`), disk free (`shutil.disk_usage`). |
| Model Runtime & Catalogue | `jarvis/brain.py` | Ollama `/api/tags`, `/api/ps`, `/api/chat`, `switch()`, `unload()` freeing VRAM before model swap. |

---

## 2. Missing Functionality (Phases 3–9)

1. **Knowledge Update Pipeline (`jarvis/learning/knowledge_pipeline.py`)**:
   - No connectors for Hugging Face dataset cards/datasets, HF model docs, approved public docs sites, local docs/repositories, or configured APIs.
   - No incremental SQLite/JSONL chunk store, content SHA-256 deduplication, version snapshots, rollback, malformed/corrupt document detection, optional Ollama embeddings, freshness status (`last_attempted`, `last_succeeded`, `new_docs`, `failed_docs`, `index_size`), or scheduled/manual/on-demand triggers.
2. **Feedback Learning Engine (`jarvis/learning/feedback.py`)**:
   - No structured feedback store (`positive`, `negative`, `correction`, `style`, `pattern`, `task_success`, `task_failure`, `workflow`) with category, context, quality score, privacy class, approval state, and topic linking/retrieval.
   - No post-correction confirmation flow (`"Remember that correction for future answers?"`).
3. **Training Data & Dataset Subsystem (`jarvis/learning/training/`)**:
   - Missing `dataset_sources.py`, `dataset_manager.py`, `dataset_validator.py`, `dataset_cleaner.py`, `dataset_builder.py` (no schema checks, immutable raw archive, license allow/block policy, credential/PII scrubbing, poisoning/prompt-injection screening, leakage checks, deterministic train/val/test split, Alpaca JSONL <-> conversational messages format conversion).
4. **Hardware-Aware Training Worker & Scheduler (`jarvis/learning/training/`)**:
   - Missing `training_config.py`, `training_manager.py`, `training_worker.py`, `training_scheduler.py`, `training_audit.py`.
   - Note: `torch`, `transformers`, `peft`, `trl`, `bitsandbytes`, `accelerate`, and `datasets` are **not installed** in `.venv` (`Python 3.14.6` on Windows), so optional dependencies must remain lazy-loaded with clean capability diagnostics and honest CPU/4 GB VRAM refusal/guidance when exceeded.
5. **Evaluation & Quality Control (`jarvis/learning/evaluation.py`)**:
   - Missing fixed 12-category evaluation benchmark suite comparing Baseline vs Candidate on task completion, memory retrieval, tool selection, hallucinations, safety/permissions, latency, and regressions.
6. **Model Registry, Adapter Compatibility & Rollback (`jarvis/learning/training/model_registry.py`, `adapter_manager.py`, `rollback.py`)**:
   - No central registry tracking base models, versions, adapters, hashes, evaluation scores, promotion approvals, Ollama GGUF compatibility verification, or one-step rollback.
7. **Learning Dashboard UI & NLP Learning Commands (`viewer/`, `jarvis/intent.py`)**:
   - Missing all 6 dashboard panels (`Brain Overview`, `Knowledge Updates`, `Training Data`, `Training Jobs`, `Model Evaluation`, `Model Versions`) and NLP command handling.

---

## 3. Incomplete Implementations & Platform Reality Checks

1. **Zero Training Frameworks in `.venv` (`pip list`)**:
   - Only inference runtimes (`ollama` external, `ctranslate2`, `onnxruntime`) exist in `.venv`. Calling PyTorch/PEFT/Transformers directly at startup would crash Jarvis. All training runtime imports **must** be deferred to runtime probe + worker subprocess.
2. **GTX 1650 4 GB VRAM + 16 GB RAM Constraint**:
   - `qwen3:4b-instruct` (~2.2 GB VRAM + ~1.5 GB RAM) leaves < 1.8 GB free VRAM at runtime. Even 4-bit QLoRA backward activations + AdamW states for a 4B LLM exceed 4 GB VRAM on Windows WDDM and risk OOM crashes if run alongside Ollama.
   - Hardware estimator (`training_manager.py`) must inspect live `nvidia-smi` + `GlobalMemoryStatusEx` + running Ollama VRAM (`/api/ps`) cleanly: prioritize **RAG knowledge updates + feedback retrieval** on this machine, limit local LoRA experiments to small models (`<= 0.5B–1.5B`) on CPU/remote worker or when Ollama is unloaded + optional deps installed, and surface clear resource estimates before approving any job.
3. **Ollama LoRA Compatibility**:
   - Ollama requires adapters in compatible GGUF `ADAPTER` format or merged weights via `Modelfile`; raw Hugging Face PEFT adapters cannot be dropped straight into Ollama without conversion. `adapter_manager.py` must model `ollama_modelfile`, `peft_lora`, and `gguf_adapter` compatibility explicitly and refuse unverified activation.

---

## 4. Duplicate Code Risks to Avoid

- **Web Fetching & Robots**: Do NOT duplicate `urllib`/`requests` fetchers inside `knowledge_pipeline.py` or `dataset_sources.py` — reuse `jarvis/netsec.py` (`safe_request`, `sanitize_url`) and `jarvis/audit.py` (`allow_outbound`) so SSRF pinning, Local Mode blocking, and external audit logging apply automatically to every dataset/doc fetch.
- **Chunking & HTML Extraction**: Reuse `extract_readable_text` from `jarvis/search.py` rather than building a second HTML parser.

---

## 5. Security Issues Specific to Automatic Learning

1. **Prompt Injection & Instruction Poisoning inside Datasets/Docs**:
   - Untrusted text containing system-override sequences (`"ignore all previous instructions"`, `"you are now"`, `curl | bash`, `<|im_start|>system`, shell backticks masquerading as tool commands) must be flagged/quarantined during `dataset_validator.py` and clearly labeled as untrusted data during RAG retrieval.
2. **Unsafe Archive Extraction / Serialization**:
   - Never load `.pkl`, `.pt`, `.bin` pickled checkpoints during inspection; prefer JSON/JSONL/Parquet/Markdown/plain text cleanly. Enforce path-slip protection on any extracted files, strict SHA-256 checksums, and max file/dataset byte limits.
3. **License & Usage Terms Enforcement**:
   - Reject datasets with missing/`unknown` or incompatible restrictive licenses when configurable (`require_verified_license: true`), storing license metadata before moving any candidate into approved training sets.
4. **Secret Scrubbing Before Indexing or Training**:
   - Prevent API keys, JWTs, passwords, `.env` lines, and private keys from entering knowledge indexes, feedback examples, or training JSONL builds.
5. **Local Mode Isolation**:
   - Whenever `settings.internet.enabled == False` (`offline_enforced == True`), remote Hugging Face / web knowledge sync and dataset downloads MUST fail fast with an explicit Local Mode guard error (logged via `audit.py`).

---

## 6. Performance Problems to Prevent

- Indexing 500–5,000 documentation chunks directly into `notes/` would stall the `NoteStore.graph()` $O(N^2)$ link builder and clutter the UI 3D galaxy. Store ingested knowledge chunks separately under `data/learning/knowledge/index.db` (SQLite FTS5 / chunk metadata + incremental hash check) so `notes/` stays crisp for personal notes while knowledge search executes in `<10 ms`.
- Background indexing and any optional training worker must run asynchronously/in separate subprocesses with bounded queues and cooperative pause/cancel flags so interactive voice latency (`voice.py`) and streaming chat never degrade.

---

## 7. Files Requiring Changes

- `jarvis/config.py`, `config.json`: Add validated `LearningSpec` (`enabled`, `automatic_knowledge_updates`, `automatic_feedback_collection`, `automatic_training`, `require_training_approval`, `minimum_examples`, `evaluation_required`, `auto_promote`, `rollback_enabled`, allowed knowledge sources, allowed dataset licenses).
- `jarvis/intent.py`, `jarvis/persona.py`, `jarvis/assistant.py`: Route knowledge/feedback/training/eval/rollback commands cleanly, inject top-ranked knowledge chunks + approved feedback lessons concisely without overflowing `num_ctx = 4096`.
- `jarvis/api.py`: Add `/api/learning/*` endpoints for overview, knowledge sync/rollback, feedback CRUD/approval, dataset discovery/validation/build, training jobs (start/pause/resume/cancel/logs), evaluation runs, registry promotion/rollback.
- `jarvis/netsec.py` / `jarvis/search.py`: Extend `DEFAULT_SEARCH_HOSTS` / configurable allowlisted doc & Hugging Face API hosts (`huggingface.co`, `datasets-server.huggingface.co`, `docs.python.org`) under explicit Online Assisted Mode scoping only.
- `viewer/index.html`, `viewer/style.css`, `viewer/js/panels.js`, `viewer/js/main.js`: Add Learning & Brain Dashboard drawer/tabs cleanly alongside Notes & System.
- `preflight.py`, `drill.py`: Add Phase 18 checks for learning config, knowledge index health, dataset validation safety, evaluation pipeline, registry, rollback, and Local Mode remote download block.

---

## 8. New Modules Genuinely Required

- `jarvis/learning/__init__.py`
- `jarvis/learning/knowledge_pipeline.py` (connectors, incremental SQLite/FTS chunk index, SHA-256 deduplication, language check, rollback versions, freshness telemetry, scheduler)
- `jarvis/learning/feedback.py` (structured feedback categories, privacy filter, approval workflow, topic-linked retrieval)
- `jarvis/learning/evaluation.py` (fixed 12-category benchmark runner comparing baseline vs candidate, latency/regression/tool/memory/hallucination scoring, pass/reject explanation)
- `jarvis/learning/training/`:
  - `dataset_sources.py` (HF datasets API connector + local/feedback/curated dataset descriptors & license metadata)
  - `dataset_manager.py` (immutable raw archive storage, SHA-256 verification, size limits, offline/online gate)
  - `dataset_validator.py` (schema, empty/corrupt, credential regex, prompt-injection/poisoning scan, license check)
  - `dataset_cleaner.py` (deduplication, language filter, quality scoring, leakage guard)
  - `dataset_builder.py` (JSONL/messages format conversion + deterministic train/val/test split manifest)
  - `training_config.py` (conservative validated LoRA/QLoRA/job parameters + dependency checks)
  - `training_manager.py` (hardware detection + VRAM/RAM estimator, job queue, lifecycle state machine, pause/resume/cancel)
  - `training_worker.py` (isolated subprocess worker supporting optional PEFT/Transformers/TRL backend when present, honest failure/capability guard when absent)
  - `model_registry.py` (central versioned registry for base models, Ollama tags, candidate adapters, optional cloud entries)
  - `adapter_manager.py` (compatibility matrix for `ollama_modelfile` / `gguf_adapter` / `peft_lora` + health test)
  - `rollback.py` (atomic active-pointer rollback preserving baseline)
  - `training_scheduler.py` (policy checks: manual/scheduled/min approved examples/dataset version changes)
  - `training_audit.py` (append-only JSONL audit log for downloads, validations, approvals, promotions, rollbacks)

---

## 9. Tests Needed

- `tests/test_knowledge_pipeline.py`: Incremental indexing, duplicate detection, corrupt input rejection, version history & rollback, HF/doc connector (mocked HTTPS + Local Mode block), freshness stats.
- `tests/test_feedback_learning.py`: Positive/negative/correction capture, confirmation before persistent promotion, secret scrubbing, retrieval ranking boost, forgetting/deletion.
- `tests/test_training_pipeline.py`: Dataset license gate, immutable raw copy preservation, poisoning/injection/credential quarantine, JSONL<->chat conversion & split leakage check, hardware estimator on GTX 1650 4 GB specs, job pause/resume/cancel lifecycle cleanly tested in subprocess/mocked worker, evaluation regression rejection vs promotion + one-step rollback, training audit log.
- `tests/test_learning_api_and_commands.py`: NLP voice/text learning commands + `/api/learning/*` contract + dashboard telemetry authenticity.

---

## 10. Implementation Separation Invariant

1. **Knowledge Updates (`knowledge_pipeline.py`)** = Update indexed chunks in `data/learning/knowledge/` (NO model weights modified; must never claim model retraining occurred).
2. **Feedback Learning (`feedback.py`)** = Store approved corrections & style/pattern preferences for retrieval & future curation (NO automatic uncurated weight updates).
3. **Training & Evaluation (`learning/training/` + `evaluation.py`)** = Optional, hardware-gated, approval-gated candidate adapter training tested against the 12-category suite before promotion, with immutable base models and one-click rollback.

