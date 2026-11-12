"""Comprehensive unit, integration, API, and DB migration tests for the upgraded audio/STT/translation pipeline."""
from __future__ import annotations

import io
import json
from pathlib import Path
import wave

from fastapi.testclient import TestClient
import numpy as np
import pytest

from jarvis.api import create_app
from jarvis.asr_providers import FasterWhisperProvider, IndicConformerProvider, MalayalamASRProvider
from jarvis.assistant import Assistant
from jarvis.audio_preprocess import (
    TARGET_SR,
    bandlimited_resample,
    diagnose_audio,
    downmix_to_mono,
    parse_wav_raw,
    preprocess_audio,
)
from jarvis.brain import Brain
from jarvis.config import load_settings
from jarvis.db.engine import DatabaseManager
from jarvis.stt import AudioBufferStore, Whisper, evaluate_transcript_quality
from jarvis.translator import (
    TranscriptRecord,
    TranscriptStore,
    Translator,
    export_consented_sample,
    guard_correction,
    verify_translation,
)

H = {"X-Jarvis": "1"}


def _wav_bytes(samples: np.ndarray, sr: int = 16000) -> bytes:
    arr = np.asarray(samples, dtype=np.float32)
    i16 = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
    ch = 1 if i16.ndim == 1 else i16.shape[1]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(i16.tobytes())
    return buf.getvalue()


def _speech_like(seconds: float = 1.6, sr: int = 16000, amp: float = 0.25, channels: int = 1) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    sig = amp * (0.65 * np.sin(2 * np.pi * 210.0 * t) + 0.35 * np.sin(2 * np.pi * 430.0 * t))
    env = np.sin(np.pi * np.linspace(0.05, 0.95, len(sig), dtype=np.float32))
    mono = (sig * env).astype(np.float32)
    if channels == 1:
        return mono
    return np.stack([mono, mono * 0.9], axis=1).astype(np.float32)


class _DummySegment:
    def __init__(self, text: str, avg_logprob: float = -0.22, no_speech_prob: float = 0.05, compression_ratio: float = 1.2):
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio
        self.start = 0.0
        self.end = 1.2


class _DummyInfo:
    def __init__(self, language: str = "en", prob: float = 0.94):
        self.language = language
        self.language_probability = prob


class _FakeBrain:
    def __init__(self, replies: list[str] | None = None):
        s = load_settings()
        self.spec = s.brains[s.boot_brain]
        self._replies = list(replies or [])

    async def stream_raw(self, messages, num_ctx: int = 2048, temperature: float = 0.05):
        out = self._replies.pop(0) if self._replies else ""
        yield out


def test_parse_wav_raw_downmix_and_resample_rates():
    for sr in (8000, 16000, 44100, 48000):
        stereo = _speech_like(seconds=0.8, sr=sr, channels=2)
        wav = _wav_bytes(stereo, sr=sr)
        raw, out_sr, ch = parse_wav_raw(wav)
        assert out_sr == sr and ch == 2 and raw.ndim == 2
        mono, det_ch = downmix_to_mono(raw)
        assert det_ch == 2 and mono.ndim == 1
        out_16k = bandlimited_resample(mono, out_sr, TARGET_SR, remove_dc=True)
        assert abs(len(out_16k) - int(0.8 * TARGET_SR)) <= 8


def test_diagnostics_empty_short_clipped_silence_and_preprocessing_safety():
    diag_empty = diagnose_audio(np.zeros(0, dtype=np.float32), sr=16000)
    assert diag_empty.empty_or_too_short

    diag_short = diagnose_audio(np.ones(800, dtype=np.float32) * 0.1, sr=16000)
    assert diag_short.empty_or_too_short

    clipped = np.clip(_speech_like(1.0, 16000, amp=1.8), -1.0, 1.0)
    diag_clip = diagnose_audio(clipped, sr=16000)
    assert diag_clip.clipped and diag_clip.clipping_percent > 1.0

    # Pure quiet room noise must NOT be boosted into Whisper hallucination territory
    rng = np.random.default_rng(7)
    floor = (rng.normal(0.0, 0.0005, 16000)).astype(np.float32)
    clean_floor, diag_floor = preprocess_audio(floor, 16000, profile="light")
    assert diag_floor.gain_applied_db == 0.0
    assert float(np.max(np.abs(clean_floor))) < 0.01

    # Quiet real speech gets bounded gain normalization (<= 5x / ~14 dB) + standard trim preserves margins
    quiet_speech = np.concatenate([np.zeros(12000, dtype=np.float32), _speech_like(1.0, 16000, amp=0.025), np.zeros(12000, dtype=np.float32)])
    proc_std, diag_std = preprocess_audio(quiet_speech, 16000, profile="standard")
    assert 0.0 < diag_std.gain_applied_db <= 14.5
    assert diag_std.trimmed is True
    assert len(proc_std) >= int(16000 * 1.0)

    # Off profile skips filtering/normalization
    raw_sig = _speech_like(0.8, 16000, amp=0.05)
    proc_off, diag_off = preprocess_audio(raw_sig, 16000, profile="off")
    assert diag_off.gain_applied_db == 0.0 and len(proc_off) == len(raw_sig)


