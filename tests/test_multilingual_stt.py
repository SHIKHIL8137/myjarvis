"""Multilingual STT, resampling, short-utterance recovery, translation separation, and transcript preservation tests."""
import asyncio
import io
import types
import wave

from fastapi.testclient import TestClient
import numpy as np
import pytest

from conftest import FakeBrain, needs_piper
from jarvis.api import create_app
from jarvis.config import ROOT, STTSpec, TranslationSpec, load_settings
from jarvis.stt import SR, Whisper, float_to_wav, list_local_models, resample_audio, wav_to_float
from jarvis.translator import Translator
from jarvis.tts import PiperTTS

H = {"X-Jarvis": "1"}
HAS_BASE = (ROOT / "models" / "whisper" / "models--Systran--faster-whisper-base").exists()
HAS_SMALL = (ROOT / "models" / "whisper" / "models--Systran--faster-whisper-small").exists()


def _make_wav(samples_f32: np.ndarray, sr: int = 16000) -> bytes:
    pcm = (np.clip(samples_f32, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_resample_audio_bandlimited_and_dc_removed():
    t = np.arange(48000, dtype=np.float32) / 48000.0
    sig = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.1  # 440 Hz + DC offset
    out = resample_audio(sig, 48000, 16000)
    assert out.dtype == np.float32
    assert len(out) == 16000
    assert abs(float(np.mean(out))) < 0.01  # DC offset removed
    assert np.max(np.abs(out)) <= 1.0


def test_local_whisper_models_discovered():
    models = list_local_models()
    assert isinstance(models, list)
    if HAS_BASE:
        assert "base" in models
    if HAS_SMALL:
        assert "small" in models


@pytest.mark.skipif(not HAS_BASE, reason="multilingual Whisper base model not downloaded")
@needs_piper
def test_english_transcription_and_auto_language_detection():
    s = load_settings()
    tts = PiperTTS(s.tts)
    audio = wav_to_float(tts.wav_bytes("Good afternoon Jarvis, explain VRAM quantization in Ollama."))
    stt = Whisper(STTSpec(model="base", language="auto", quality="balanced", vocab_hints="Jarvis, Ollama, VRAM"))
    res = stt.transcribe(audio)
    assert res["detected_language"] == "en"
    assert res["language_probability"] >= 0.7
    assert 0.0 < res["confidence"] <= 1.0
    assert "ollama" in res["text"].lower() and "vram" in res["text"].lower()
    assert res["original_transcript"] == res["text"]
    assert res["audio_id"] is not None


def test_malayalam_and_hindi_language_routing_and_en_model_rejection(monkeypatch):
    calls = []

    class FakeModel:
        model = types.SimpleNamespace(is_multilingual=True)

        def transcribe(self, audio, **kw):
            calls.append(kw)
            raw_lang = kw.get("language")
            if raw_lang == "ml":
                lang, txt = "ml", "നമസ്കാരം ജാർവിസ്"
            elif raw_lang == "hi":
                lang, txt = "hi", "नमस्ते जार्विस"
            else:
                lang, txt = "ml", "ഇന്ന് weather എങ്ങനെയാണ്?"
            seg = types.SimpleNamespace(text=txt, tokens=[1, 2, 3], avg_logprob=-0.22, no_speech_prob=0.04, compression_ratio=1.2)
            info = types.SimpleNamespace(language=lang, language_probability=0.93, duration=1.5, duration_after_vad=1.4)
            return [seg], info

    stt = Whisper(STTSpec(model="small", language="ml", quality="accurate", vocab_hints="Jarvis"))
    monkeypatch.setattr(stt, "load", lambda *a, **k: FakeModel())
    audio = np.ones(SR, dtype=np.float32) * 0.05

    ml_res = stt.transcribe(audio, language="ml")
    assert ml_res["text"] == "നമസ്കാരം ജാർവിസ്"
    assert ml_res["detected_language"] == "ml"
    assert ml_res["confidence"] > 0.7
    assert calls[-1]["beam_size"] == 5 and calls[-1]["language"] == "ml"

    hi_res = stt.transcribe(audio, language="hi")
    assert hi_res["text"] == "नमस्ते जार्विस" and hi_res["detected_language"] == "hi"

    # Mixed Malayalam + English in auto-detect passes language=None and multilingual=True
    mixed_res = stt.transcribe(audio, language="auto")
    assert calls[-1]["language"] is None and calls[-1].get("multilingual") is True
    assert "weather" in mixed_res["text"] and "ഇന്ന്" in mixed_res["text"]

    # English-only model rejects explicit non-English language cleanly
    en_stt = Whisper(STTSpec(model="small.en", language="en"))
    monkeypatch.setattr(en_stt, "load", lambda *a, **k: types.SimpleNamespace(model=types.SimpleNamespace(is_multilingual=False)))
    with pytest.raises(ValueError, match="English"):
        en_stt.transcribe(audio, language="ml")


def test_short_utterance_vad_recovery(monkeypatch):
    attempts = []

    class FakeModel:
        model = types.SimpleNamespace(is_multilingual=True)

        def transcribe(self, audio, **kw):
            attempts.append(kw["vad_filter"])
            if kw["vad_filter"]:
                return [], types.SimpleNamespace(language="en", language_probability=0.5, duration=0.25, duration_after_vad=0.0)
            seg = types.SimpleNamespace(text="Stop.", tokens=[1], avg_logprob=-0.3, no_speech_prob=0.1, compression_ratio=1.0)
            return [seg], types.SimpleNamespace(language="en", language_probability=0.91, duration=0.25, duration_after_vad=0.25)

    stt = Whisper(STTSpec(model="base", vad_filter=True))
    monkeypatch.setattr(stt, "load", lambda *a, **k: FakeModel())
    t = np.arange(int(0.25 * SR), dtype=np.float32) / SR
    short_audio = (0.08 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    res = stt.transcribe(short_audio)
    assert attempts == [True, False]  # retried without VAD when VAD dropped short speech
    assert res["text"] == "Stop."


def test_translation_separated_from_transcription_and_preserves_original(settings, monkeypatch):
    brain = FakeBrain(settings)
    brain.replies = ["Hello Jarvis, how are you?"]
    stt = Whisper(STTSpec(model="small"))
    tr = Translator(stt, brain)

    # 1. Transcribe-only mode MUST NOT translate silently
    stt_res = {
        "text": "നമസ്കാരം ജാർവിസ്, സുഖമാണോ?",
        "original_transcript": "നമസ്കാരം ജാർവിസ്, സുഖമാണോ?",
        "detected_language": "ml",
        "language_probability": 0.94,
        "confidence": 0.81,
        "audio_id": "rec1",
        "model": "small",
        "seconds": 1.1,
    }
    rec = asyncio.run(tr.process_stt(stt_res, TranslationSpec(mode="transcribe_only", target_language="en")))
    assert rec.translated_text is None and rec.target_language == "none"
    assert brain.seen == []

    # 2. Explicit translation to English uses Whisper translate when audio is given
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda a, **kw: {"text": "Hello Jarvis, how are you?"} if kw.get("task") == "translate" else {"text": ""},
    )
    rec2 = asyncio.run(
        tr.process_stt(
            stt_res,
            TranslationSpec(mode="translate", target_language="en", prefer_whisper_for_english=True),
            audio=np.zeros(SR, dtype=np.float32),
        )
    )
    assert rec2.translation_engine == "whisper"
    assert rec2.translated_text == "Hello Jarvis, how are you?"
    assert rec2.original_transcript == "നമസ്കാരം ജാർവിസ്, സുഖമാണോ?"

    # 3. English -> Malayalam (and mixed-language -> English) uses local Qwen brain
    brain.replies = ["ഇന്ന് കാലാവസ്ഥ എന്താണ്?"]
    en_res = {**stt_res, "audio_id": "rec2", "text": "What is the weather today?", "original_transcript": "What is the weather today?", "detected_language": "en"}
    rec3 = asyncio.run(tr.process_stt(en_res, TranslationSpec(mode="translate", target_language="ml")))
    assert rec3.translation_engine == "qwen"
    assert rec3.translated_text == "ഇന്ന് കാലാവസ്ഥ എന്താണ്?"
    assert rec3.original_transcript == "What is the weather today?"

    # 4. Correction preserves original_transcript immutably
    brain.replies = ["ഇന്ന് താപനില എത്രയാണ്?"]
    updated = asyncio.run(tr.apply_correction("rec2", "What is the temperature today?", target_language="ml"))
    assert updated.original_transcript == "What is the weather today?"
    assert updated.corrected_transcript == "What is the temperature today?"
    assert updated.translated_text == "ഇന്ന് താപനില എത്രയാണ്?"


def test_api_hear_correct_and_translate_preserve_original(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    monkeypatch.setattr(
        assistant.stt,
        "transcribe",
        lambda audio, **kw: {
            "text": "നമസ്കാരം Jarvis" if kw.get("task", "transcribe") == "transcribe" else "Hello Jarvis",
            "original_transcript": "നമസ്കാരം Jarvis",
            "detected_language": "ml",
            "language_probability": 0.92,
            "confidence": 0.83,
            "avg_logprob": -0.2,
            "no_speech_prob": 0.05,
            "compression_ratio": 1.1,
            "audio_id": "aud_abc",
            "model": "small",
            "seconds": 0.8,
        },
    )
    assistant.brain.replies = ["Hello Jarvis!"]
    with TestClient(create_app(settings, assistant)) as c:
        wav_bytes = _make_wav(np.zeros(SR, dtype=np.float32))
        r = c.post("/api/voice/hear", content=wav_bytes, headers=H)
        assert r.status_code == 200
        j = r.json()
        assert j["original_transcript"] == "നമസ്കാരം Jarvis"
        assert j["detected_language"] == "ml"
        assert j["translated_text"] is None  # transcribe_only by default

        # Correct transcript and translate to English
        c_res = c.post(
            "/api/voice/correct",
            json={"id": j["id"], "corrected_transcript": "നമസ്കാരം ജാർവിസ്", "target_language": "en"},
            headers=H,
        )
        assert c_res.status_code == 200
        cj = c_res.json()
        assert cj["original_transcript"] == "നമസ്കാരം Jarvis"
        assert cj["corrected_transcript"] == "നമസ്കാരം ജാർവിസ്"
        assert cj["translated_text"] == "Hello Jarvis!"

        # Direct text translation endpoint
        t_res = c.post("/api/voice/translate", json={"text": "Hello sir", "source_language": "en", "target_language": "ml"}, headers=H)
        assert t_res.status_code == 200 and t_res.json()["target_language"] == "ml"
