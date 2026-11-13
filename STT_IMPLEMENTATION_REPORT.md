# JARVIS Speech-to-Text (STT) Accuracy Overhaul — Implementation Report

**Author:** DeepMind Agent Engineering Team  
**Date:** November 13, 2026  
**System Target:** JARVIS Personal AI Assistant (`myjarvis-main`)  
**Target Hardware:** AMD Ryzen 5 5600H (6 Cores / 12 Threads) | 16 GB RAM | NVIDIA GeForce GTX 1650 (4 GB VRAM)  
**Target Voice Profile:** English Only (`en`), Indian English Accent (`en-IN`)  
**Execution Environment:** Completely local, offline, CTranslate2 INT8 CPU  

---

## 1. Executive Summary

This engineering overhaul resolves accuracy, stability, and latency challenges within the JARVIS local speech-to-text subsystem. The previous implementation suffered from:
1. **Language Classification Drift:** Using `language="auto"` caused Indian English phonemes to be intermittently misclassified as Marathi (`mr`) or Hindi (`hi`).
2. **Repetition and Hallucination Loops:** Whisper's default `condition_on_previous_text=True` caused repetitive loops when ambient fan noise or trailing silence occurred.
3. **Vocabulary Prompt Overfitting:** Injecting large technical vocabulary prompts into short utterances (< 1.5s) caused false-positive insertions (e.g. transcribing "Stop" as "FastAPI").
4. **Cloud Model Routing & Settings Crash:** Switching to cloud providers (e.g., OpenRouter) previously misdirected Ollama health probes to cloud endpoints, producing UI crashes on the Settings panel.

### Core Achievements
- **Pinned English Voice Profile:** Locked `stt.language="en"` to eliminate multi-lingual drift, while preserving full multilingual capability when explicitly requested.
- **Upgraded Accurate Preset:** Configured `beam_size=5`, `condition_on_previous_text=False`, and `vad_threshold=0.45` to eliminate looping hallucinations.
- **Short-Utterance Guard:** Guarded prompt biasing with `raw_dur >= 2.0s`, ensuring clean transcription of rapid single-word commands.
- **Model Provisioning:** Added local support for `Systran/faster-whisper-medium.en` (1.46 GB model.bin) alongside `small.en` and `base`.
- **OpenRouter & Settings Resilience:** Isolated `ollama_host` from cloud provider endpoints and updated SettingsPanel to prevent any model catalog failure from breaking the UI.
- **Real-Time Accuracy HUD:** Enhanced the Speech Inspector with live WER/CER calculation against user-corrected reference transcripts.

---

## 2. Key Codebase Changes & Architecture

### A. Engine Upgrades in [`jarvis/stt.py`](file:///d:/projects/myjarvis/jarvis/stt.py)
1. **Preset Alignment:** Aliased `"high"` to `"accurate"` to eliminate configuration mismatches between frontend and backend.
2. **Hallucination Prevention:** Hardened decoding parameters to set `condition_on_previous_text=False` across all quality tiers.
3. **Prompt Injection Duration Guard:**
   ```python
   # Only apply initial prompt biasing on sustained utterances to avoid overfitting single-word commands
   if raw_dur >= 2.0 and hints:
       decode_kwargs["initial_prompt"] = hints
   ```
4. **Multi-Pass Quality Upgrades:** On low-confidence or high-repetition outputs, automatically re-transcribes using an upgraded model (`small.en` → `medium.en` or `base` → `small.en`) and adjusts VAD filtering.

### B. Configuration Hardening in [`jarvis/config.py`](file:///d:/projects/myjarvis/jarvis/config.py)
- Updated `STTSpec` defaults:
  - `model`: `"small.en"`
  - `language`: `"en"`
  - `quality`: `"accurate"`
  - `max_cached_models`: `1` (strict RAM control for 16 GB host)
  - `hotwords`: `True`

### C. Technical Vocabulary Expansion in [`config/vocab_hints.txt`](file:///d:/projects/myjarvis/config/vocab_hints.txt)
Expanded vocabulary with over 30 domain terms including:
- Projects & Modules: `Kavlien`, `ERP`, `ERP_V2`, `Graphify`
- Web & Backend: `React`, `FastAPI`, `TypeScript`, `Node.js`, `Python`, `PostgreSQL`, `Redis`, `Docker`, `Kubernetes`
- AI & Models: `Whisper`, `Faster-Whisper`, `Ollama`, `Qwen`, `OpenRouter`, `Qdrant`, `ChromaDB`

### D. Provider Gateway & Settings Isolation in [`jarvis/brain.py`](file:///d:/projects/myjarvis/jarvis/brain.py) & [`jarvis/api_v2.py`](file:///d:/projects/myjarvis/jarvis/api_v2.py)
- Replaced ambiguous `self.host` references with explicit `self.ollama_host` (`http://127.0.0.1:11434`), ensuring cloud provider base URLs (e.g. `https://openrouter.ai/api/v1`) are never queried for Ollama version or model tags.
- Added graceful `.catch()` in `SettingsPanel.tsx` around `/api/models` to prevent catastrophic UI failure if local LLM daemon is offline.
- Added active cloud provider prioritization in `_stream_llm_with_tts` so user-selected cloud models are immediately utilized for completions.

### E. Frontend Speech Inspector Upgrades in [`apps/frontend/src/components/SpeechInspectorPanel.tsx`](file:///d:/projects/myjarvis/apps/frontend/src/components/SpeechInspectorPanel.tsx)
- Added `medium.en` and `"accurate"` preset to inspector options.
- Built-in Levenshtein distance algorithm calculating live Word Error Rate (WER) and Character Error Rate (CER) against edited references.
- Added "Save as Verified Benchmark Reference" and "Mark Inaccurate" one-click action buttons.

---

## 3. Verification & Automated Test Results

- **Speech Pipeline Suites:** 47 passed, 0 failed in 26.49s (`tests/test_speech.py`, `tests/test_multilingual_stt.py`, `tests/test_speech_pipeline_upgrade.py`, `tests/test_continuous_voice.py`).
- **OpenRouter API Key Verification:** Validated live connectivity using encrypted DPAPI key; verified streaming responses through `ModelGateway` and `Brain._cloud_complete`.
- **Frontend Build:** Clean compilation with Vite (`tsc --noEmit && vite build`) in 997ms.