def test_stt_silence_short_circuit_script_mismatch_and_bounded_retry(tmp_path, monkeypatch):
    s = load_settings()
    s.stt.model = "base"
    s.stt.quality = "balanced"
    s.stt.retry_on_low_quality = True
    s.stt.max_retry_attempts = 2
    s.stt.audio_store_dir = str(tmp_path / "audio")
    w = Whisper(s.stt)

    # Empty / dead silence must never call model or hallucinate
    called = {"n": 0}

    def _never_call(_m):
        called["n"] += 1
        raise AssertionError("model should not be loaded for dead silence")

    monkeypatch.setattr(w, "load", _never_call)
    res_silent = w.transcribe(np.zeros(16000, dtype=np.float32))
    assert res_silent["text"] == "" and "no_speech_energy" in res_silent["quality_flags"] and called["n"] == 0

    # Malayalam output written in Devanagari must fail quality check & retry cleanly
    pass_idx = {"n": 0}

    class RetryModel:
        def transcribe(self, audio, **kw):
            pass_idx["n"] += 1
            if pass_idx["n"] == 1:
                return [_DummySegment("विंदान समयः", avg_logprob=-0.25)], _DummyInfo("ml", 0.91)
            return [_DummySegment("ഇന്ന് നല്ല ദിവസമാണ്", avg_logprob=-0.22)], _DummyInfo("ml", 0.96)

    monkeypatch.setattr(w, "load", lambda m=None: RetryModel())
    audio = _speech_like(1.5, 16000, amp=0.3)
    res_retry = w.transcribe(audio, language="auto")
    assert pass_idx["n"] >= 2
    assert "ഇന്ന്" in res_retry["text"]
    assert res_retry["detected_language"] == "ml"
    assert len(res_retry["attempts"]) >= 2

    # Direct quality flag check for Devanagari on ml
    ev = evaluate_transcript_quality(
        {
            "text": "विंदान",
            "detected_language": "ml",
            "language_probability": 0.92,
            "avg_logprob": -0.2,
            "no_speech_prob": 0.1,
            "compression_ratio": 1.1,
        }
    )
    assert "script_mismatch" in ev["flags"] and ev["quality_tier"] == "low"


def test_provider_lru_cache_and_optional_malayalam_unavailable(tmp_path):
    fw = FasterWhisperProvider(max_cached=2)
    fw._cache[("m1", "int8", 4)] = object()
    fw._cache[("m2", "int8", 4)] = object()
    fw._cache[("m3", "int8", 4)] = object()
    while len(fw._cache) > fw.max_cached:
        fw._cache.popitem(last=False)
    assert [k[0] for k in fw._cache.keys()] == ["m2", "m3"]

    ml_missing = MalayalamASRProvider(model_dir=tmp_path / "no_hf", ct2_dir=tmp_path / "no_ct2")
    assert ml_missing.is_available() is False
    ic_missing = IndicConformerProvider(model_dir=tmp_path / "nonexistent_conformer")
    assert ic_missing.is_available() is False


