"""Generate local deterministic synthetic English reference WAVs (Piper + Windows SAPI)
to seed benchmarks/multilingual/manifest.json without fabricating human-verified Indic transcripts.
Users can add human-verified WAV recordings for Malayalam, mixed, Hindi, and Tamil to manifest.json."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_settings  # noqa: E402
from jarvis.stt import SR, resample_audio, wav_to_float  # noqa: E402
from jarvis.tts import PiperTTS  # noqa: E402

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def _encode_wav_pcm16(audio_f32: np.ndarray, sr: int, channels: int = 1) -> bytes:
    a = np.clip(np.asarray(audio_f32, dtype=np.float32), -1.0, 1.0)
    if channels == 2 and a.ndim == 1:
        a = np.stack([a, a], axis=1)
    pcm = (a * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth_sapi(items: list[tuple[str, str, str]]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as td:
        cmds = []
        for idx, (_sid, hint, txt) in enumerate(items):
            p = Path(td) / f"{idx}.wav"
            safe_txt = txt.replace("'", "''")
            safe_hint = hint.replace("'", "''")
            cmds.append(
                f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$v = $s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Name -like '*{safe_hint}*' }} | Select-Object -First 1; "
                f"if ($v) {{ $s.SelectVoice($v.VoiceInfo.Name) }}; "
                f"$s.SetOutputToWaveFile('{p}'); $s.Speak('{safe_txt}'); $s.Dispose();"
            )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Add-Type -AssemblyName System.Speech; " + " ".join(cmds)],
            capture_output=True, text=True, timeout=90,
        )
        for idx, (sid, _hint, _txt) in enumerate(items):
            p = Path(td) / f"{idx}.wav"
            if p.exists():
                out[sid] = p.read_bytes()
    return out


def build_english_corpus(force: bool = False) -> dict:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    tts = PiperTTS(s.tts)

    existing = {"targets": {"en": 30, "ml": 30, "mixed": 30, "hi": 15, "ta": 15, "noisy": 15, "technical": 15}, "samples": []}
    if MANIFEST_PATH.exists():
        existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in existing.get("samples", []) if isinstance(x, dict) and "id" in x}

    piper_specs = [
        ("en_001", "normal_speech", "en", "What is the weather forecast for this afternoon?"),
        ("en_002", "normal_speech", "en", "Open my project notes and summarize the main tasks."),
        ("en_003", "normal_speech", "en", "Do not delete the database backup file before verification."),
        ("en_004", "normal_speech", "en", "Schedule a reminder for tomorrow morning at nine thirty."),
        ("tech_001", "technical", "en", "Jarvis, check the FastAPI and PostgreSQL status in Ollama."),
        ("tech_002", "technical", "en", "Update the React and TypeScript frontend to connect with Node.js."),
        ("tech_003", "technical", "en", "Verify Qwen quantization and MongoDB connection pooling."),
        ("cmd_001", "short_command", "en", "Stop."),
        ("cmd_002", "short_command", "en", "Yes."),
        ("cmd_003", "short_command", "en", "Pause."),
        ("cmd_004", "short_command", "en", "Resume listening."),
    ]
    for sid, cat, lang, text in piper_specs:
        rel = f"samples/{sid}.wav"
        dest = SAMPLES_DIR / f"{sid}.wav"
        if force or not dest.exists():
            f32 = wav_to_float(tts.wav_bytes(text))
            dest.write_bytes(_encode_wav_pcm16(f32, SR, 1))
        by_id[sid] = {
            "id": sid,
            "audio": rel,
            "language": lang,
            "reference": text,
            "category": cat,
            "source": "synthetic_piper",
            "reference_verified": True,
        }

    # Add 48 kHz noisy + 44.1 kHz stereo variants from real synthesized audio (not fabricated text)
    base_tech = wav_to_float((SAMPLES_DIR / "tech_001.wav").read_bytes())
    rng = np.random.default_rng(7)
    a48 = resample_audio(base_tech, SR, 48000, remove_dc=False)
    a48_noisy = np.clip(a48 + rng.normal(0.0, 0.015, len(a48)), -1.0, 1.0)
    (SAMPLES_DIR / "noisy_001_48k.wav").write_bytes(_encode_wav_pcm16(a48_noisy, 48000, 1))
    by_id["noisy_001_48k"] = {
        "id": "noisy_001_48k",
        "audio": "samples/noisy_001_48k.wav",
        "language": "en",
        "reference": "Jarvis, check the FastAPI and PostgreSQL status in Ollama.",
        "category": "noisy",
        "source": "synthetic_piper_48k_noisy",
        "reference_verified": True,
    }

    a44_stereo = resample_audio(wav_to_float((SAMPLES_DIR / "en_003.wav").read_bytes()), SR, 44100, remove_dc=False)
    (SAMPLES_DIR / "en_005_44k_stereo.wav").write_bytes(_encode_wav_pcm16(a44_stereo, 44100, 2))
    by_id["en_005_44k_stereo"] = {
        "id": "en_005_44k_stereo",
        "audio": "samples/en_005_44k_stereo.wav",
        "language": "en",
        "reference": "Do not delete the database backup file before verification.",
        "category": "normal_speech",
        "source": "synthetic_piper_44k_stereo",
        "reference_verified": True,
    }

    # Windows SAPI multi-speaker samples when available
    sapi_items = [
        ("en_sapi_zira", "Zira", "Explain Qwen local inference and latency on six threads."),
        ("en_sapi_david", "David", "Do not skip the regression tests before deployment."),
    ]
    sapi_wavs = _synth_sapi(sapi_items)
    for sid, _hint, text in sapi_items:
        if sid in sapi_wavs:
            (SAMPLES_DIR / f"{sid}.wav").write_bytes(sapi_wavs[sid])
            by_id[sid] = {
                "id": sid,
                "audio": f"samples/{sid}.wav",
                "language": "en",
                "reference": text,
                "category": "normal_speech",
                "source": "synthetic_windows_sapi",
                "reference_verified": True,
            }

    manifest = {
        "schema_version": 1,
        "description": (
            "Multilingual STT benchmark manifest. Only synthetic exact-prompt English/Technical/Noisy/Command WAVs "
            "or explicitly user-verified recordings may carry reference_verified=true. Never fabricate human-verified "
            "Malayalam, Hindi, Tamil, or Mixed transcripts."
        ),
        "targets": {
            "en": 30,
            "ml": 30,
            "mixed": 30,
            "hi": 15,
            "ta": 15,
            "noisy": 15,
            "technical": 15,
        },
        "samples": sorted(by_id.values(), key=lambda x: x["id"]),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    m = build_english_corpus(force=args.force)
    print(f"Prepared {len(m['samples'])} manifest entries at {MANIFEST_PATH}")

