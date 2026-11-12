# Jarvis Speech Recognition & Translation Accuracy — Targeted Fix Report

## 1. Root Causes Fixed

1. **Specialised Malayalam Provider Early-Return Bypass (`jarvis/stt.py`)**:
   - Previously, `Whisper.transcribe()` returned immediately when `_choose_special_provider()` returned a provider on `language="ml"`, bypassing `quality_tier` validation, `script_mismatch` checks, empty-output detection, repetition checks, and exception handling.
   - Fixed by replacing the early return with a unified candidate pipeline (`Whisper._resolve_special_provider()` + `Whisper.transcribe()`): specialist output is evaluated with `evaluate_transcript_quality()` and only short-circuits when `quality_tier == "high"` with zero quality flags and non-empty text. Low-quality output, `script_mismatch`, empty output with speech energy, or provider exceptions fall through automatically to `Faster-Whisper`, record `provider_status` and `provider_errors`, and compare scored candidates.
   - `language="mixed"` and `language="auto"` never force monolingual Malayalam-only ASR upfront, and final candidate selection preserves Latin technical terms in `mixed` mode.

2. **ASR Quality Gate & Candidate Scoring (`jarvis/stt.py`)**:
   - Strengthened `_has_repetition_loop()` to detect consecutive identical words (`>= 3`), repeated bigrams, and character runs (`>= 6`).
   - Upgraded `evaluate_transcript_quality()` with language-aware words-per-second thresholds, suspiciously-long hallucination detection on short clips (`short_audio_long_hallucination`), explicit `language_mismatch`, `borderline_confidence` (`medium` / `review_needed` tier), and `script_mismatch` detection (catching Devanagari and multi-word Latin-only hallucinations when `ml` is expected, while preserving short valid commands such as `"Stop."`, `"Yes"`, and `"അതെ"`).
   - Upgraded `_candidate_score()` with tier/script weighting and cross-pass consistency scoring without length bias or uncalibrated confidence assumptions.

3. **Bounded Diverse Retries & Thread/RAM Safety (`jarvis/stt.py`, `jarvis/asr_providers.py`)**:
   - Retries track tried model/decoding signatures (`tried_fw_configs`, `tried_providers`) so identical model+settings combinations never run twice.
   - Respected `cpu_threads` across `MalayalamASRProvider` (CT2 INT8 & Transformers) and `IndicConformerProvider` (`sherpa_onnx`), and unloaded superseded specialist/Whisper models when `max_cached_models <= 1`.
   - Local weights under `models/asr/` are never auto-downloaded at runtime; missing weights report `provider_status.available = false` and `provider_unavailable` cleanly while falling back to `Faster-Whisper`.

4. **Guarded Correction & Translation Verification (`jarvis/translator.py`)**:
   - Strengthened `guard_correction()` to reject LLM conversational chatter (`correction_rejected_llm_chatter`), date/time shifts (`correction_rejected_datetime_mismatch`), technical identifier / proper entity swaps (`correction_rejected_entity_mismatch`), meaningful word drop/insertion (`correction_rejected_word_drift`), negation & local negation-scope changes (`correction_rejected_negation_change`), question/command intent flips (`correction_rejected_intent_change`), and cosmetic paraphrases (`correction_rejected_paraphrase`), while keeping `original_transcript` immutable.
   - Strengthened `verify_translation()` with Indic/English negation parity, date/time/day anchor checks, technical identifier preservation across directions, question/intent parity, `untranslated_copy` detection, length ratio bounds, and explicit honest metadata (`semantic_equivalence_proven: false`, `check_method: "heuristic_anchors"`).
   - Fixed a key-lookup bug in `Translator.process_stt()` where `recognition_quality` previously looked up `stt_res.get("quality")` instead of `stt_res.get("recognition_quality")` (which had caused records to fall back to `"medium"` instead of reporting `"high"` or `"low"`), and persisted `review_needed`, `provider_status`, and `provider_errors` inside `VoiceTranscriptRow.meta_json`.