@pytest.mark.anyio
async def test_correction_guard_translation_verification_and_retry(tmp_path):
    # Guard accepts benign spelling/tech casing normalization
    ok_txt, accepted, flags = guard_correction("open fast api on port 8000", "Open FastAPI on port 8000.")
    assert accepted and ok_txt == "Open FastAPI on port 8000." and not flags

    # Guard rejects hallucinated expansion, missing numbers, or flipped negations
    orig = "Do not delete file 42."
    kept_exp, ok_exp, f_exp = guard_correction(orig, orig + " Wait, let me also explain in 120 characters why deletion might be bad for you today.")
    assert not ok_exp and kept_exp == orig and "correction_rejected_expansion" in f_exp

    kept_num, ok_num, f_num = guard_correction(orig, "Do not delete file 99.")
    assert not ok_num and kept_num == orig and "correction_rejected_number_mismatch" in f_num

    kept_neg, ok_neg, f_neg = guard_correction(orig, "Please delete file 42.")
    assert not ok_neg and kept_neg == orig and "correction_rejected_negation_change" in f_neg

    # Semantic verification checks
    v_bad = verify_translation("Open FastAPI port 8080 now?", "ഇപ്പോൾ തുറക്കൂ.", "en", "ml")
    assert not v_bad["passed"] and "missing_number:8080" in v_bad["warnings"]

    v_good = verify_translation("Open FastAPI port 8080 now?", "ഇപ്പോൾ FastAPI പോർട്ട് 8080 തുറക്കണോ?", "en", "ml")
    assert v_good["passed"] and not v_good["uncertain"]

    # Translation auto-retry repairs first flawed attempt
    brain = _FakeBrain(
        replies=[
            "ഇപ്പോൾ തുറക്കൂ.",  # missing 8080 + ?
            "ഇപ്പോൾ FastAPI പോർട്ട് 8080 തുറക്കണോ?",
        ]
    )
    tr = Translator(stt=None, brain=brain)
    out, rep = await tr.translate_text_with_verification("Open FastAPI port 8080 now?", "en", "ml")
    assert "8080" in out and rep["passed"]


def test_db_persistence_immutability_consent_and_delete(tmp_path):
    db = DatabaseManager(data_dir=tmp_path)
    db.init_schema_sync()
    store = TranscriptStore(maxlen=10, db_session_factory=db.sync_session)
    audio_store = AudioBufferStore(disk_dir=tmp_path / "audio")
    audio_id = audio_store.put(_speech_like(1.0, 44100, channels=1), sample_rate=44100, channels=1)

    rec = TranscriptRecord(
        id="rec001",
        audio_id=audio_id,
        original_transcript="check qwen server 4",
        corrected_transcript="Check Qwen server 4",
        detected_language="en",
        language_probability=0.98,
        confidence=0.91,
        avg_logprob=-0.21,
        no_speech_prob=0.04,
        compression_ratio=1.1,
        model="small",
        seconds=1.0,
    )
    store.put(rec)

    # Re-open from DB cleanly
    reloaded_store = TranscriptStore(maxlen=10, db_session_factory=db.sync_session)
    got = reloaded_store.get("rec001")
    assert got is not None and got.original_transcript == "check qwen server 4"

    # Attempted overwrite of original_transcript via put keeps immutable original
    tampered = TranscriptRecord(
        id="rec001",
        audio_id=audio_id,
        original_transcript="TAMPERED ORIGINAL",
        corrected_transcript="Check Qwen3 server 4",
        detected_language="en",
        language_probability=0.98,
        confidence=0.91,
        avg_logprob=-0.21,
        no_speech_prob=0.04,
        compression_ratio=1.1,
    )
    store.put(tampered)
    assert store.get("rec001").original_transcript == "check qwen server 4"

    # Consented eval export rejects unconsented, writes consented cleanly
    with pytest.raises(ValueError):
        export_consented_sample(got, tmp_path / "eval")

    got.eval_consent = True
    exported = export_consented_sample(got, tmp_path / "eval", audio_wav_bytes=audio_store.get_wav_bytes(audio_id))
    assert exported.exists() and (tmp_path / "eval" / f"{got.id}.wav").exists()

    assert store.delete("rec001") is True
    assert TranscriptStore(maxlen=10, db_session_factory=db.sync_session).get("rec001") is None
    assert audio_store.delete(audio_id) is True and audio_store.get_raw(audio_id) is None


