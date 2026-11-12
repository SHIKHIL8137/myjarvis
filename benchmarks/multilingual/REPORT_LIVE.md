# Multilingual ASR Benchmark Report

- **Machine**: Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8
- **Manifest**: `benchmarks\multilingual\manifest_live.json` (4 WAVs loaded)
- **Verified Reference Counts by Language**: `{}`

## Model & Preset Summary

| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate | Script Mismatch | Empty | Hallucinations |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| small | balanced | 3.97 | 778.9 | 2.77 | 0.56x | 100.0% | N/A | N/A | N/A | N/A | N/A | 1 | 1 | 0 |

## Per-Language Breakdown

| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER | Script Mismatch | Empty |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| small | balanced | `en` | 2 | 0 | 100.0% | N/A | N/A | 0 | 0 |
| small | balanced | `mixed` | 1 | 0 | 100.0% | N/A | N/A | 0 | 1 |
| small | balanced | `ml` | 1 | 0 | 100.0% | N/A | N/A | 1 | 0 |
