"""Interactive real-microphone verification script for Steps 9, 10, 11, and 12:
Records actual human speech for English, Malayalam, Mixed Malayalam-English, and optional Wake Word ("Hey Jarvis"),
runs Whisper base & small, tests translation & transcript correction (verifying original_transcript preservation),
and saves WAVs + manifest + results to benchmarks/samples/ and benchmarks/live_mic_results.json."""
import asyncio
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.brain import Brain  # noqa: E402
from jarvis.config import STTSpec, TranslationSpec, load_settings  # noqa: E402
from jarvis.stt import SR, Whisper, float_to_wav, resample_audio  # noqa: E402
from jarvis.translator import Translator  # noqa: E402
from jarvis.wakeword import FRAME, WakeWord  # noqa: E402


def record_utterance(prompt: str, max_s: float = 10.0, silence_s: float = 1.1) -> np.ndarray:
    input(f"\n>>> {prompt}\nPress ENTER when ready, then speak clearly...")
    print("Recording... (stop speaking for ~1s to finish)")
    dev = sd.query_devices(kind="input")
    cap_sr = int(dev.get("default_samplerate") or SR)
    block = int(round(FRAME * cap_sr / SR))
    frames = []
    speech_seen = False
    last_voice = time.time()
    t0 = time.time()
    noise = 0.008

    with sd.InputStream(samplerate=cap_sr, channels=1, dtype="float32", blocksize=block) as stream:
        while time.time() - t0 < max_s:
            chunk, _ = stream.read(block)
            mono = chunk[:, 0]
            if cap_sr != SR:
                mono = resample_audio(mono, cap_sr, SR, remove_dc=False)
            frames.append(mono.copy())
            rms = float(np.sqrt(np.mean(mono ** 2)))
            now = time.time()
            bar = "#" * min(30, int(rms * 400))
            print(f"\r  level [{bar:<30}]", end="", flush=True)
            if rms > max(0.015, 3.0 * noise):
                speech_seen = True
                last_voice = now
            elif not speech_seen:
                noise = 0.95 * noise + 0.05 * min(rms, 0.05)
            if speech_seen and (now - last_voice) >= silence_s and (now - t0) > 1.0:
                break
    print("\n  Captured.")
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    return audio


async def run_all():
    s = load_settings()
    samples_dir = ROOT / "benchmarks" / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    brain = Brain(s)

    w_base = Whisper(STTSpec(model="base", language="auto", quality="balanced", vocab_hints=s.stt.vocab_hints))
    w_small = Whisper(STTSpec(model="small", language="auto", quality="balanced", vocab_hints=s.stt.vocab_hints))
    print("Loading Whisper base and small models...")
    w_base.load()
    w_small.load()

    wake = WakeWord(s.wake)
    wake.load()

    prompts = [
        ("live_wake", "wake", "Say 'Hey Jarvis, what is the time?' (wake word + English test)", "en"),
        ("live_english", "english", "Speak an ENGLISH sentence (e.g. 'Explain how VRAM works on my GPU.')", "en"),
        ("live_malayalam", "malayalam", "Speak a MALAYALAM sentence (e.g. 'ഇന്ന് കാലാവസ്ഥ എങ്ങനെയാണ്?')", "ml"),
        ("live_mixed", "mixed", "Speak a MIXED Malayalam-English sentence (e.g. 'എന്റെ Python project എങ്ങനെയുണ്ട്?')", "auto"),
    ]

    manifest = []
    report = {"samples": []}

    for sid, cat, msg, expected_lang in prompts:
        audio = record_utterance(msg)
        wav_path = samples_dir / f"{sid}.wav"
        wav_path.write_bytes(float_to_wav(audio, SR))
        dur = round(len(audio) / SR, 2)

        wake_peak = 0.0
        if sid == "live_wake":
            pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
            wake.reset()
            wake_peak = max((wake.score(pcm[i:i + FRAME]) for i in range(0, max(0, len(pcm) - FRAME), FRAME)), default=0.0)
            print(f"  Wake word peak score: {wake_peak:.3f} (threshold={s.wake.threshold})")

        res_base = w_base.transcribe(audio, language=expected_lang if expected_lang == "ml" else "auto")
        res_small = w_small.transcribe(audio, language=expected_lang if expected_lang == "ml" else "auto")
        res_small_auto = w_small.transcribe(audio, language="auto") if expected_lang == "ml" else res_small

        print(f"  [base]  lang={res_base['detected_language']}({res_base['language_probability']}) conf={res_base['confidence']} ({res_base['seconds']}s): {res_base['text']!r}")
        print(f"  [small] lang={res_small['detected_language']}({res_small['language_probability']}) conf={res_small['confidence']} ({res_small['seconds']}s): {res_small['text']!r}")
        if expected_lang == "ml":
            print(f"  [small auto-detect] lang={res_small_auto['detected_language']}({res_small_auto['language_probability']}): {res_small_auto['text']!r}")

        # Verify translation separation + original transcript preservation
        tr = Translator(w_small, brain)
        rec_no_tr = await tr.process_stt(res_small, TranslationSpec(mode="transcribe_only", target_language="none"), audio=audio)
        assert rec_no_tr.translated_text is None

        target = "ml" if res_small["detected_language"] == "en" else "en"
        rec_tr = await tr.process_stt(res_small, TranslationSpec(mode="translate", target_language=target), audio=audio)
        print(f"  Translated ({rec_tr.translation_engine} -> {target}): {rec_tr.translated_text!r}")

        edited = rec_tr.corrected_transcript + " [verified]"
        rec_corr = await tr.apply_correction(rec_tr.id, edited, target_language=target)
        assert rec_corr.original_transcript == res_small["original_transcript"], "original_transcript mutated!"
        assert rec_corr.corrected_transcript == edited
        print(f"  Original preserved after correction+translation: {rec_corr.original_transcript!r}")

        ref = input("Optional: type exact reference words (or press ENTER to skip WER): ").strip()
        if ref:
            manifest.append({"id": sid, "file": f"{sid}.wav", "category": cat, "language": expected_lang, "reference": ref})

        report["samples"].append({
            "id": sid,
            "category": cat,
            "duration_s": dur,
            "wake_peak": round(float(wake_peak), 3) if sid == "live_wake" else None,
            "base": res_base,
            "small": res_small,
            "small_auto": res_small_auto if expected_lang == "ml" else None,
            "translated": {"target": target, "engine": rec_tr.translation_engine, "text": rec_tr.translated_text},
            "original_preserved": rec_corr.original_transcript == res_small["original_transcript"],
            "reference": ref or None,
        })

    if manifest:
        (samples_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    out_file = ROOT / "benchmarks" / "live_mic_results.json"
    out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    await brain.client.aclose()
    print(f"\nSaved live microphone verification results to {out_file}")


if __name__ == "__main__":
    asyncio.run(run_all())

