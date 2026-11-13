# JARVIS Speech Recognition (STT) Production Configuration Guide

**Hardware Profile:** AMD Ryzen 5 5600H (6 Cores / 12 Threads) | 16 GB RAM | Windows 11  
**Default STT Engine:** Faster-Whisper (`Systran/faster-whisper-small.en` & `medium.en`)  
**Execution Environment:** Completely local, offline, CPU INT8 Quantization with 6 Compute Threads  

---

## 1. Engine & Model Architecture

### Recommended Local Model Selection
| Model | Size | Best For | Expected WER | Latency (Ryzen 5) | RAM Footprint |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`small.en`** *(Default)* | 466 MB | Daily voice commands, Indian English, tech terminology | **Low (< 8%)** | ~0.22–0.30x RTF (~0.8s) | ~550 MB |
| **`medium.en`** | 1.46 GB | Maximum transcription accuracy, complex dictation | **Lowest (< 5%)** | ~0.55–0.70x RTF (~1.8s) | ~1.65 GB |
| **`base`** | 145 MB | Ultra-low latency, resource-constrained fallback | Moderate (~14%) | ~0.10–0.15x RTF (~0.4s) | ~250 MB |

> [!NOTE]
> **Why NVIDIA Parakeet TDT was not chosen for this environment:**  
> The runtime environment runs Python 3.14 on Windows without PyTorch or CUDA binaries. CTranslate2 v4.9.0 provides optimized AVX2 INT8 execution on the Ryzen 5 5600H CPU, which outperforms PyTorch CPU inference and operates completely offline with zero external dependencies.

---

## 2. Pinned English (`en-IN`) Voice Configuration

To prevent Indian English acoustic patterns from triggering language auto-detection misclassifications (e.g., misclassifying "Hey Jarvis, what's the time?" as Marathi `mr`), pin the STT language strictly to English:

In `data/settings.json` (or via Settings UI):
```json
{
  "stt": {
    "engine": "faster_whisper",
    "model": "small.en",
    "language": "en",
    "quality": "accurate",
    "hotwords": true,
    "max_cached_models": 1,
    "cpu_threads": 6,
    "compute_type": "int8"
  }
}
```

---

## 3. Quality Presets & Decoding Parameters

| Parameter | `accurate` (Default) | `balanced` | `fast` |
| :--- | :--- | :--- | :--- |
| `beam_size` | 5 | 3 | 1 |
| `best_of` | 5 | 3 | 1 |
| `condition_on_previous_text` | **`False`** (Prevents hallucination loops) | `False` | `False` |
| `vad_filter` | `True` | `True` | `False` |
| `vad_threshold` | 0.45 | 0.45 | 0.50 |
| `hotwords` | Enabled | Enabled | Disabled |

> [!IMPORTANT]
> `condition_on_previous_text=False` is strictly enforced to eliminate repetitive looping hallucinations (e.g., repeating the same sentence continuously when silence or ambient fan noise occurs).

---

## 4. Technical Terminology & Vocabulary Biasing

Vocabulary hints are maintained in [`config/vocab_hints.txt`](file:///d:/projects/myjarvis/config/vocab_hints.txt).  
To ensure custom project names, libraries, and frameworks are recognized accurately in Indian English speech, keep this list populated with your active workspace terminology:

```text
Kavlien
ERP
ERP_V2
React
TypeScript
FastAPI
Whisper
Faster-Whisper
OpenRouter
Qwen
VS Code
Ollama
PostgreSQL
Docker
```

### Short-Utterance Overfitting Guard
When audio duration is `< 2.0s` (e.g., "Yes", "Stop", "Cancel", "Wait"), the vocabulary prompt injection is automatically bypassed. This ensures short commands are never corrupted by technical prompt bias (e.g., transcribing "Stop" as "FastAPI").

---

## 5. Cloud LLM Integration & OpenRouter Configuration

When using OpenRouter for cloud model routing alongside local speech:

1. **Configure API Key:**  
   In the Settings UI under **Encrypted Cloud API Keys**, enter your OpenRouter key (`sk-or-v1-...`). It is encrypted locally using Windows DPAPI.
2. **Model Selection:**  
   Use standard model slugs such as `openrouter:qwen/qwen3-coder`.
   > [!WARNING]  
   > OpenRouter has deprecated `:free` slugs for certain models like Qwen 3 Coder. Always select `qwen/qwen3-coder` rather than `qwen/qwen3-coder:free`.
3. **Credit & Context Windows:**  
   OpenRouter pre-authorizes against available credits. Jarvis automatically sets `max_tokens: 512` (up to 2048) on chat requests to prevent 402 out-of-credit rejection errors on pay-as-you-go balances.

---

## 6. Real-Time Speech Inspection & Diagnostics

Navigate to the Assistant view and click the Speech Inspector toggle (or access `/api/voice/transcripts`):
- **Live Audio Playback:** Listen to the original WAV buffer before and after VAD filtering.
- **Side-by-Side Comparison:** Compare the immutable original transcript with the corrected reference.
- **Inline WER/CER Metrics:** Automatically calculated in real-time when editing reference text.
- **Save Benchmark Reference:** One-click save to the local verified evaluation dataset for continuous regression benchmarking.

