"""Regression tests for short-command handling, English-only transcription accuracy,
vocab hint suppression on short clips, and repetition loop cleanup."""
from __future__ import annotations

import json
import numpy as np
import pytest

from jarvis.config import ROOT, STTSpec
from jarvis.stt import (
    SR,
    Whisper,
    clean_english_transcript,
    load_vocab_hints,
    wav_to_float,
)


def test_clean_english_transcript_repetition_loops():
    """Verify repeated-word and repeated-phrase loops are collapsed."""
    assert clean_english_transcript("stop stop stop") == "stop"
    assert clean_english_transcript("no no no no") == "no"
    assert clean_english_transcript("the server the server") == "the server"
    assert clean_english_transcript("Please wait. Please wait.") == "Please wait."
    assert clean_english_transcript("Yes. Yes. Yes.") == "Yes."


def test_clean_english_transcript_strips_leaked_hints():
    """Verify chained vocab hint hallucinations on ambiguous/short audio are stripped."""
    hints = ["Jarvis", "Kavlien", "Ollama", "Qwen", "Python", "Docker", "FastAPI", "Git", "ERP"]
    diag = {
        "duration_s": 0.65,
        "vad_speech_duration_s": 0.15,
        "warnings": ["mostly_silence"],
    }
    leaked = "Jarvis, Kavlien, ERP."
    cleaned = clean_english_transcript(leaked, vocab_hints=hints, audio_diag=diag)
    assert cleaned == ""


def test_clean_english_transcript_preserves_legitimate_commands():
    """Verify legitimate voice commands containing hint words are intact."""
    hints = ["Jarvis", "Kavlien", "Ollama", "Qwen", "Python", "Docker", "FastAPI", "Git"]
    diag = {
        "duration_s": 2.5,
        "vad_speech_duration_s": 2.1,
        "warnings": [],
    }
    cmd = "Please restart the Docker container for FastAPI."
    cleaned = clean_english_transcript(cmd, vocab_hints=hints, audio_diag=diag)
    assert cleaned == cmd


def test_clean_english_transcript_acoustic_single_word_normalization():
    """Verify single-word acoustic confusions ('Top' -> 'Stop.', 'Paws' -> 'Pause.')."""
    assert clean_english_transcript("Top.") == "Stop."
    assert clean_english_transcript("top") == "Stop."
    assert clean_english_transcript("SOP") == "Stop."
    assert clean_english_transcript("Paws.") == "Pause."


def test_vocab_hints_guard_warning(caplog):
    """Verify warning is logged if vocab_hints has more than 10 terms."""
    long_list = "Jarvis, Kavlien, Ollama, Qwen, Python, Docker, FastAPI, Git, Redis, Postgres, MongoDB, React"
    with caplog.at_level("WARNING"):
        terms = load_vocab_hints(override=long_list)
    assert len(terms) > 10
    assert any("Vocabulary hints contains" in r.message for r in caplog.records)


def test_short_command_no_hotword_bias():
    """Verify that for utterances < 2.0s, hotwords and initial_prompt are NOT passed to Whisper."""
    spec = STTSpec(
        model="small.en",
        compute_type="int8",
        cpu_threads=4,
        language="en",
        quality="fast",
        vocab_hints="Jarvis, Kavlien, Ollama, Qwen",
    )
    w = Whisper(spec)

    captured_kwargs = {}

    class MockModel:
        def transcribe(self, audio, **kwargs):
            captured_kwargs.update(kwargs)
            class Seg:
                text = "Stop."
                avg_logprob = -0.2
                no_speech_prob = 0.05
                compression_ratio = 1.0
                words = []
            class Info:
                language = "en"
                language_probability = 0.99
                duration = 0.8
                duration_after_vad = 0.8
            return [Seg()], Info()

    w._m = MockModel()
    w._loaded_key = ("small.en", spec.compute_type, spec.cpu_threads)

    # 0.8s utterance (< 2.0s) with audible tone
    t = np.linspace(0, 0.8, int(SR * 0.8), endpoint=False)
    short_audio = (0.25 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    res = w.transcribe(short_audio, save_audio=False, allow_retry=False)

    assert captured_kwargs.get("hotwords") is None
    assert captured_kwargs.get("initial_prompt") is None
    assert res["text"] == "Stop."
    assert res["language"] == "en"
    assert res["detected_language"] == "en"
    assert res["original_transcript"] == "Stop."


def test_benchmark_short_commands_transcription():
    """Test actual transcription on calibrated short command samples."""
    manifest_path = ROOT / "benchmarks" / "manifest_english_accuracy.json"
    if not manifest_path.exists():
        pytest.skip("Benchmark manifest not present")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = {s["id"]: s for s in manifest.get("samples", [])}

    for cid in ["cmd_stop", "cmd_yes", "cmd_no"]:
        if cid not in samples:
            continue
        s = samples[cid]
        audio_path = ROOT / "benchmarks" / s["audio"]
        if not audio_path.exists():
            continue

        spec = STTSpec(
            model="medium.en",
            compute_type="int8",
            cpu_threads=6,
            language="en",
            quality="accurate",
            vad_filter=True,
            vocab_hints_file="config/vocab_hints.txt",
        )
        w = Whisper(spec)
        audio = wav_to_float(audio_path.read_bytes())
        res = w.transcribe(audio, language="en", quality="accurate", save_audio=False)

        assert res["text"], f"Expected non-empty transcript for {cid}"
        assert res["language"] == "en"
        assert res["detected_language"] == "en"
        # Must not be hallucinated custom vocabulary
        assert "Kavlien" not in res["text"]
        assert "ERP" not in res["text"]
        if cid == "cmd_stop":
            assert res["text"] == "Stop."
        elif cid == "cmd_yes":
            assert res["text"] == "Yes."
        elif cid == "cmd_no":
            assert res["text"] == "No."