def test_voice_api_replay_diagnostics_retry_correct_consent_and_delete(tmp_path, monkeypatch):
    s = load_settings()
    s.stt.audio_store_dir = str(tmp_path / "voice_audio")
    brain = Brain(s)
    asst = Assistant(s, data_dir=tmp_path, brain=brain)
    monkeypatch.setattr(asst, "_warm_speech", lambda: None)

    class _MockModel:
        def transcribe(self, audio, **kw):
            lang = kw.get("language") or "en"
            return [_DummySegment("open fast api now", avg_logprob=-0.25)], _DummyInfo(lang, 0.95)

    monkeypatch.setattr(asst.stt, "load", lambda m=None: _MockModel())

    # Disable host-mic device probing in test client lifecycle
    stereo_44k = _wav_bytes(_speech_like(1.1, sr=44100, channels=2), sr=44100)
    app = create_app(s, assistant=asst)
    with TestClient(app) as client:
        r_hear = client.post("/api/voice/hear", content=stereo_44k, headers=H)
        assert r_hear.status_code == 200
        rec = r_hear.json()
        audio_id = rec["audio_id"]
        rec_id = rec["id"]
        assert rec["original_transcript"] == "open fast api now"
        assert rec["audio_diagnostics"]["sample_rate"] == 44100
        assert rec["audio_diagnostics"]["channels"] == 2

        # Replay stored original WAV -> must still be 44.1 kHz stereo!
        r_aud = client.get(f"/api/voice/audio/{audio_id}")
        assert r_aud.status_code == 200 and r_aud.headers["content-type"].startswith("audio/wav")
        raw_out, sr_out, ch_out = parse_wav_raw(r_aud.content)
        assert sr_out == 44100 and ch_out == 2 and len(raw_out) > 40000

        # Diagnostics endpoint
        r_diag = client.get(f"/api/voice/diagnostics/{audio_id}")
        assert r_diag.status_code == 200 and r_diag.json()["diagnostics"]["sample_rate"] == 44100

        # Retry endpoint
        r_retry = client.post(
            "/api/voice/retry",
            json={"audio_id": audio_id, "model": "small", "language": "en", "quality": "high", "preprocessing_profile": "off"},
            headers=H,
        )
        assert r_retry.status_code == 200
        retry_rec = r_retry.json()["transcript_record"]
        assert retry_rec["model"] == "small"

        # Manual correction keeps original_transcript immutable
        r_corr = client.post(
            "/api/voice/correct",
            json={"id": rec_id, "corrected_transcript": "Open FastAPI now."},
            headers=H,
        )
        assert r_corr.status_code == 200
        assert r_corr.json()["original_transcript"] == "open fast api now"
        assert r_corr.json()["corrected_transcript"] == "Open FastAPI now."

        # Consented eval save
        asst.settings.translation.eval_export_dir = str(tmp_path / "consented_eval")
        r_cons = client.post("/api/voice/eval-consent", json={"id": rec_id, "consent": True, "include_audio": True}, headers=H)
        assert r_cons.status_code == 200 and r_cons.json()["ok"]
        exported = json.loads(Path(r_cons.json()["exported_path"]).read_text(encoding="utf-8"))
        assert exported["original_transcript"] == "open fast api now"
        assert exported["corrected_transcript"] == "Open FastAPI now."

        # Delete transcript + audio
        r_del = client.delete(f"/api/voice/transcripts/{rec_id}?delete_audio=true", headers=H)
        assert r_del.status_code == 200 and r_del.json()["ok"]
        assert client.get(f"/api/voice/audio/{audio_id}").status_code == 404


