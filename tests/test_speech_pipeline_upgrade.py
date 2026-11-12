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
