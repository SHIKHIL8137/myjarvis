# Multilingual ASR Benchmark Report

- **Machine**: Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8
- **Manifest**: `benchmarks\multilingual\manifest_live.json` (4 WAVs loaded)
- **Verified Reference Counts by Language**: `{}`

## Model & Preset Summary

| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate | Script Mismatch | Mismatch Rate | Empty | Hallucinations |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | balanced | 1.31 | 907.4 | 2.67 | 0.59x | 100.0% | N/A | N/A | N/A | N/A | N/A | 0 | 0.0% | 1 | 0 |
| small | balanced | 2.57 | 907.4 | 2.49 | 0.51x | 100.0% | N/A | N/A | N/A | N/A | N/A | 1 | 25.0% | 1 | 0 |

## Per-Language Breakdown

> WER/CER are computed strictly on samples with `reference_verified: true`; unverified or missing references report `N/A`.

| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER | Script Mismatch | Mismatch Rate | Empty |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base | balanced | `en` | 2 | 0 | 100.0% | N/A | N/A | 0 | 0.0% | 0 |
| base | balanced | `mixed` | 1 | 0 | 100.0% | N/A | N/A | 0 | 0.0% | 1 |
| base | balanced | `ml` | 1 | 0 | 100.0% | N/A | N/A | 0 | 0.0% | 0 |
| small | balanced | `en` | 2 | 0 | 100.0% | N/A | N/A | 0 | 0.0% | 0 |
| small | balanced | `mixed` | 1 | 0 | 100.0% | N/A | N/A | 0 | 0.0% | 1 |
| small | balanced | `ml` | 1 | 0 | 100.0% | N/A | N/A | 1 | 100.0% | 0 |
