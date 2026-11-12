# Multilingual Speech Benchmark Dataset

This directory implements the multilingual STT benchmark suite across English, Malayalam, Mixed Malayalam-English, Hindi, Tamil, Noisy Speech, Short Commands, and Technical Vocabulary.

## Manifest Schema (`manifest.json`)
```json
{
  "samples": [
    {
      "id": "ml_001",
      "audio": "samples/ml_001.wav",
      "language": "ml",
      "reference": "Manually verified Malayalam transcript",
      "category": "normal_speech",
      "source": "human_recording",
      "reference_verified": true
    }
  ]
}
```

### Dataset Rules
- Synthetic English/Technical/Noisy/Short-Command WAVs generated from known TTS prompt strings may be marked `"reference_verified": true` because the exact input text is known (`python benchmarks/multilingual/prepare_corpus.py`).
- Human-verified Malayalam, Hindi, Tamil, and Mixed Malayalam-English references **must never be fabricated**. Place real 8–48 kHz mono/stereo WAV recordings into `samples/` and record exact human-verified ground truth in `manifest.json`.
- Target collection goals: English (30), Malayalam (30), Mixed Malayalam-English (30), Hindi (15), Tamil (15), Noisy (15), Technical Vocabulary (15).

## Running
```powershell
.\.venv\Scripts\python.exe benchmarks\multilingual\prepare_corpus.py
.\.venv\Scripts\python.exe benchmarks\multilingual\runner.py --models base small small.en --qualities fast balanced accurate
```

