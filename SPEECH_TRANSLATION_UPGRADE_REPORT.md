# Jarvis Speech Recognition & Translation Accuracy Upgrade — Final Report

## 1. Root Causes Identified (`SPEECH_TRANSLATION_AUDIT.md`)

1. **Silence/Noise Amplification Hallucination Loop**: Blind `0.7 / peak` normalization scaled dead room noise floors (e.g. `benchmarks/samples/live_mixed.wav`, `rms=0.00058`, `peak=0.0027`) by ~259x into fake high-logprob Whisper repetition loops (`-0.057`).
2. **Capture & Browser Resampling Artifacts**: Browser `VoiceDock.tsx` used un-filtered nearest-neighbor decimation down to 16 kHz (`Math.floor(i * ratio)`), introducing aliasing; backend `VoiceLoop` resampled 80 ms chunks individually instead of resampling the whole utterance once.
3. **Key Mismatch in Live Transcript Events**: `jarvis/api.py` and `jarvis/assistant.py` read `stt_res.get("language")` instead of `stt_res.get("detected_language")`, publishing `"unknown"` on transcript events.
4. **Translator Configuration & Attribute Mismatches Discovered & Fixed During Verification**: `TranslationSpec` uses `mode == "translate"` and `auto_correct`, and `Brain` exposes `stream()`/`complete()` rather than `stream_raw()`; all paths were unified and verified against live `Brain` and mock brains.
5. **Malayalam Script Drift in Base OpenAI Whisper Models**: Empirically confirmed on `benchmarks/samples/live_malayalam.wav` that OpenAI Whisper `small` outputs Devanagari script even with `language="ml"`. Pluggable local adapters (`MalayalamASRProvider`, `IndicConformerProvider`) and a deterministic `script_mismatch` quality gate now detect and handle this cleanly.

## 2. Implemented Changes

- **Audio Diagnostics & Safe Preprocessing (`jarvis/audio_preprocess.py`)**:
  - Added `parse_wav_raw`, `downmix_to_mono`, `bandlimited_resample`, `diagnose_audio`, and configurable profiles (`off`, `light`, `standard`) with 65 Hz high-pass filtering, margin-preserving trimming, short-utterance padding, and bounded normalization that skips sub-threshold silence floors.
- **Native Capture & Browser Audio Preservation (`jarvis/voice.py`, `apps/frontend/src/components/VoiceDock.tsx`)**:
  - Updated `VoiceLoop` to prefer hardware native sample rates when opening real `sounddevice.InputStream`, downmix stereo inputs, resample whole concatenated utterances cleanly once per turn, and attach native capture metadata via `pop_utterance_meta`.
  - Updated `encodeWav16` in `VoiceDock.tsx` to preserve native sample rate PCM16 WAV without nearest-neighbor aliasing.
- **ASR Provider Architecture, Vocabulary Hints & Quality-Gated Retry (`jarvis/asr_providers.py`, `jarvis/stt.py`, `config/vocab_hints.txt`, `jarvis/config.py`, `config.json`)**:
  - Added `ASRProvider`, `FasterWhisperProvider` (LRU cache capped by `max_cached_models=2`), and offline-safe adapters `MalayalamASRProvider` (`sajilck/whisper-small-malayalam-v2`) and `IndicConformerProvider` (`ai4bharat/indicconformer_stt_ml_hybrid_ctc_rnnt_large`).
  - Added `AudioBufferStore` disk/memory preservation for untouched original audio (`data/audio/`), externalized `config/vocab_hints.txt`, `evaluate_transcript_quality` (logprob, no-speech, repetition/compression ratio, word rate, Indic Unicode script consistency), and bounded multi-pass candidate scoring.
- **Guarded Correction, Code-Switched Translation & Semantic Verification (`jarvis/translator.py`, `jarvis/db/models.py`, `jarvis/db/engine.py`, `jarvis/db/migrations/versions/0006_voice_pipeline_upgrade.py`)**:
  - Added `guard_correction` (blocks expansions, truncations, mutated numbers, flipped negations), code-switching aware translation prompts, `verify_translation` + 1 automatic repair retry, DB-backed `TranscriptStore` (`VoiceTranscriptRow` + Alembic `0006`), immutable `original_transcript` enforcement, deletion APIs, and strictly opt-in `export_consented_sample`.
- **REST API & Centralized UI (`jarvis/api.py`, `jarvis/assistant.py`, `apps/frontend/src/components/SpeechInspectorPanel.tsx`, `apps/frontend/src/features/AssistantHome.tsx`, `apps/frontend/src/features/SettingsPanel.tsx`)**:
  - Preserved native WAV bytes in `/api/voice/hear` & `/api/voice/command`, fixed `detected_language` event reporting, and added `/api/voice/audio/{audio_id}`, `/api/voice/diagnostics/{audio_id}`, `/api/voice/retry`, `/api/voice/eval-consent`, and `DELETE /api/voice/transcripts`.
  - Added the contextual **Speech & Translation Inspector** (`Voice (N)`) inside `JARVIS ASSISTANT` plus preprocessing, quality, vocabulary hints, Malayalam model path, and translation verification controls inside `SETTINGS`.

## 3. Benchmarks & Verification Summary

- **15-WAV Benchmark (`benchmarks/multilingual/results_baseline.json` vs `benchmarks/multilingual/results_after.json`)**:
  - `base fast`: WER **20.4% → 10.0%** | CER **5.2% → 3.3%**
  - `base balanced`: WER **13.1% → 12.9%** | CER **3.4% → 3.5%**
  - `small balanced`: WER **20.3% → 10.7%** | CER **5.0% → 3.7%**
  - `small.en balanced`: WER **19.9% → 11.8%** | CER **4.8% → 3.5%**
- **Known Limitations & Remaining Gaps**:
  - Specialized local Malayalam checkpoints (`whisper-small-malayalam-v2-ct2` or `indicconformer-ml`) are intentionally NOT auto-downloaded; until placed under `models/asr/`, base Whisper models on CPU still risk Devanagari drift on conversational Malayalam (now cleanly flagged with `quality_flags=['script_mismatch']`).
  - Live Docker and PostgreSQL+pgvector tests remain skipped where host Docker daemon or `JARVIS_PG_TEST_URL` are not active.

