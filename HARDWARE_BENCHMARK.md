# Hardware benchmark

Machine: Ryzen 5 5600H (6 cores / 12 threads), GTX 1650 4 GB, 15.3 GB RAM, Windows 11, Ollama 0.40.16, Python 3.14.6.
Measured 2026-11-11. Mostly single runs: read these as indicative, not statistical.

## Latency of one spoken turn (from `preflight.py`)

| Stage | Time |
|---|---|
| Hear: Whisper small.en on CPU, about 5 s of speech | 1.7–2.4 s |
| Think: full chat turn answered from notes | 6.5–8.6 s in preflight; 3–6 s in a warm conversation |
| Speak: Piper, one sentence, uncached | 0.2–0.8 s |

Speech starts after the first complete sentences arrive, not after the whole answer.

## Language model

| | qwen3:4b-instruct (default) | qwen3:4b (thinking) |
|---|---|---|
| Load time | 3.4 s | 6.0 s |
| Generation speed | 23–28 tokens/s | 24–27 tokens/s |
| Time to first word, small talk, warm | 0.4–0.8 s | — |
| Time to first word, with notes, warm | 1.0–3.6 s (mean 2.2 s over 10 turns) | — |
| A simple greeting, end to end | about 2 s | 29.5 s (544 hidden reasoning tokens) |
| Reasoning visible to the user | No | No (discarded by Jarvis) |
| In VRAM | 2.18 of 3.29 GB (66%) | the same |

The model does not fit entirely on the 4 GB card, so a third of it runs on the CPU. That limits prompt reading to roughly
250 tokens/s, which is why prompt size dominates the wait.

## Tuning that was measured

| Change | Effect on time to first word |
|---|---|
| Context 262k (model default) → 4096 | First reply 83 s → about 8 s |
| Notes per turn: 4 × 700 chars → 3 × 450 chars | 2.38 s → 1.22 s |
| History: sliding window → grow-then-halve window, 5000 → 2000 chars | mean 3.50 s → 2.24 s over a 10-turn conversation |

## Speech

| | Result |
|---|---|
| Whisper small.en load and warm-up | 1.3–2.0 s |
| Whisper transcription | 1.7 s for 5.5 s of audio |
| Piper load | 3.2 s |
| Piper synthesis | 1.85 s for 5.5 s of audio on first use; 0.2–0.8 s per sentence afterwards |
| Stop speaking | 0.19 s |
| Wake word | 1.7–2.7 ms per 80 ms frame, about 2–3% of one core while listening |

## Memory with everything loaded

| | Usage |
|---|---|
| Jarvis server (Whisper, Piper, wake word in RAM) | about 750 MB |
| Ollama model in system RAM | about 1.5 GB |
| VRAM in use | 2.9 of 4.0 GB |
| Free system RAM during tests | 1.4–6.5 GB, depending on other open apps |

Free RAM was the tightest resource. With under 2 GB free the first Phase 0 reply took 83 s partly for that reason.

## Retrieval

Keyword search over 30 notes takes well under a millisecond and returned the expected note first in every test query.
Embeddings were not added: there was no measured miss to fix, and a second model would cost RAM this machine does not have spare.
Known weakness: a loosely related note is sometimes attached to a question that needs none.