def test_malayalam_provider_fallback_matrix_and_mixed_guard(tmp_path, monkeypatch):
    """Phase 1 & 3 & 6: Specialist high->accept, low/script_mismatch/exception/unavailable->FW fallback, mixed preserves Latin tech words."""
    class _FakeSpecialProvider:
        name = "malayalam_whisper"

        def __init__(self, mode: str = "high"):
            self.mode = mode
            self.calls = 0

        def is_available(self) -> bool:
            return self.mode != "unavailable"

        def unload(self, model_name=None):
            return None

        def transcribe(self, audio, options=None):
            self.calls += 1
            if self.mode == "error":
                raise RuntimeError("corrupt specialist checkpoint")
            if self.mode == "low":
                return {
                    "provider": self.name,
                    "model": "whisper-small-malayalam-v2",
                    "text": "x x x x x x",
                    "detected_language": "ml",
                    "language_probability": 0.9,
                    "confidence": 0.25,
                    "avg_logprob": -1.35,
                    "no_speech_prob": 0.8,
                    "compression_ratio": 2.6,
                    "duration_s": 1.6,
                    "duration_after_vad_s": 1.4,
                }
            if self.mode == "script_mismatch":
                return {
                    "provider": self.name,
                    "model": "whisper-small-malayalam-v2",
                    "text": "नदान समयः",
                    "detected_language": "ml",
                    "language_probability": 0.95,
                    "confidence": 0.85,
                    "avg_logprob": -0.22,
                    "no_speech_prob": 0.05,
                    "compression_ratio": 1.1,
                    "duration_s": 1.6,
                    "duration_after_vad_s": 1.4,
                }
            return {
                "provider": self.name,
                "model": "whisper-small-malayalam-v2",
                "text": "ഇന്ന് കാലാവസ്ഥ നല്ലതാണ്",
                "detected_language": "ml",
                "language_probability": 0.99,
                "confidence": 0.91,
                "avg_logprob": -0.18,
                "no_speech_prob": 0.03,
                "compression_ratio": 1.05,
                "duration_s": 1.6,
                "duration_after_vad_s": 1.4,
            }

    class _GoodFWModel:
        def __init__(self, text="ഇന്ന് കാലാവസ്ഥ എങ്ങനെയാണ്", lang="ml"):
            self.text = text
            self.lang = lang
            self.calls = 0

        def transcribe(self, audio, **kw):
            self.calls += 1
            return [_DummySegment(self.text, avg_logprob=-0.2)], _DummyInfo(self.lang, 0.96)

    audio = _speech_like(1.6, 16000, amp=0.25)

    # 1. High quality specialist -> accepted directly without unnecessary FW fallback
    s = load_settings()
    s.stt.model = "base"
    s.stt.malayalam_model = "malayalam_whisper"
    s.stt.audio_store_dir = str(tmp_path / "aud1")
    w = Whisper(s.stt)
    sp_high = _FakeSpecialProvider("high")
    w.malayalam_provider = sp_high
    w._providers["malayalam_whisper"] = sp_high
    fw_calls = {"n": 0}
    monkeypatch.setattr(w, "load", lambda m=None: fw_calls.__setitem__("n", fw_calls["n"] + 1) or _GoodFWModel())
    r_high = w.transcribe(audio, language="ml", save_audio=False)
    assert r_high["provider"] == "malayalam_whisper"
    assert r_high["recognition_quality"] == "high"
    assert r_high["retry_triggered"] is False
    assert fw_calls["n"] == 0

    # 2. Specialist returns script_mismatch (Devanagari) -> falls back to Faster-Whisper cleanly
    sp_bad_script = _FakeSpecialProvider("script_mismatch")
    w.malayalam_provider = sp_bad_script
    w._providers["malayalam_whisper"] = sp_bad_script
    fw_model = _GoodFWModel("ഇന്ന് സമയം എന്താണ്", "ml")
    monkeypatch.setattr(w, "load", lambda m=None: fw_model)
    r_script = w.transcribe(audio, language="ml", save_audio=False)
    assert fw_model.calls >= 1
    assert r_script["provider"] == "faster_whisper"
    assert "ഇന്ന്" in r_script["text"]
    assert r_script["retry_triggered"] is True
    assert len(r_script["attempts"]) >= 2

    # 3. Specialist returns low quality -> falls back to Faster-Whisper & FW wins
    sp_low = _FakeSpecialProvider("low")
    w.malayalam_provider = sp_low
    w._providers["malayalam_whisper"] = sp_low
    fw_model2 = _GoodFWModel("എന്റെ ഫയൽ തുറക്കൂ", "ml")
    monkeypatch.setattr(w, "load", lambda m=None: fw_model2)
    r_low = w.transcribe(audio, language="ml", save_audio=False)
    assert r_low["provider"] == "faster_whisper"
    assert "ഫയൽ" in r_low["text"]

    # 4. Specialist raises exception -> graceful fallback + provider_errors captured
    sp_err = _FakeSpecialProvider("error")
    w.malayalam_provider = sp_err
    w._providers["malayalam_whisper"] = sp_err
    monkeypatch.setattr(w, "load", lambda m=None: _GoodFWModel("ഹലോ ജാർവിസ്", "ml"))
    r_err = w.transcribe(audio, language="ml", save_audio=False)
    assert r_err["provider"] == "faster_whisper"
    assert "ഹലോ" in r_err["text"]
    assert r_err["provider_errors"] and r_err["provider_errors"][0]["provider"] == "malayalam_whisper"
    assert any(a.get("error") for a in r_err["attempts"])

    # 5. Specialist configured but missing on disk -> provider_status + provider_unavailable flag + FW works
    sp_unavail = _FakeSpecialProvider("unavailable")
    w.malayalam_provider = sp_unavail
    w._providers["malayalam_whisper"] = sp_unavail
    r_unavail = w.transcribe(audio, language="ml", save_audio=False)
    assert r_unavail["provider"] == "faster_whisper"
    assert r_unavail["provider_status"]["available"] is False
    assert "provider_unavailable" in r_unavail["quality_flags"]

    # 6. Mixed Malayalam-English does NOT blindly use monolingual specialist or drop tech English tokens
    sp_ml_only = _FakeSpecialProvider("high")
    w.malayalam_provider = sp_ml_only
    w._providers["malayalam_whisper"] = sp_ml_only
    fw_mixed = _GoodFWModel("എന്റെ Python FastAPI project തുറക്കൂ", "ml")
    monkeypatch.setattr(w, "load", lambda m=None: fw_mixed)
    r_mixed = w.transcribe(audio, language="mixed", save_audio=False)
    assert sp_ml_only.calls == 0
    assert "FastAPI" in r_mixed["text"] and "എന്റെ" in r_mixed["text"]


