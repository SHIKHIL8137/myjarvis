# Multilingual ASR Benchmark Report

- **Machine**: Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8
- **Manifest**: `D:\projects\myjarvis\benchmarks\multilingual\manifest.json` (15 WAVs loaded)
- **Verified Reference Counts by Language**: `{'en': 15}`

## Model & Preset Summary

| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | fast | 1.50 | 908.8 | 2.50 | 2.36x | 100.0% | 10.0% | 3.3% | 9.4% | 0.0% | 0.6% |
| base | balanced | 0.99 | 925.3 | 2.31 | 2.18x | 100.0% | 12.9% | 3.5% | 10.8% | 0.0% | 2.1% |
| small | fast | 2.91 | 925.3 | 3.85 | 2.07x | 100.0% | 10.0% | 3.3% | 9.4% | 0.0% | 0.6% |
| small | balanced | 2.80 | 925.3 | 3.94 | 2.08x | 100.0% | 10.7% | 3.7% | 10.1% | 0.0% | 0.6% |
| small.en | fast | 1.45 | 925.3 | 2.41 | 1.33x | 100.0% | 13.0% | 3.7% | 10.6% | 0.9% | 1.5% |
| small.en | balanced | 1.54 | 925.3 | 2.47 | 1.52x | 100.0% | 11.8% | 3.5% | 10.4% | 0.0% | 1.5% |

## Per-Language Breakdown

| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER |
|---|---|---|---:|---:|---:|---:|---:|
| base | fast | `en` | 15 | 15 | 100.0% | 10.0% | 3.3% |
| base | balanced | `en` | 15 | 15 | 100.0% | 12.9% | 3.5% |
| small | fast | `en` | 15 | 15 | 100.0% | 10.0% | 3.3% |
| small | balanced | `en` | 15 | 15 | 100.0% | 10.7% | 3.7% |
| small.en | fast | `en` | 15 | 15 | 100.0% | 13.0% | 3.7% |
| small.en | balanced | `en` | 15 | 15 | 100.0% | 11.8% | 3.5% |
