"""Speech-to-text hardware benchmark for Ryzen 5 5600H CPU.
Measures load time, RSS memory, latency, real-time factor (RTF), language detection, and Word Error Rate (WER)
against known reference transcripts across English (en-GB, en-US, en-IN), short commands, noisy/48kHz audio,
and any user-provided Malayalam / mixed-language WAV samples in benchmarks/samples/manifest.json.
Never claims WER for languages without reference audio."""
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from jarvis.config import STTSpec, load_settings  # noqa: E402
from jarvis.stt import SR, Whisper, list_local_models, wav_to_float  # noqa: E402
from jarvis.tts import PiperTTS  # noqa: E402


def rss_mb() -> float:
    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return round(pmc.WorkingSetSize / (1024 * 1024), 1)


def normalize_words(text: str) -> list[str]:
    text = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return [w for w in text.split() if w]


def word_error_rate(ref: str, hyp: str) -> float:
    r = normalize_words(ref)
    h = normalize_words(hyp)
    if not r:
        return 0.0 if not h else 1.0
    dp = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, dp[0] = dp[0], i
        for j, hw in enumerate(h, 1):
            cur = dp[j]
            dp[j] = prev if rw == hw else 1 + min(prev, dp[j], dp[j - 1])
            prev = cur
    return dp[-1] / len(r)


def synth_sapi_wavs(items: list[tuple[str, str, str]]) -> dict[str, bytes]:
    """Synthesize reference English clips via Windows built-in OneCore/SAPI voices where available."""
    out = {}
    with tempfile.TemporaryDirectory() as td:
        script = []
        for idx, (key, voice_hint, text) in enumerate(items):
            p = Path(td) / f"{idx}.wav"
            safe_txt = text.replace("'", "''")
            safe_hint = voice_hint.replace("'", "''")
            script.append(
                f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$v = $s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Name -like '*{safe_hint}*' }} | Select-Object -First 1; "
                f"if ($v) {{ $s.SelectVoice($v.VoiceInfo.Name) }}; "
                f"$s.SetOutputToWaveFile('{p}'); $s.Speak('{safe_txt}'); $s.Dispose();"
            )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Add-Type -AssemblyName System.Speech; " + " ".join(script)],
            capture_output=True, text=True, timeout=60,
        )
        for idx, (key, _, _) in enumerate(items):
            p = Path(td) / f"{idx}.wav"
            if p.exists():
                out[key] = p.read_bytes()
    return out


