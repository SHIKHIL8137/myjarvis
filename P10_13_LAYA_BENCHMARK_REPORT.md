# P10.13 Phase 2 & 7 — Experimental Laya Benchmark & Compatibility Report

Measured directly on the target Windows 11 host (`AMD Ryzen 5 5600H`, `NVIDIA GeForce GTX 1650 4 GB VRAM`, `15.3 GB RAM`, `Python 3.14.6`) inside an isolated virtual environment (`data/laya_iso_venv`, leaving core `.venv` untouched).

---

## 1. Compatibility & Upstream Research Findings (`laya 0.4.31`)

- **Upstream Repository**: [`NandhaKishorM/laya`](https://github.com/NandhaKishorM/laya) (inspected commit `3a38cbe83f6ef556f3125fb0065fdc5a55d05792`, `2026-11-11`, **Apache-2.0** license).
- **PyPI Package & Dependencies**: `laya 0.4.31` (`requires-python >= 3.10`) resolved cleanly with `pip install "laya[serve,onnx]"` inside `data/laya_iso_venv` alongside `torch 2.15.0+cpu`, `transformers 5.22.2`, `vegaml 0.16.1`, `safetensors 0.8.0`, `huggingface_hub 2.4.0`, `onnx 1.23.2`, `onnxruntime 1.31.1`, `fastapi 0.162.4`.
- **CUDA Note on Python `3.14.6` Windows**: Standard PyPI resolved `torch 2.15.0+cpu` (`torch.cuda.is_available() == False`), so CPU inference was measured directly without touching CUDA driver installs or stealing VRAM from Ollama (`qwen3:4b`) / `faster-whisper`.
- **Upstream Checkpoint Sizes (`HfApi().model_info`)**:
  - `convaiinnovations/laya` (English ModernBERT-large 421M bundle): `2,262.7 MB` (`39` files)
  - `convaiinnovations/laya-multilingual` (mmBERT-base 322M): `646.8 MB` (`8` files) -> **benchmarked end-to-end here**
  - `nandakishorm/vega-U` (Qwen3.5-0.8B reader with conformal sets): `1,741.0 MB` (`32` files)
- **Measured Footprint (`convaiinnovations/laya-multilingual`, CPU)**:
  - Initial download + model load: `131.35 s` (`646.8 MB` cached locally in `data/laya_hf_cache`)
  - Warm disk load startup time: **`8.58 s` – `10.68 s`** (`startup_ms = 8576.02` / `10680.68`)
  - Worker process RSS (`laya_service.py` via Windows `GetProcessMemoryInfo`): **`42.9 MB` baseline -> `1,652.4 MB` after load -> `1,696.6 MB` after inference** (~1.65 GB RAM).
  - Single-question (`1` choice) warm CPU latency: **`89.4 ms`** (`271.9 ms` first call)
  - Full Jarvis 6-question schema (`department`, `intent` [13 classes], `complexity`, `risk_tier`, `needs_live_web`, `needs_project_context`): **`1,638 ms` – `1,696 ms` P50**, **`1,996 ms` – `2,421 ms` P95**.

---

## 2. Versioned Evaluation Dataset (`data/decision/datasets/v1`)

| Split | Samples | Languages & Coverage | Leakage Verification |
|---|---|---|---|
| `train.jsonl` | `24` | `en`, `ml`, `manglish`; trivial, complex dev, docs, reminders, web, memory, ambiguous, negated, sensitive | Strictly disjoint |
| `val.jsonl` | `16` | Used exclusively for threshold calibration checks | Strictly disjoint |
| `test.jsonl` | **`36`** | Held out (`24` `en`, `6` `ml` Malayalam script, `6` `manglish` code-mixed); zero overlap with train/val | **`leakage_free = True`** |

---

## 3. Held-Out Comparative Benchmark Results (`test.jsonl`, `N = 36`)

Live runs measured against [`data/decision/laya_3way_benchmark.json`](file:///d:/projects/myjarvis/data/decision/laya_3way_benchmark.json) and [`data/decision/laya_calibrated_benchmark.json`](file:///d:/projects/myjarvis/data/decision/laya_calibrated_benchmark.json):

| Classifier | Dept Acc | Dept Macro-F1 | Intent Acc | Intent Macro-F1 | Complexity Acc | Risk Tier Acc | Abstain Recall / False Pos | Sec Policy Enforcement | Latency P50 / P95 |
|---|---|---|---|---|---|---|---|---|---|
| **Existing Heuristic** | **0.6111** | **0.6391** | **0.5556** | 0.3996 | 0.5000 | **0.8333** | 0.25 / 0 | **1.0000** | **0.034 ms / 0.055 ms** |
| **Live Fast LLM (`qwen3:4b-instruct` via Ollama)** | 0.3333 | 0.3257 | 0.5278 | **0.5318** | **0.5556** | 0.6389 | 0.00 / 0 | **1.0000** | 5,401 ms / 9,599 ms |
| **Isolated Laya-Multilingual (raw threshold `0.70`)** | 0.3056 | 0.3044 | 0.3611 | 0.4542 | 0.2222 | 0.5556 | **0.50** / 18 | **1.0000** | 1,696 ms / 2,233 ms |
| **Isolated Laya-Multilingual (calibrated threshold `0.35`)** | 0.3056 | 0.3044 | 0.4444 | 0.4885 | 0.2222 | 0.5556 | 0.25 / **0** | **1.0000** | 1,693 ms / 2,421 ms |

### Per-Language Held-Out Breakdown (`en` = 24, `ml` = 6, `manglish` = 6)

| Classifier | `en` Dept / Intent Acc | `ml` (Malayalam) Dept / Intent Acc | `manglish` (Code-Mixed) Dept / Intent Acc |
|---|---|---|---|
| **Existing Heuristic** | **0.7083 / 0.6250** (`0.034 ms`) | **0.5000 / 0.5000** (`0.043 ms`) | 0.3333 / 0.3333 (`0.031 ms`) |
| **Live Fast LLM (`qwen3:4b-instruct`)** | 0.3750 / 0.5417 (`5,427 ms`) | 0.1667 / 0.3333 | 0.3333 / **0.6667** (`5,141 ms`) |
| **Laya-Multilingual (`threshold=0.70`)** | 0.4167 / 0.4167 (`1,683 ms`) | 0.0000 / 0.0000 (`1,791 ms`) | 0.1667 / 0.5000 (`1,635 ms`) |
| **Laya-Multilingual (`calibrated 0.35`)** | 0.4167 / 0.4583 (`1,659 ms`) | 0.0000 / 0.1667 (`1,774 ms`) | 0.1667 / **0.6667** (`1,675 ms`) |

### Observed Confusion Highlights (`Department`, Calibrated Laya & Heuristic)
- **Heuristic Confusion**: `chat -> chat: 4`, `dev -> dev: 6, chat: 4`, `assistant -> assistant: 7, chat: 8`, `system -> system: 5, chat: 1, dev: 1` (misses domain phrasing lacking English regex matches, defaulting to `general_chat`).
- **Calibrated Laya Confusion**: `chat -> chat: 1, dev: 3`, `dev -> dev: 6, system: 3, assistant: 1`, `assistant -> assistant: 1, dev: 7, system: 7`, `system -> system: 3, dev: 4` (zero-shot encoder without fitted `vega` heads tends to over-predict `dev` / `system` across Jarvis-specific department labels).
- **Sensitivity & Security Enforcement**: Across **100%** (`1.0000`) of sensitive/dangerous prompts in `test.jsonl`, `enforce_security_boundary` guaranteed `EXECUTE` risk tier + `requires_owner_approval=True` regardless of upstream classifier accuracy.

---

## 4. Empirical Recommendation & Rollout Posture

1. **Keep `laya.enabled = false` (`disabled`) by default** in core Jarvis until fine-tuned/calibrated on Jarvis-specific labels (`vega-U` head fitting or reduced question schema) and/or accelerated via CUDA PyTorch or ONNX INT8:
   - Unspecialized zero-shot `laya-multilingual` adds `~1.69 GB` CPU RSS and `~1.7 s` P50 latency for the 6-question schema while trailing `Heuristic` on overall department accuracy (`0.3056` vs `0.6111`).
2. **Where Calibrated Laya Shines**:
   - On **Malayalam-English code-mixed (`manglish`) intent classification**, calibrated Laya reached **`0.6667`** accuracy — **2x higher than Heuristic (`0.3333`)** and **matching `qwen3:4b-instruct` (`0.6667`) at 3x lower latency (`1.67 s` vs `5.14 s`)**, with **`0` false abstentions** vs `18` uncalibrated false abstentions.
3. **Production Safe Policy**:
   - Default to `disabled` in `config.json`; allow opt-in **`shadow`** telemetry mode first, **`laya_with_heuristic_fallback`** only after owner calibration approval, and preserve hard Layer 1 rules + Layer 3 security boundaries so zero privileged operations can ever bypass owner confirmation.

