"""TTS cleaning/chunking, STT parsing, Speaker control, wake word, VoiceLoop resource handling (mocked mic) + real-model loops."""
import io
import time
import types
import wave

import numpy as np
import pytest

from conftest import needs_mic, needs_piper, needs_wake, needs_whisper
from jarvis.config import load_settings
from jarvis.stt import Whisper, wav_to_float
from jarvis.tts import PiperTTS, Speaker, clean_for_speech, split_chunks
from jarvis.voice import VoiceLoop
from jarvis.wakeword import FRAME, WakeWord


def wav(sr=16000, ch=1, secs=0.5):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(ch), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((np.sin(np.linspace(0, 200, int(sr * secs) * ch)) * 8000).astype(np.int16).tobytes())
    return b.getvalue()


# ---- text for speech ----
def test_clean_for_speech_removes_markdown_emoji_code():
    t = clean_for_speech("# Title\n**Bold** and `code` 😊 see [docs](http://x.y/z).\n```python\nprint(1)\n```\n- item")
    assert "*" not in t and "`" not in t and "😊" not in t and "http" not in t and "print" not in t and "#" not in t
    assert "Bold and code" in t and t.endswith("The code is on your screen.") and " ." not in t


def test_split_chunks_never_breaks_sentences():
    text = " ".join(f"This is sentence number {i}, written to test the chunking logic." for i in range(10))
    chunks = split_chunks(text, 170)
    assert all(len(c) <= 200 for c in chunks) and all(c.endswith(".") for c in chunks) and " ".join(chunks) == text
    assert split_chunks("Short one.", 170) == ["Short one."] and split_chunks("") == []
    assert all(len(c) <= 340 for c in split_chunks("word " * 300, 170))  # one endless sentence still gets cut


# ---- stt parsing ----
def test_wav_to_float_resamples_and_validates():
    a = wav_to_float(wav(sr=48000, ch=2, secs=1.0))
    assert a.dtype == np.float32 and abs(len(a) - 16000) <= 1 and np.abs(a).max() <= 1
    with pytest.raises(ValueError):
        wav_to_float(b"not a wav at all")


# ---- Speaker control (fake synth, mocked sounddevice) ----
class FakeTTS:
    spec = types.SimpleNamespace(enabled=True, chunk_chars=170, output_device=None)

    def synth(self, text):
        return 16000, np.zeros(16000, dtype=np.int16)  # 1 s of silence per chunk


def test_speaker_stop_is_immediate_and_discards_queue(monkeypatch):
    import sounddevice as sd
    state = {"active": False, "plays": 0}
    monkeypatch.setattr(sd, "play", lambda *a, **k: state.update(active=True, plays=state["plays"] + 1))
    monkeypatch.setattr(sd, "stop", lambda: state.update(active=False))
    monkeypatch.setattr(sd, "get_stream", lambda: types.SimpleNamespace(active=state["active"]))
    events = []
    sp = Speaker(FakeTTS(), on_state=events.append)
    sp.say(" ".join(f"Sentence {i} is here and it is reasonably long to force many chunks of text." for i in range(12)))
    time.sleep(0.5)
    assert sp.speaking
    t = time.time()
    sp.stop()
    assert time.time() - t < 0.5 and not sp.speaking and sp._pending == 0
    time.sleep(0.5)
    assert state["plays"] <= 2 and events[-1] is False  # nothing queued kept playing after stop


def test_speaker_disabled_says_nothing():
    f = FakeTTS()
    f.spec = types.SimpleNamespace(enabled=False, chunk_chars=170, output_device=None)
    assert Speaker(f).say("hello") == 0


# ---- VoiceLoop resource handling with a mocked microphone ----
class FakeStream:
    instances = []

    def __init__(self, **kw):
        self.closed, self.reads = False, 0
        FakeStream.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self, n):
        self.reads += 1
        time.sleep(0.01)
        return np.zeros((n, 1), dtype=np.int16), False


def make_voice(settings, **kw):
    ev = []
    wake = types.SimpleNamespace(load=lambda: None, score=lambda f: 0.0, reset=lambda: None)
    v = VoiceLoop(settings, wake, types.SimpleNamespace(transcribe=lambda a: {"text": "", "seconds": 0}),
                  types.SimpleNamespace(speaking=False, stop=lambda: None), ev.append, lambda t: None)
    return v, ev


def test_voice_loop_releases_microphone(settings, monkeypatch):
    import sounddevice as sd
    FakeStream.instances.clear()
    monkeypatch.setattr(sd, "InputStream", FakeStream)
    v, ev = make_voice(settings)
    v.enable()
    time.sleep(0.4)
    assert v.state == "listening" and FakeStream.instances[0].reads > 5
    v.disable()
    assert v.state == "off" and FakeStream.instances[0].closed and not v._thread.is_alive()