def test_quality_evaluator_conditions_short_commands_and_unique_retries(tmp_path, monkeypatch):
    """Phase 2 & 6: All quality anomalies caught cleanly while valid short commands stay 'high' and retries never repeat identical configs."""
    # Valid short commands remain high quality
    for cmd_txt, lang in [("Stop.", "en"), ("Yes", "en"), ("അതെ", "ml")]:
        ev_cmd = evaluate_transcript_quality(
            {"text": cmd_txt, "detected_language": lang, "language_probability": 0.92, "avg_logprob": -0.25, "no_speech_prob": 0.08, "compression_ratio": 1.0, "duration_s": 0.7, "duration_after_vad_s": 0.5},
            {"peak_amplitude": 0.3, "rms_level": 0.08, "vad_speech_duration_s": 0.5, "empty_or_too_short": False},
            requested_language=lang,
        )
        assert ev_cmd["quality_tier"] == "high" and not ev_cmd["flags"]

    # Empty output with speech energy
    ev_empty = evaluate_transcript_quality(
        {"text": "", "detected_language": "en", "language_probability": 0.9, "no_speech_prob": 0.1, "duration_s": 1.4, "duration_after_vad_s": 1.1},
        {"peak_amplitude": 0.25, "rms_level": 0.06, "vad_speech_duration_s": 1.1, "empty_or_too_short": False},
    )
    assert "empty_with_speech_energy" in ev_empty["flags"] and ev_empty["quality_tier"] == "low"

    # Repetition loop (consecutive words & char run)
    ev_rep_w = evaluate_transcript_quality({"text": "samayak samayak samayak", "detected_language": "ml", "language_probability": 0.9, "avg_logprob": -0.3, "duration_s": 1.5})
    assert "repetition_hallucination" in ev_rep_w["flags"]
    ev_rep_c = evaluate_transcript_quality({"text": "വവവവവവവ", "detected_language": "ml", "language_probability": 0.9, "avg_logprob": -0.3, "duration_s": 1.5})
    assert "repetition_hallucination" in ev_rep_c["flags"]

    # Suspiciously long hallucination on very short audio
    ev_short_long = evaluate_transcript_quality({
        "text": "Thank you very much for watching this video please subscribe today",
        "detected_language": "en",
        "language_probability": 0.95,
        "avg_logprob": -0.3,
        "no_speech_prob": 0.1,
        "duration_s": 0.55,
        "duration_after_vad_s": 0.4,
    })
    assert "short_audio_long_hallucination" in ev_short_long["flags"] and ev_short_long["quality_tier"] == "low"

    # Explicit requested language mismatch + Latin-only output on explicit 'ml'
    ev_lang = evaluate_transcript_quality({
        "text": "Indano samayak doctor today",
        "detected_language": "tl",
        "language_probability": 0.4,
        "avg_logprob": -0.4,
        "no_speech_prob": 0.1,
        "duration_s": 2.0,
    }, requested_language="ml")
    assert "language_mismatch" in ev_lang["flags"] and "script_mismatch" in ev_lang["flags"]

    # Bounded diverse retries never duplicate identical beam/vad settings
    s = load_settings()
    s.stt.model = "base"
    s.stt.quality = "balanced"
    s.stt.max_retry_attempts = 2
    s.stt.audio_store_dir = str(tmp_path / "aud_retry")
    w = Whisper(s.stt)
    seen_kw: list[tuple] = []

    class _TrackingModel:
        def transcribe(self, audio, **kw):
            seen_kw.append((kw.get("beam_size"), kw.get("vad_filter"), kw.get("condition_on_previous_text")))
            return [_DummySegment("नदान", avg_logprob=-1.1, no_speech_prob=0.1)], _DummyInfo("ml", 0.9)

    monkeypatch.setattr(w, "load", lambda m=None: _TrackingModel())
    r = w.transcribe(_speech_like(1.5, 16000, amp=0.3), language="ml", save_audio=False)
    assert r["retry_triggered"] is True
    assert len(r["attempts"]) <= 3
    assert len(seen_kw) == len(set(seen_kw))