def resample_wav_48k(audio_f32: np.ndarray, add_noise_rms: float = 0.0) -> bytes:
    """Create a 48 kHz WAV (optionally with background noise) to benchmark resampling + VAD."""
    n48 = int(len(audio_f32) * 48000 / SR)
    a48 = np.interp(np.linspace(0, len(audio_f32) - 1, n48), np.arange(len(audio_f32)), audio_f32)
    if add_noise_rms > 0:
        rng = np.random.default_rng(42)
        a48 = np.clip(a48 + rng.normal(0.0, add_noise_rms, len(a48)), -1.0, 1.0)
    pcm = (a48 * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(48000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def build_samples() -> list[dict]:
    s = load_settings()
    tts = PiperTTS(s.tts)
    samples = []

    piper_cases = [
        ("en_tech", "en", "Explain VRAM quantization in Ollama and FastAPI on a local laptop."),
        ("en_notes", "en", "Jarvis, search my knowledge base for spaced repetition and active recall."),
        ("en_short_stop", "en", "Stop."),
        ("en_short_yes", "en", "Yes."),
    ]
    for cid, lang, text in piper_cases:
        wav_b = tts.wav_bytes(text)
        f32 = wav_to_float(wav_b)
        samples.append({"id": cid, "category": "english_piper", "expected_lang": lang, "ref": text, "audio": f32, "duration_s": len(f32) / SR})
        if cid == "en_tech":
            noisy_48k = wav_to_float(resample_wav_48k(f32, add_noise_rms=0.015))
            samples.append({"id": "en_tech_48k_noisy", "category": "english_noisy_48k", "expected_lang": lang, "ref": text, "audio": noisy_48k, "duration_s": len(noisy_48k) / SR})

    sapi_wavs = synth_sapi_wavs([
        ("en_us_zira", "Zira", "What is the current latency of the voice pipeline on this processor?"),
        ("en_us_david", "David", "Remember that the finish window should be nine hundred milliseconds."),
    ])
    for cid, ref in [
        ("en_us_zira", "What is the current latency of the voice pipeline on this processor?"),
        ("en_us_david", "Remember that the finish window should be nine hundred milliseconds."),
    ]:
        if cid in sapi_wavs:
            f32 = wav_to_float(sapi_wavs[cid])
            samples.append({"id": cid, "category": "english_sapi", "expected_lang": "en", "ref": ref, "audio": f32, "duration_s": len(f32) / SR})

    manifest = ROOT / "benchmarks" / "samples" / "manifest.json"
    if manifest.exists():
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        for item in entries:
            wav_path = manifest.parent / item["file"]
            if wav_path.exists():
                f32 = wav_to_float(wav_path.read_bytes())
                samples.append({
                    "id": item["id"],
                    "category": item.get("category", "custom"),
                    "expected_lang": item.get("language", "auto"),
                    "ref": item.get("reference", ""),
                    "audio": f32,
                    "duration_s": len(f32) / SR,
                })
    return samples


def run_benchmark() -> dict:
    available = list_local_models()
    samples = build_samples()
    print(f"Installed local Whisper models: {available}")
    print(f"Loaded {len(samples)} reference audio samples (categories: {sorted({s['category'] for s in samples})})")

    configs = []
    for model in [m for m in ("base", "small", "small.en") if m in available]:
        for quality, threads in (("fast", 6), ("balanced", 6), ("accurate", 6), ("balanced", 4)):
            if model == "small.en" and quality != "balanced":
                continue
            if threads == 4 and quality != "balanced":
                continue
            configs.append({"model": model, "quality": quality, "cpu_threads": threads})

    results = []
    for cfg in configs:
        spec = STTSpec(
            model=cfg["model"],
            compute_type="int8",
            cpu_threads=cfg["cpu_threads"],
            language="auto",
            quality=cfg["quality"],
            vocab_hints="Jarvis, Ollama, FastAPI, VRAM, quantization, Silero",
        )
        rss_before = rss_mb()
        w = Whisper(spec)
        t_load = time.time()
        w.load()
        load_s = round(time.time() - t_load, 2)
        rss_after = rss_mb()

        case_rows = []
        for s in samples:
            r = w.transcribe(s["audio"], save_audio=False)
            wer = word_error_rate(s["ref"], r["text"]) if s["ref"] else None
            lang_hit = (r["detected_language"] == s["expected_lang"]) if s["expected_lang"] != "auto" else None
            case_rows.append({
                "id": s["id"],
                "category": s["category"],
                "duration_s": round(s["duration_s"], 2),
                "seconds": r["seconds"],
                "rtf": round(r["seconds"] / max(0.1, s["duration_s"]), 3),
                "detected_language": r["detected_language"],
                "language_probability": r["language_probability"],
                "lang_hit": lang_hit,
                "confidence": r["confidence"],
                "wer": round(wer, 4) if wer is not None else None,
                "ref": s["ref"],
                "hyp": r["text"],
            })

        wers = [c["wer"] for c in case_rows if c["wer"] is not None]
        rtfs = [c["rtf"] for c in case_rows]
        secs = [c["seconds"] for c in case_rows]
        summary = {
            **cfg,
            "load_s": load_s,
            "rss_delta_mb": round(max(0.0, rss_after - rss_before), 1),
            "mean_wer": round(sum(wers) / len(wers), 4) if wers else None,
            "mean_seconds": round(sum(secs) / len(secs), 2),
            "mean_rtf": round(sum(rtfs) / len(rtfs), 3),
            "cases": case_rows,
        }
        results.append(summary)
        print(
            f"[{cfg['model']:8} | {cfg['quality']:8} | thr={cfg['cpu_threads']}] "
            f"load={load_s:.2f}s rss+={summary['rss_delta_mb']:.0f}MB "
            f"mean_time={summary['mean_seconds']:.2f}s rtf={summary['mean_rtf']:.2f}x "
            f"mean_WER={summary['mean_wer'] * 100:.1f}%"
        )

    out_path = ROOT / "benchmarks" / "stt_benchmark_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "machine": "Ryzen 5 5600H / 16GB RAM / Windows / CPU int8",
        "models_tested": available,
        "has_custom_malayalam_samples": any(s["category"] in ("malayalam", "mixed") for s in samples),
        "results": results,
    }
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved benchmark report to {out_path}")
    return report


if __name__ == "__main__":
    run_benchmark()

