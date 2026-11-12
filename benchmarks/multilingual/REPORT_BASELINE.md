# Multilingual ASR Benchmark Report

- **Machine**: Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8
- **Manifest**: `D:\projects\myjarvis\benchmarks\multilingual\manifest.json` (15 WAVs loaded)
- **Verified Reference Counts by Language**: `{'en': 15}`

## Model & Preset Summary

| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | fast | 1.48 | 403.7 | 1.32 | 0.69x | 100.0% | 20.4% | 5.2% | 16.8% | 0.0% | 3.6% |
| base | balanced | 1.16 | 446.7 | 1.36 | 0.72x | 100.0% | 13.1% | 3.4% | 9.6% | 0.0% | 3.6% |
| base | accurate | 0.99 | 446.7 | 1.36 | 0.71x | 100.0% | 13.8% | 3.6% | 10.2% | 0.0% | 3.6% |
| small | fast | 2.92 | 818.7 | 4.02 | 2.10x | 100.0% | 21.0% | 5.3% | 16.8% | 0.0% | 4.2% |
| small | balanced | 2.93 | 825.3 | 3.75 | 2.00x | 100.0% | 20.3% | 5.0% | 16.1% | 0.0% | 4.2% |
| small | accurate | 2.81 | 825.6 | 3.72 | 1.96x | 100.0% | 21.0% | 5.1% | 16.8% | 0.0% | 4.2% |
| small.en | fast | 1.52 | 826.0 | 1.79 | 0.94x | 100.0% | 20.7% | 5.0% | 17.1% | 0.0% | 3.6% |
| small.en | balanced | 1.25 | 826.0 | 1.85 | 0.96x | 100.0% | 19.9% | 4.8% | 16.4% | 0.0% | 3.6% |
| small.en | accurate | 1.14 | 826.0 | 1.99 | 1.10x | 100.0% | 19.9% | 4.8% | 16.4% | 0.0% | 3.6% |

## Per-Language Breakdown

| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER |
|---|---|---|---:|---:|---:|---:|---:|
| base | fast | `en` | 15 | 15 | 100.0% | 20.4% | 5.2% |
| base | balanced | `en` | 15 | 15 | 100.0% | 13.1% | 3.4% |
| base | accurate | `en` | 15 | 15 | 100.0% | 13.8% | 3.6% |
| small | fast | `en` | 15 | 15 | 100.0% | 21.0% | 5.3% |
| small | balanced | `en` | 15 | 15 | 100.0% | 20.3% | 5.0% |
| small | accurate | `en` | 15 | 15 | 100.0% | 21.0% | 5.1% |
| small.en | fast | `en` | 15 | 15 | 100.0% | 20.7% | 5.0% |
| small.en | balanced | `en` | 15 | 15 | 100.0% | 19.9% | 4.8% |
| small.en | accurate | `en` | 15 | 15 | 100.0% | 19.9% | 4.8% |
