# Jarvis Speech Recognition & Translation Accuracy — Targeted Fix Report

## 1. Root Causes Fixed

1. **Specialised Malayalam Provider Availability & Candidate Pipeline (`jarvis/stt.py`, `jarvis/asr_providers.py`)**:
   - Added `_provider_expected_dirs()` and `_resolve_special_provider_status()` in `Whisper`: when `language="ml"` is requested and neither specialised Malayalam provider (`malayalam_whisper`, `indic_conformer`) is installed locally, `provider_status` explicitly reports `available=False`, `state="unavailable"`, `reason`, and `expected_dir` / `expected_dirs` while falling back cleanly to `Faster-Whisper` without attempting automatic model downloads.
   - Strictly separated **unavailable** (not installed locally -> `"provider_unavailable"` flag only, `provider_errors=[]`) from runtime exceptions (actual model/runtime failure recorded in `provider_errors`). When generic `Faster-Whisper` produces a `script_mismatch` on `ml`, `review_needed=True` is set and surfaced in the API and UI.
   - Specialist outputs never bypass quality validation: evaluated with `evaluate_transcript_quality()` and only short-circuited when `quality_tier == "high"` with zero quality flags and non-empty text. `language="mixed"` and `language="auto"` never force monolingual Malayalam-only ASR upfront, and final candidate selection preserves non-specialist code-switched ML+EN candidates against monolingual specialist replacement.

2. **Honest Confidence, Quality Gate & Candidate Scoring (`jarvis/asr_providers.py`, `jarvis/stt.py`, `jarvis/translator.py`)**:
   - Eliminated hardcoded synthetic confidence (`0.72` / `0.78`) and hardcoded `language_probability` (`1.0` / `0.95`) from `MalayalamASRProvider` and `IndicConformerProvider` (`None` + `confidence_available=False` / `language_probability_available=False` when not measured by the backend; `_segments_metrics()` and `_compute_confidence()` also return `None` when logprob metrics are absent). Stored exact `None` values faithfully in `TranscriptStore` (`VoiceTranscriptRow.meta_json`) without breaking SQLite/PostgreSQL schema compatibility.
   - Neutral-centered `_candidate_score()` so missing `avg_logprob`/`no_speech_prob`/`language_probability` contribute `0.0` (never penalizing measured Whisper passes against unmeasured passes), keeping language/script compatibility and quality tier primary.
   - Strengthened `_has_repetition_loop()` (consecutive identical words `>= 3`, repeated bigrams, char runs `>= 6`) and `evaluate_transcript_quality()` (language-aware words-per-second thresholds, `short_audio_long_hallucination`, explicit `language_mismatch`, `borderline_confidence`, `script_mismatch`, while keeping short valid commands such as `"Stop."`, `"Yes"`, and `"അതെ"` as `high`). Exposed `selection_reason` and `selected_candidate` cleanly across STT, DB persistence, REST API, and UI.

3. **Bounded Diverse Retries & Thread/RAM Safety (`jarvis/stt.py`, `jarvis/asr_providers.py`)**:
   - Retries track tried model/decoding signatures (`tried_fw_configs`, `tried_providers`) so identical model+settings combinations never run twice.
   - Respected `cpu_threads` across `MalayalamASRProvider` (CT2 INT8 & Transformers) and `IndicConformerProvider` (`sherpa_onnx`), and unloaded superseded specialist/Whisper models when `max_cached_models <= 1`.

4. **Guarded Correction, Structural Translation Verification & Preservation (`jarvis/translator.py`)**:
   - Strengthened `guard_correction()` to reject LLM conversational chatter (`correction_rejected_llm_chatter`), date/time shifts (`correction_rejected_datetime_mismatch`), technical identifier / proper entity swaps (`correction_rejected_entity_mismatch`), meaningful word drop/insertion (`correction_rejected_word_drift`), negation & local negation-scope changes (`correction_rejected_negation_change`), question/command intent flips (`correction_rejected_intent_change`), and cosmetic paraphrases (`correction_rejected_paraphrase`), while keeping `original_transcript` immutable.
   - Updated `verify_translation()` with explicit structural wording (`structural_check: True`, `semantic_equivalence_proven: False`, note `"Structural anchor check only (numbers, negation, intent, technical terms, target script); not proof of semantic accuracy."`), keeping original, corrected, and translated text independent.
   - Updated `Translator.apply_correction()` so a failed or empty subsequent translation attempt preserves any earlier valid `translated_text` (`previous_translation_preserved: True` and `"translation_retry_failed_kept_previous"` warning) instead of overwriting it. Fixed `recognition_quality` key lookup in `process_stt()`.