def test_voice_loop_reports_missing_microphone(settings, monkeypatch):
    import sounddevice as sd
    monkeypatch.setattr(sd, "InputStream", lambda **k: (_ for _ in ()).throw(OSError("no device")))
    v, ev = make_voice(settings)
    v.enable()
    time.sleep(0.3)
    assert v.state == "off" and "no device" in v.last_error and any(e["type"] == "error" for e in ev)


def test_voice_loop_records_and_transcribes_utterance(settings, monkeypatch):
    import sounddevice as sd
    loud = (np.random.default_rng(0).standard_normal((FRAME, 1)) * 3000).astype(np.int16)

    class Stream(FakeStream):
        def read(self, n):
            self.reads += 1
            time.sleep(0.005)
            return (loud if 10 <= self.reads < 30 else np.zeros((n, 1), dtype=np.int16)), False
    monkeypatch.setattr(sd, "InputStream", Stream)
    got, ev = [], []
    wake = types.SimpleNamespace(load=lambda: None, score=lambda f: 1.0 if got == [] and Stream.instances[-1].reads == 5 else 0.0, reset=lambda: None)
    settings.audio.silence_ms = 300
    v = VoiceLoop(settings, wake, types.SimpleNamespace(transcribe=lambda a: {"text": "hello there", "seconds": 0.1}),
                  types.SimpleNamespace(speaking=False, stop=lambda: None), ev.append, got.append)
    v.enable()
    for _ in range(100):
        if got:
            break
        time.sleep(0.05)
    v.disable()
    assert got == ["hello there"] and [e["state"] for e in ev if e["type"] == "voice_state"][:3] == ["listening", "recording", "transcribing"]


# ---- real models ----
@needs_piper
def test_piper_synth_cached_and_valid():
    t = PiperTTS(load_settings().tts)
    sr, a = t.synth("Cache test sentence number one.")
    assert sr >= 16000 and len(a) > sr // 2
    t0 = time.time()
    assert np.array_equal(t.synth("Cache test sentence number one.")[1], a) and time.time() - t0 < 0.2  # disk cache hit


@needs_piper
@needs_whisper
def test_voice_loop_piper_to_whisper_roundtrip():
    s = load_settings()
    tts, stt = PiperTTS(s.tts), Whisper(s.stt)
    sentence = "Good afternoon sir the local voice is live"
    r = stt.transcribe(wav_to_float(tts.wav_bytes(sentence + ".")))
    got = set(r["text"].lower().replace(",", "").replace(".", "").split())
    assert len(got & set(sentence.lower().split())) >= 7, r  # allow one small difference


@needs_piper
@needs_wake
def test_wake_word_detects_hey_jarvis_and_ignores_other_speech():
    s = load_settings()
    tts, w = PiperTTS(s.tts), WakeWord(s.wake)

    def peak(text):
        sr, a = tts.synth(text)
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr)), np.arange(len(a)), a.astype(np.float32)).astype(np.int16)
        a = np.concatenate([np.zeros(16000, np.int16), a, np.zeros(16000, np.int16)])
        w.reset()
        return max(w.score(a[i:i + FRAME]) for i in range(0, len(a) - FRAME, FRAME))
    assert peak("Hey Jarvis, what time is it?") >= s.wake.threshold
    assert peak("The weather is lovely today and I would like some coffee") < 0.2
    assert peak("Hey Travis") < 0.2


@needs_piper
def test_real_playback_and_stop():
    sp = Speaker(PiperTTS(load_settings().tts))
    sp.say("Playback check.")
    assert sp.wait_idle(30) and not sp.errors
    sp.say("This is a longer sentence that we are going to interrupt almost immediately. " * 3)
    time.sleep(0.8)
    sp.stop()
    assert not sp.speaking


@needs_mic
def test_real_microphone_opens_streams_and_releases():
    s = load_settings()
    v = VoiceLoop(s, WakeWord(s.wake), Whisper(s.stt), types.SimpleNamespace(speaking=False, stop=lambda: None), lambda e: None, lambda t: None)
    s.wake.enabled = False  # exercise the stream only; no model load needed
    v.enable()
    for _ in range(60):  # device open takes a moment; the real device then delivers ~12.5 frames/s
        if v.frames_processed > 12 or v.last_error:
            break
        time.sleep(0.1)
    assert v.last_error is None, v.last_error
    assert v.state in ("listening", "recording") and v.frames_processed > 12
    v.disable()
    assert v.state == "off" and not v._thread.is_alive()
