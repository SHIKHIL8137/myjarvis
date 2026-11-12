# Multilingual Speech Benchmark Dataset & Malayalam Verification Plan

This directory implements the multilingual STT benchmark suite across English, Malayalam, Mixed Malayalam-English, Hindi, Tamil, Noisy Speech, Short Commands, and Technical Vocabulary.

## Separation of Datasets

- **Synthetic Verified English Suite (`manifest.json`)**: Contains synthetic TTS WAV recordings (`en`, `technical`, `noisy`, `short`) whose exact spoken prompt string is known (`"reference_verified": true`). Generated deterministically via `python benchmarks/multilingual/prepare_corpus.py`.
- **Real Mic & Human-Verified Evaluation Suite (`manifest_live.json` or custom local manifest)**: Contains real microphone WAV recordings captured via Jarvis Speech Inspector (opt-in `Approve for Local Eval`) or manually curated human recordings. Must **never** be merged into synthetic English averages, and WER/CER must never be computed unless `"reference_verified": true`.

## Procedure for Adding Manually Verified Malayalam & Mixed (`ml` / `mixed`) Samples

1. **Collect real audio**: Record natural spoken Malayalam (`ml`) and code-switched Malayalam-English (`mixed`, e.g., `"Server log check ചെയ്യൂ port 8080ൽ"`) as 16 kHz mono PCM WAV files (or copy existing recorded `.wav` files into `benchmarks/multilingual/samples_verified/`).
2. **Transcribe manually (never synthetic TTS guesswork)**: Listen to each clip and write the exact verbatim reference text in Malayalam Unicode (`U+0D00–U+0D7F`) + English technical words as spoken.
3. **Add entries to `manifest_live.json`** (or a dedicated `manifest_ml_verified.json`) following this schema:

```json
{
  "dataset_version": "1.0",
  "samples": [
    {
      "id": "ml_human_001",
      "audio_path": "samples_verified/ml_human_001.wav",
      "language": "ml",
      "requested_language": "ml",
      "category": "general",
      "source": "human_mic",
      "reference": "ഇന്ന് കാലാവസ്ഥ എങ്ങനെയാണ്",
      "reference_verified": true
    },
    {
      "id": "mixed_human_001",
      "audio_path": "samples_verified/mixed_human_001.wav",
      "language": "mixed",
      "requested_language": "ml",
      "category": "code_switched",
      "source": "human_mic",
      "reference": "Server restart ചെയ്യൂ port 8080ൽ",
      "reference_verified": true
    }
  ]
}
```

4. **Strictly guard unverified clips**: If `"reference"` is a rough note or model transcript not yet human-checked, set `"reference_verified": false` so the runner skips WER/CER (`N/A`) while still measuring `script_mismatch_rate`, `empty_count`, `hallucination_or_loop_count`, latency, RTF, and peak RSS.

## Metrics Reported Separately

The benchmark runner (`benchmarks/multilingual/runner.py`) computes per-language summaries (`by_language["ml"]`, `by_language["mixed"]`, `by_language["en"]`, etc.):
- **Malayalam WER / CER** (`mean_wer`, `mean_cer` on verified `ml` samples; `N/A` if 0 verified samples)
- **Mixed Malayalam-English WER / CER** (`mean_wer`, `mean_cer` on verified `mixed` samples; `N/A` if 0 verified samples)
- **`script_mismatch_rate`** & `script_mismatch_count`
- **`empty_count`** & **`hallucination_or_loop_count`**
- **Latency (`mean_seconds`), `mean_rtf`, and `peak_rss_mb`**

## Dataset Rules
- **Never** invent, machine-translate, or copy ASR outputs as verified Malayalam ground truth.
- **Never** merge unverified microphone clips into verified accuracy tables.

## Running the Benchmarks

```powershell
# 1. Synthetic verified English benchmark:
.\.venv\Scripts\python.exe -m benchmarks.multilingual.runner --manifest benchmarks/multilingual/manifest.json --models base small --qualities balanced --output-json benchmarks/multilingual/results_after.json --output-md benchmarks/multilingual/REPORT_AFTER.md

# 2. Separate real mic / Malayalam human-verified benchmark:
.\.venv\Scripts\python.exe -m benchmarks.multilingual.runner --manifest benchmarks/multilingual/manifest_live.json --models base small --qualities balanced --output-json benchmarks/multilingual/results_live.json --output-md benchmarks/multilingual/REPORT_LIVE.md
```