5. **UI Status Clarity (`apps/frontend/src/components/SpeechInspectorPanel.tsx`, `apps/frontend/src/speechStatus.ts`)**:
   - Inspector renders `Conf: unavailable` (`Conf N/A` in selector buttons) when `confidence == null` or `confidence_available === false` instead of `0%`.
   - Distinguishes `Provider unavailable` (with `reason` and `expected_dir`) from runtime `Provider errors`, displays `"Structural check passed (not semantic proof)"` / `"Translation uncertain: ..."`, shows `"Previous valid translation preserved"` when applicable, and displays `selection_reason` + per-attempt metrics in `"Compare passes"`.

---

## 2. Verification & Benchmark Evidence

- **Full Python Suite (`.\.venv\Scripts\pytest.exe -q -rs`)**: **233 passed, 4 skipped, 0 failed** (`236.48s`).
  - Skipped host-dependent integration tests: Docker daemon not running (`test_p10_11_hardening.py:221`, `test_p10_12_hardening.py:165`) and `JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL` unset (`test_p10_11_hardening.py:378`, `test_p10_pgvector_memory.py:193`).
- **Focused Speech Suite (`tests/test_speech_pipeline_upgrade.py tests/test_multilingual_stt.py tests/test_speech.py -k "not mic"`)**: **28 passed, 3 deselected, 0 failed** (`11.92s`).
- **Static Checks**: `ruff check` (`All checks passed!`) and `mypy` (`Success: no issues found in 4 source files`) exited `0`.
- **Frontend (`apps/frontend`)**: `npm test` **7 passed, 0 failed** (`graphCore.test.ts` + `speechStatus.test.ts`), `tsc --noEmit && vite build` exited `0` (`43` modules transformed, `dist/assets/index-Ubz16Thc.js` `267.95 kB`).
- **Synthetic Verified English Benchmark (`benchmarks/multilingual/manifest.json`, 15 WAVs, `balanced`, 6 threads)**:
  - `base balanced`: Load `1.34s`, Peak RSS `909.2 MB`, Mean Latency `2.10s` (`1.98x` RTF), Lang Acc `100.0%`, WER `12.9%`, CER `3.5%`, Script Mismatch `0` (`0.0%`), Empty `0`, Hallucinations `0`.
  - `small balanced`: Load `2.57s`, Peak RSS `909.2 MB`, Mean Latency `3.55s` (`1.88x` RTF), Lang Acc `100.0%`, WER `10.7%`, CER `3.7%`, Script Mismatch `0` (`0.0%`), Empty `0`, Hallucinations `0`.
- **Real Microphone WAV Benchmark (`benchmarks/multilingual/manifest_live.json`, 4 WAVs, `balanced`, 6 threads)**:
  - Separated strictly from synthetic WAVs with zero fabricated references (`Verified Reference Counts: {}`, WER/CER reported as `N/A`); benchmark procedure for adding manually verified `ml` and `mixed` WAVs documented in `benchmarks/multilingual/README.md`.
  - `base balanced`: Load `1.31s`, Peak RSS `907.4 MB`, Mean Latency `2.67s`, Lang Acc `100.0%`, Empty `1` (silent `mixed` capture), Hallucinations `0`.
  - `small balanced`: Load `2.57s`, Peak RSS `907.4 MB`, Mean Latency `2.49s`, Lang Acc `100.0%`, Script Mismatch `1` (`25.0%` overall / `100.0%` on `ml`), Empty `1`, Hallucinations `0`.

## 3. Honest Remaining Hardware / Asset Limits

- No local specialized Malayalam checkpoint (`models/asr/whisper-small-malayalam-v2-ct2`, `models/asr/whisper-small-malayalam-v2`, or `models/asr/indicconformer-ml`) or human-verified Malayalam/mixed reference transcript corpus is present in this workspace yet; until those local assets or consented human-verified recordings are added via `benchmarks/multilingual/README.md`, generic CPU Whisper models cannot yield verified Malayalam WER numbers and will continue reporting `provider_status.available = false` (`provider_errors = []`) and flagging Devanagari/Latin drift via `script_mismatch` with `review_needed = true`.