@pytest.mark.anyio
async def test_phase4_and_5_guards_translation_and_api_visibility(tmp_path, monkeypatch):
    """Phase 4, 5 & 8: Expanded correction guard, translation verification honesty, and API/DB quality status propagation."""
    # Correction guard catches dates/times, entities, negation scope, intent flip, word drift, paraphrase, LLM chatter
    assert guard_correction("Schedule meeting tomorrow at 5 pm.", "Schedule meeting today at 6 pm.")[1] is False
    assert "correction_rejected_datetime_mismatch" in guard_correction("Schedule meeting tomorrow at 5 pm.", "Schedule meeting today at 5 pm.")[2]
    assert "correction_rejected_entity_mismatch" in guard_correction("Call Rahul about FastAPI.", "Call Rohan about FastAPI.")[2]
    assert "correction_rejected_entity_mismatch" in guard_correction("Use FastAPI and Qwen3.", "Use Flask and Qwen3.")[2]
    assert "correction_rejected_intent_change" in guard_correction("Open project settings.", "Open project settings?")[2]
    assert "correction_rejected_word_drift" in guard_correction("Open database backup folder.", "Open cloud archive storage.")[2]
    assert "correction_rejected_llm_chatter" in guard_correction("Check server status.", "Here is the corrected transcript:\n\nCheck server status.")[2]

    # Verification preserves numbers, times, tech terms, negation, question intent, and never claims semantic proof
    v_ml_missing_tech = verify_translation("Open FastAPI port 8080 tomorrow?", "നാളെ പോർട്ട് 8080 തുറക്കണോ?", "en", "ml")
    assert v_ml_missing_tech["uncertain"] is True and "missing_term:FastAPI" in v_ml_missing_tech["warnings"]
    assert v_ml_missing_tech["semantic_equivalence_proven"] is False

    v_neg = verify_translation("Do not stop server.", "സെർവർ നിർത്തൂ.", "en", "ml")
    assert not v_neg["passed"] and "negation_mismatch" in v_neg["warnings"]

    v_echo = verify_translation("Please open the application settings now.", "Please open the application settings now.", "en", "ml")
    assert not v_echo["passed"] and "untranslated_copy" in v_echo["warnings"]

    # End-to-end API surfaces recognition_quality="high" cleanly (previously stuck at "medium") & uncertain status
    s = load_settings()
    s.stt.audio_store_dir = str(tmp_path / "aud_api")
    asst = Assistant(s, data_dir=tmp_path, brain=Brain(s))
    monkeypatch.setattr(asst, "_warm_speech", lambda: None)

    class _CleanModel:
        def transcribe(self, audio, **kw):
            return [_DummySegment("open fast api now", avg_logprob=-0.18, no_speech_prob=0.04)], _DummyInfo("en", 0.98)

    monkeypatch.setattr(asst.stt, "load", lambda m=None: _CleanModel())
    app = create_app(s, assistant=asst)
    wav = _wav_bytes(_speech_like(1.1, sr=16000, channels=1), sr=16000)
    with TestClient(app) as client:
        r = client.post("/api/voice/hear", content=wav, headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["recognition_quality"] == "high"
        assert body["review_needed"] is False
        assert body["status"] == "completed"
        assert body["provider_errors"] == []

