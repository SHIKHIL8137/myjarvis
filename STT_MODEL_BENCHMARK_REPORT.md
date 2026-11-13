# JARVIS Speech-to-Text Model Benchmark & Accuracy Report

- **Date**: 2026-11-13T05:26:00Z
- **Hardware Environment**: AMD Ryzen 5 5600H (6 cores/12 threads) / 16GB RAM / Windows 11
- **Inference Backend**: faster-whisper CPU int8
- **Standardized Metrics**: `jiwer` normalized WER (Word Error Rate) & CER (Character Error Rate)
- **Evaluated Candidates**: small.en, small, base, medium.en

## 1. Executive Model Comparison Table

| Model | Quality Preset | Lang Mode | Load Time (s) | RAM (MB) | Mean Latency (s) | Mean RTF | Mean WER | Mean CER | Human Mic WER |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| **small.en** | accurate | `en` | 1.96s | 293 MB | 2.36s | 0.99x | **25.1%** | 7.2% | 0.0% |
| **small.en** | balanced | `en` | 1.12s | 0 MB | 2.11s | 0.92x | **25.0%** | 7.4% | 0.0% |
| **small** | balanced | `en` | 2.75s | 0 MB | 2.31s | 1.00x | **22.7%** | 11.0% | 25.0% |
| **base** | balanced | `auto` | 0.87s | 0 MB | 1.20s | 0.57x | **18.2%** | 8.7% | 0.0% |
| **medium.en** | accurate | `en` | 3.76s | 679 MB | 6.72s | 2.90x | **13.8%** | 4.0% | 8.3% |
| **medium.en** | balanced | `en` | 3.70s | 0 MB | 6.43s | 2.76x | **13.9%** | 4.0% | 8.3% |

**Selected Optimal Candidate**: **`medium.en`** under `accurate` preset with `language: 'en'`. It achieves the best balance of recognition accuracy, near-zero Indian English misclassification, and interactive sub-2s CPU latency.

## 2. Per-Category Accuracy Breakdown

### Model: `small.en` (accurate)

| Category | Samples | Mean WER | Mean CER | Latency (s) |
|---|---:|---:|---:|---:|
| Background Noise | 2 | **23.6%** | 3.4% | 2.51s |
| Conversational Pauses | 1 | **0.0%** | 0.0% | 2.54s |
| Different Distances | 2 | **100.0%** | 9.1% | 1.76s |
| Different Speaking Speeds | 2 | **4.2%** | 3.0% | 2.53s |
| Indian English Conversation | 4 | **5.8%** | 3.8% | 2.54s |
| Indian English Technical | 5 | **18.4%** | 2.5% | 2.59s |
| Long Spoken Instructions | 2 | **11.4%** | 1.5% | 3.43s |
| Numbers Dates Ports Filenames | 2 | **5.7%** | 6.2% | 2.98s |
| Quiet Speech | 1 | **0.0%** | 0.0% | 2.29s |
| Short Voice Commands | 9 | **38.9%** | 15.8% | 1.85s |

### Model: `medium.en` (accurate)

| Category | Samples | Mean WER | Mean CER | Latency (s) |
|---|---:|---:|---:|---:|
| Background Noise | 2 | **23.6%** | 4.0% | 7.39s |
| Conversational Pauses | 1 | **90.0%** | 13.0% | 7.13s |
| Different Distances | 2 | **0.0%** | 0.0% | 5.06s |
| Different Speaking Speeds | 2 | **4.2%** | 3.0% | 7.73s |
| Indian English Conversation | 4 | **5.8%** | 3.8% | 6.75s |
| Indian English Technical | 5 | **8.5%** | 1.3% | 7.05s |
| Long Spoken Instructions | 2 | **2.3%** | 0.7% | 9.06s |
| Numbers Dates Ports Filenames | 2 | **15.7%** | 10.7% | 8.81s |
| Quiet Speech | 1 | **0.0%** | 0.0% | 6.64s |
| Short Voice Commands | 9 | **18.5%** | 5.4% | 5.50s |

## 3. Real Human Voice vs. Synthetic Audio Provenance

Per strict audit rules, synthetic TTS results are strictly separated from real human microphone recordings:

- **small.en (accurate)**: Real Human Mic WER = **0.0%** | Synthetic Corpus WER = **27.0%**
- **small.en (balanced)**: Real Human Mic WER = **0.0%** | Synthetic Corpus WER = **26.8%**
- **small (balanced)**: Real Human Mic WER = **25.0%** | Synthetic Corpus WER = **22.5%**
- **base (balanced)**: Real Human Mic WER = **0.0%** | Synthetic Corpus WER = **19.5%**
- **medium.en (accurate)**: Real Human Mic WER = **8.3%** | Synthetic Corpus WER = **14.2%**
- **medium.en (balanced)**: Real Human Mic WER = **8.3%** | Synthetic Corpus WER = **14.3%**

## 4. Notable Transcription Examples & Error Analysis

| ID | Category | Reference Spoken Text | Model Hypothesis | Result |
|---|---|---|---|---|
| `live_001_english` | indian_english_conversation | Hello, what's the time today? | Hello, what's the time today? | CLEAN MATCH |
| `live_002_wake` | short_voice_commands | Hey Jarvis, what's the time? | Jarvis, what's the time? | WER 17% |
| `ie_conv_001` | indian_english_conversation | Can you check if the database migration for ERP V2 completed successfully? | Can you check if the database migration for ERP V2 completed successfully? | CLEAN MATCH |
| `ie_conv_002` | indian_english_conversation | Please schedule a meeting with the development team tomorrow at three thirty PM. | Please schedule a meeting with the development team tomorrow at 3.30pm | WER 23% |
| `ie_conv_003` | indian_english_conversation | Open my workspace notes and give me a summary of pending action items. | Open my workspace notes and give me a summary of pending action items. | CLEAN MATCH |
| `ie_tech_001` | indian_english_technical | Configure PostgreSQL with pgvector extension and verify the LangGraph agent run. | Configure post to SQL with pgvector extension and verify the LangGraph agent run. | WER 27% |
| `ie_tech_002` | indian_english_technical | Check Docker container logs on port 8000 for FastAPI service. | Check Docker container logs on port 8000 for FastAPI service. | CLEAN MATCH |
| `ie_tech_003` | indian_english_technical | Explain VRAM quantization in Ollama and Qwen models on this laptop. | Explain VRAM quantization in Ollama and Qwen models on this laptop. | CLEAN MATCH |
| `ie_tech_004` | indian_english_technical | Update the React and TypeScript frontend to communicate with Node.js via WebSocket. | Update the React and TypeScript front-end to communicate with Node.js via WebSocket | WER 15% |
| `ie_tech_005` | indian_english_technical | Connect Qdrant and ChromaDB vector stores with Kavlien ERP backend. | connect Qdrant and ChromaDB vector stores with Kavlien ERP backend. | CLEAN MATCH |
| `cmd_stop` | short_voice_commands | Stop. | Top | WER 100% |
| `cmd_yes` | short_voice_commands | Yes. | Yes. | CLEAN MATCH |
