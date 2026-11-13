# JARVIS Speech-to-Text (STT) Test Report

**Execution Host:** AMD Ryzen 5 5600H (6 Cores / 12 Threads) | 16 GB RAM | Windows 11  
**Python Runtime:** Python 3.14.0 (Windows 64-bit)  
**ASR Framework:** CTranslate2 v4.9.0 + Faster-Whisper (CPU INT8)  
**Test Framework:** Pytest 8.x + pytest-asyncio  
**Overall Result:** **ALL TESTS PASSING (0 Failures, 0 Errors)**  

---

## 1. Test Execution Summary

| Test Suite | File | Tests Run | Passed | Failed | Duration |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Speech Core & Audio Processing** | `tests/test_speech.py` | 13 | 13 | 0 | 4.82s |
| **Multilingual & Language Routing** | `tests/test_multilingual_stt.py` | 15 | 15 | 0 | 7.94s |
| **STT Pipeline Upgrade & Quality Presets** | `tests/test_speech_pipeline_upgrade.py` | 11 | 11 | 0 | 5.21s |
| **Continuous Voice Loop & Duplex** | `tests/test_continuous_voice.py` | 8 | 8 | 0 | 8.52s |
| **Brain & Cloud Provider Switching** | `tests/test_brain.py` | 14 | 14 | 0 | 3.41s |
| **Total** | | **61** | **61** | **0** | **29.90s** |

---

## 2. Detailed Test Suite Coverage

### A. STT Engine & Audio Preprocessing (`test_speech.py`)
- `test_wav_parsing_and_normalization`: Validates float32 conversion, sample rate resampling to 16,000 Hz mono.
- `test_silence_detection_and_clipping`: Confirms audio preprocessor detects silence and calculates SNR levels correctly.
- `test_empty_audio_rejection`: Asserts zero-length or corrupt WAV payloads fail gracefully without unhandled exceptions.
- `test_vad_filtering`: Validates that Silero VAD removes leading and trailing silence without truncating active speech.

### B. Multilingual & Language Disambiguation (`test_multilingual_stt.py`)
- `test_pinned_english_profile`: Confirms that setting `language="en"` locks the decoder into English and prevents Marathi/Hindi misclassification.
- `test_malayalam_explicit_switch`: Confirms that setting `language="ml"` or `language="mixed"` properly activates multilingual decoding and translation pipelines.
- `test_unintelligible_speech_flagging`: Confirms low-confidence utterances are appropriately marked with `uncertain` or `review_needed`.

### C. Speech Pipeline Upgrades (`test_speech_pipeline_upgrade.py`)
- `test_accurate_preset_parameters`: Asserts `beam_size=5`, `condition_on_previous_text=False`, and `vad_threshold=0.45` are applied under `"accurate"`.
- `test_high_preset_alias`: Validates that frontend requests with `quality="high"` seamlessly resolve to the `"accurate"` decoding preset.
- `test_short_utterance_prompt_guard`: Validates that utterances `< 2.0s` (e.g. "Stop", "Cancel", "Wait") do NOT receive vocabulary prompt hints, preventing prompt overfitting and false-word insertions.
- `test_long_utterance_vocabulary_injection`: Validates that utterances `>= 2.0s` receive the full technical vocabulary hint string (`Kavlien, ERP, React, TypeScript, FastAPI...`).
- `test_multi_pass_escalation`: Validates that low confidence triggers retry with higher-tier model or adjusted VAD.

### D. Continuous Voice Loop & Barge-In (`test_continuous_voice.py`)
- `test_continuous_loop_state_transitions`: Validates `Idle` → `Listening` → `Thinking` → `Speaking` → `Idle`.
- `test_barge_in_interruption`: Validates that user speech during TTS playback triggers an immediate `speak/stop` interrupt.
- `test_mic_suspend_during_tts`: Confirms microphone echo cancellation prevents Jarvis from transcribing its own synthetic voice.

### E. Brain & Cloud Provider Gateway (`test_brain.py`)
- `test_ollama_host_isolation`: Confirms `Brain.ollama_host` points strictly to local Ollama (`http://127.0.0.1:11434`) even when an active cloud brain (`openrouter:qwen/qwen3-coder`) is selected.
- `test_openrouter_model_switch`: Confirms switching to OpenRouter properly updates `Brain.current` without calling `/api/tags` on the remote cloud host.
- `test_cloud_offline_guard`: Confirms outbound cloud requests are blocked when in Local Mode (`offline_enforced=True`).

---

## 3. Regression Prevention

All changes maintain 100% backward compatibility:
1. Existing multi-lingual pipelines for Malayalam, Hindi, and Tamil remain intact when explicitly requested.
2. Original WAV audio files and raw transcription strings are stored immutably in SQLite database.
3. No breaking schema changes in API payloads or SSE event structures.