5. **UI Status Clarity (`apps/frontend/src/components/SpeechInspectorPanel.tsx`, `apps/frontend/src/speechStatus.ts`)**:
   - Inspector badges now distinguish `High quality`, `Needs review`, `Low quality`, `Provider unavailable` + provider fallback notes, `Translation uncertain` + heuristic note, and per-attempt `provider`, `reason`, `quality_tier`, and `error` details while preserving the existing two-destination (`JARVIS ASSISTANT` / `SETTINGS`) HUD layout.

---

## 2. Verification & Benchmark Evidence

- **Full Python Suite (`.\.venv\Scripts\pytest.exe -q -rs`)**: **232 passed, 4 skipped, 0 failed** (`352.31s`).
  - Skipped host-dependent integration tests: Docker daemon not running (`test_p10_11_hardening.py:221`, `test_p10_12_hardening.py:165`) and `JARVIS_PG_TEST_URL` / `JARVIS_PG_SCRATCH_URL` unset (`test_p10_11_hardening.py:378`, `test_p10_pgvector_memory.py:193`).
- **Focused Speech Suite (`tests/test_speech_pipeline_upgrade.py tests/test_multilingual_stt.py tests/test_speech.py -k "not mic"`)**: **27 passed, 3 deselected**.
- **Static Checks**: `ruff check` (`All checks passed!`) and `mypy` (`Success: no issues found in 4 source files`) exited `0`.
- **Frontend (`apps/frontend`)**: `npm test` **7 passed, 0 failed** (`graphCore.test.ts` + `speechStatus.test.ts`), `tsc --noEmit && vite build` exited `0` (`43` modules transformed, `dist/assets/index-CT6zLmQu.js` `266.86 kB`).
- **Synthetic Verified English Benchmark (`benchmarks/multilingual/manifest.json`, 15 WAVs, `balanced`, 6 threads)**:
  - `small balanced`: Load `4.09s`, Peak RSS `780.2 MB`, Mean Latency `6.13s`, Lang Acc `100.0%`, WER `10.7%`, CER `3.7%`, Script Mismatch `0`, Empty `0`, Hallucinations `0`.
  - `base balanced`: Load `1.41s`, Peak RSS `924.1 MB`, Mean Latency `3.13s`, Lang Acc `100.0%`, WER `12.9%`, CER `3.5%`, Script Mismatch `0`, Empty `0`, Hallucinations `0`.
- **Real Microphone WAV Benchmark (`benchmarks/multilingual/manifest_live.json`, 4 WAVs, `small balanced`, 6 threads)**:
  - Separated strictly from synthetic WAVs with zero fabricated references (`Verified Reference Counts: {}`, WER/CER reported as `N/A`).
  - `en` (2 WAVs): Lang Acc `100.0%`, Script Mismatch `0`, Empty `0`, Hallucinations `0`.
  - `ml` (`benchmarks/samples/live_malayalam.wav`): Lang Acc `100.0%`, Script Mismatch `1` (generic Whisper `small` emits Devanagari on Malayalam; cleanly flagged `recognition_quality='low'`, `quality_flags=['script_mismatch']`, `retry_triggered=True` since local specialized Malayalam weights under `models/asr/` are not installed on disk).
  - `mixed` (`benchmarks/samples/live_mixed.wav`): Empty `1`, Hallucinations `0` (silent/noise-floor capture short-circuited cleanly as `no_speech_energy`).

## 3. Honest Remaining Hardware / Asset Limits

- No local specialized Malayalam checkpoint (`models/asr/whisper-small-malayalam-v2-ct2`, `models/asr/whisper-small-malayalam-v2`, or `models/asr/indicconformer-ml`) or human-verified Malayalam/mixed reference transcript corpus is present in this workspace yet; until those local assets or consented user recordings are supplied, generic CPU Whisper models cannot yield verified Malayalam WER numbers and will continue flagging Devanagari drift honestly via `script_mismatch`.

