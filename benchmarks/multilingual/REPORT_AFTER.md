# Multilingual ASR Benchmark Report

- **Machine**: Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8
- **Manifest**: `benchmarks\multilingual\manifest.json` (15 WAVs loaded)
- **Verified Reference Counts by Language**: `{'en': 15}`

## Model & Preset Summary

| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate | Script Mismatch | Mismatch Rate | Empty | Hallucinations |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | balanced | 1.34 | 909.2 | 2.10 | 1.98x | 100.0% | 12.9% | 3.5% | 10.8% | 0.0% | 2.1% | 0 | 0.0% | 0 | 0 |
| small | balanced | 2.57 | 909.2 | 3.55 | 1.88x | 100.0% | 10.7% | 3.7% | 10.1% | 0.0% | 0.6% | 0 | 0.0% | 0 | 0 |

## Per-Language Breakdown

> WER/CER are computed strictly on samples with `reference_verified: true`; unverified or missing references report `N/A`.

| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER | Script Mismatch | Mismatch Rate | Empty |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base | balanced | `en` | 15 | 15 | 100.0% | 12.9% | 3.5% | 0 | 0.0% | 0 |
| small | balanced | `en` | 15 | 15 | 100.0% | 10.7% | 3.7% | 0 | 0.0% | 0 |
