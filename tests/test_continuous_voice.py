"""Tests for continuous hands-free conversation mode, audio state machine, barge-in, and recovery."""
import asyncio
import queue
import threading
import time
import numpy as np
import pytest
from fastapi.testclient import TestClient

from jarvis.api import create_app
from jarvis.assistant import Assistant
from jarvis.brain import BrainError
from jarvis.config import Settings, load_settings
from jarvis.voice import FRAME, SR, VoiceState


# ---- deterministic audio / mock helpers ---------------------------------------------------------------

def _tone(seconds: float, amp: int = 5500, freq: float = 440.0) -> np.ndarray:
    n = int(round(SR * seconds))
    t = np.arange(n, dtype=np.float32) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.int16)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(SR * seconds)), dtype=np.int16)


class ScriptedMic:
    """Thread-safe streamed audio source simulating live sounddevice.InputStream without sleeps."""

    def __init__(self, on_open=None, on_close=None):
        self._chunks: queue.Queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.int16)
        self._on_open = on_open
        self._on_close = on_close
        self.fail_on_read = False

    def push(self, pcm: np.ndarray):
        self._chunks.put(np.asarray(pcm, dtype=np.int16).reshape(-1))

    def push_error(self, exc: Exception):
        self._chunks.put(exc)

    def __enter__(self):
        # Match real PortAudio stream creation: audio occurring while stream was closed is not buffered
        self._buf = np.zeros(0, dtype=np.int16)
        while not self._chunks.empty():
            try:
                item = self._chunks.get_nowait()
                if isinstance(item, Exception):
                    self._chunks.put(item)
                    break
            except queue.Empty:
                break
        if self._on_open:
            self._on_open()
        return self

    def __exit__(self, *args):
        self._buf = np.zeros(0, dtype=np.int16)
        if self._on_close:
            self._on_close()
        return False

    def read(self, n: int):
        if self.fail_on_read:
            raise OSError("Simulated USB mic disconnect")
        while len(self._buf) < n:
            try:
                item = self._chunks.get(timeout=0.05)
                if isinstance(item, Exception):
                    raise item
                self._buf = np.concatenate([self._buf, item])
            except queue.Empty:
                # Deliver tiny silence slices so loop checks _run & state smoothly
                self._buf = np.concatenate([self._buf, np.zeros(n, dtype=np.int16)])
        out = self._buf[:n]
        self._buf = self._buf[n:]
        return out.reshape(-1, 1).copy(), False


class FakeBrain:
    def __init__(self, replies=None):
        self.current = "local"
        self.prompts: list[str] = []
        self.replies = list(replies or ["First response.", "Second response."])
        self.fail_next = False
        self.stream_delay_s = 0.0
        self.last_stats = {"ttft": 0.01, "tps": 50.0, "model": "qwen3:4b-instruct", "truncated": False}
        from jarvis.config import BrainSpec
        self.spec = BrainSpec(model="qwen3:4b-instruct")
        self.adhoc = {}
        self.client = type("C", (), {"aclose": self._aclose})()

    async def _aclose(self):
        return None

    async def warm(self):
        return None

    async def health(self):
        return {"up": True, "version": "0.40", "loaded": ["qwen3:4b-instruct"]}

    async def catalogue(self):
        return [{"key": "local", "model": "qwen3:4b-instruct", "installed": True, "thinking": False, "current": True}]

    async def stream(self, messages):
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        raw = user_msg.rsplit("\nUser: ", 1)[-1] if "\nUser: " in user_msg else user_msg
        self.prompts.append(raw)
        if self.fail_next:
            self.fail_next = False
            raise BrainError("Ollama unreachable")
        idx = len(self.prompts) - 1
        text = self.replies[idx] if idx < len(self.replies) else f"Reply {idx + 1}."
        for word in text.split(" "):
            if self.stream_delay_s:
                await asyncio.sleep(self.stream_delay_s)
            yield word + " "

    async def stream_events(self, messages, use_tools=False, tools=None, cancel_event=None):
        async for piece in self.stream(messages):
            if cancel_event is not None and cancel_event.is_set():
                yield {"type": "cancelled", "stats": {}}
                return
            yield {"type": "token", "text": piece}
        yield {"type": "stats", "stats": dict(self.last_stats)}


def _fast_settings(tmp_path) -> Settings:
    s = load_settings()
    (tmp_path / "notes").mkdir(exist_ok=True)
    s.notes_dir = str(tmp_path / "notes")
    s.internet.enabled = False
    s.audio.voice_mode = "continuous"
    s.audio.duplex_mode = "half_duplex"
    s.audio.suppress_mic_during_tts = True
    s.audio.use_silero_vad = False  # deterministic RMS path for fast unit verification
    s.audio.speech_confirm_ms = 160
    s.audio.silence_ms = 240
    s.audio.min_utterance_ms = 160
    s.audio.pre_roll_ms = 240
    s.audio.mic_resume_delay_ms = 40
    s.audio.post_tts_cooldown_ms = 40
    s.audio.no_speech_timeout_s = 1.5
    s.wake.enabled = False
    s.tts.enabled = True
    return s


# ---- 1-7: continuous two-turn lifecycle, single response, TTS wait, auto restart ----------------------

def test_continuous_full_two_turn_lifecycle_and_auto_restart(tmp_path, monkeypatch):
    """Covers scenarios 1-7 + half-duplex suspension & auto-reopen:
    start continuous -> capture 1st utterance -> suspend/release mic during processing/TTS ->
    one response -> Piper playback -> wait until TTS ends -> reopen mic & resume LISTENING ->
    process 2nd utterance without clicking anything.
    """
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["Copy that, turning lights off.", "Done, status is nominal."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)

    captured_audios: list[np.ndarray] = []
    stt_texts = iter(["turn off the lights", "check system status"])
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: (
        captured_audios.append(audio.copy()),
        {
            "text": next(stt_texts),
            "detected_language": "en",
            "language_probability": 0.99,
            "confidence": 0.95,
            "seconds": round(len(audio) / SR, 2),
            "model": "small",
            "beam_size": 3,
            "segments": [],
        },
    )[1])

    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(1600, dtype=np.int16)))
    import sounddevice as sd
    open_calls = []
    stream_balance = {"active": 0}
    mic = ScriptedMic(
        on_open=lambda: stream_balance.__setitem__("active", stream_balance["active"] + 1),
        on_close=lambda: stream_balance.__setitem__("active", stream_balance["active"] - 1),
    )
    monkeypatch.setattr(sd, "InputStream", lambda **kw: (open_calls.append(kw), mic)[1])

    stream_active = {"v": False}
    play_gate = threading.Event()
    tts_play_started = threading.Event()
    timeline: list[tuple[str, float]] = []

    def fake_play(audio, sr, device=None):
        stream_active["v"] = True
        timeline.append(("tts_start", time.monotonic()))
        tts_play_started.set()

    def fake_stop():
        if stream_active["v"]:
            timeline.append(("tts_end", time.monotonic()))
            stream_active["v"] = False
            play_gate.set()

    def fake_get_stream():
        active = stream_active["v"] and not play_gate.is_set()
        if stream_active["v"] and play_gate.is_set():
            stream_active["v"] = False
            timeline.append(("tts_end", time.monotonic()))
        return type("S", (), {"active": active})()

    monkeypatch.setattr(sd, "play", fake_play)
    monkeypatch.setattr(sd, "stop", fake_stop)
    monkeypatch.setattr(sd, "get_stream", fake_get_stream)

    async def scenario():
        await a.start()
        try:
            q = a.bus.subscribe()
            state_seq: list[str] = []
            orig_pub = a.bus.publish

            def rec_pub(ev):
                if ev.get("type") == "voice_state":
                    st = ev["audio_state"]
                    state_seq.append(st)
                    timeline.append((f"state:{st}", time.monotonic()))
                return orig_pub(ev)

            monkeypatch.setattr(a.bus, "publish", rec_pub)
            monkeypatch.setattr(a.voice, "emit", rec_pub)

            async def collect_until(target_done_count: int):
                dones = 0
                while dones < target_done_count:
                    ev = await asyncio.wait_for(q.get(), 10)
                    if ev["type"] == "done":
                        dones += 1

            # 1. Start continuous mode once
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)
            assert len(open_calls) == 1 and stream_balance["active"] == 1

            # 2-5. First spoken utterance (pre-roll silence + voiced speech + trailing silence)
            mic.push(np.concatenate([_silence(0.24), _tone(0.32), _silence(0.40)]))
            assert await asyncio.to_thread(tts_play_started.wait, 5.0)
            # In Safe Half-Duplex mode, hardware mic MUST be suspended & closed during SPEAKING
            await asyncio.sleep(0.06)
            assert stream_balance["active"] == 0, "Half-duplex must release mic stream during TTS playback"
            assert a.voice.mic_suspended is True
            assert a.voice.audio_state == VoiceState.SPEAKING

            # Release Piper playback -> must reopen mic & resume LISTENING automatically ONLY AFTER tts_end
            play_gate.set()
            await collect_until(1)
            deadline = time.monotonic() + 5.0
            while (a.voice.audio_state != VoiceState.LISTENING or sum(1 for k, _ in timeline if k == "state:LISTENING") < 2) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert a.voice.audio_state == VoiceState.LISTENING
            assert len(open_calls) == 2 and stream_balance["active"] == 1

            t_tts_end = [t for k, t in timeline if k == "tts_end"][0]
            t_resumed = [t for k, t in timeline if k == "state:LISTENING"][-1]
            assert t_resumed >= t_tts_end

            play_gate.clear()
            tts_play_started.clear()

            # 7. Feed second utterance WITHOUT clicking anything
            mic.push(np.concatenate([_silence(0.24), _tone(0.32), _silence(0.40)]))
            assert await asyncio.to_thread(tts_play_started.wait, 5.0)
            play_gate.set()
            await collect_until(1)
            deadline = time.monotonic() + 5.0
            while a.voice.audio_state != VoiceState.LISTENING and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

            a.voice.stop_session()
            assert stream_balance["active"] == 0
            return state_seq
        finally:
            await a.stop()

    state_seq = asyncio.run(scenario())

    assert len(open_calls) == 3, "Mic opens cleanly per listening cycle (Turn 1, Turn 2, Resume after Turn 2)"
    assert len(captured_audios) == 2
    assert len(captured_audios[0]) > int(0.4 * SR), "Pre-roll + utterance frames should be preserved"
    assert len(brain.prompts) == 2
    assert [m["role"] for m in a.history.items] == ["user", "assistant", "user", "assistant"]
    assert VoiceState.LISTENING.value in state_seq
    assert VoiceState.SPEECH_DETECTED.value in state_seq
    assert VoiceState.PROCESSING.value in state_seq
    assert VoiceState.GENERATING.value in state_seq
    assert VoiceState.SPEAKING.value in state_seq
    assert VoiceState.RESUMING.value in state_seq
    assert a.voice.audio_state == VoiceState.IDLE


# ---- 8: stop cleanly + never restart after stop -------------------------------------------------------

def test_stop_continuous_mode_and_no_restart_after_stop(tmp_path, monkeypatch):
    """Covers scenario 8 & prevents accidental mic restart after Stop."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    a = Assistant(s, data_dir=tmp_path, brain=FakeBrain())
    import sounddevice as sd
    mic = ScriptedMic()
    counts = {"opened": 0, "closed": 0, "sd_stop": 0}
    mic._on_open = lambda: counts.__setitem__("opened", counts["opened"] + 1)
    mic._on_close = lambda: counts.__setitem__("closed", counts["closed"] + 1)
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "stop", lambda: counts.__setitem__("sd_stop", counts["sd_stop"] + 1))

    async def go():
        await a.start()
        try:
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            # Simulate valid PROCESSING -> SPEAKING turn transition before stop
            a.voice._active_utt_id = 7
            a.voice._transition(VoiceState.PROCESSING, reason="test")
            a.voice.mark_speaking(7)
            assert a.voice.audio_state == VoiceState.SPEAKING

            a.voice.stop_session()
            assert a.voice.audio_state == VoiceState.IDLE

            # Late complete_turn callback arriving after Stop must NOT restart LISTENING!
            a.voice.complete_turn(7)
            assert a.voice.audio_state == VoiceState.IDLE
            assert a.voice.state == "off"
        finally:
            await a.stop()

    asyncio.run(go())
    assert counts["opened"] == 1 and counts["closed"] == 1
    assert counts["sd_stop"] >= 1


# ---- 9: pause and resume ------------------------------------------------------------------------------

def test_pause_and_resume_behavior(tmp_path, monkeypatch):
    """Covers scenario 9: Pause ignores incoming speech and stops TTS; Resume restores LISTENING."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["Resumed and heard you."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)
    heard = []
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: (
        heard.append(len(audio)),
        {"text": "after resume", "detected_language": "en", "language_probability": 0.99, "confidence": 0.95, "seconds": 0.4, "model": "small", "beam_size": 3, "segments": []},
    )[1])
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(400, dtype=np.int16)))
    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            a.voice.pause()
            assert a.voice.paused is True
            assert a.voice.audio_state == VoiceState.PAUSED

            # Speech during PAUSED must be discarded
            mic.push(np.concatenate([_tone(0.4), _silence(0.4)]))
            await asyncio.sleep(0.15)
            assert len(heard) == 0
            assert a.voice.audio_state == VoiceState.PAUSED

            # Resume -> LISTENING -> accepts next speech
            a.voice.resume()
            assert a.voice.paused is False
            assert a.voice.audio_state == VoiceState.LISTENING
            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.4)]))
            deadline = time.monotonic() + 5.0
            while not brain.prompts and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert brain.prompts == ["after resume"]
        finally:
            await a.stop()

    asyncio.run(go())


# ---- 10: barge-in interrupts TTS & preserves triggering speech; speaker fallback mutes ----------------

def test_barge_in_interrupts_tts_and_preserves_new_speech(tmp_path, monkeypatch):
    """Covers scenario 10: Verified full-duplex barge-in during SPEAKING stops Piper, cancels old LLM, preserves barge-in speech."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    s.audio.duplex_mode = "full_duplex"
    s.audio.barge_in_enabled = True
    s.audio.barge_in_sensitivity = "high"  # 160ms confirm, low rms threshold
    s.audio.suppress_mic_during_tts = False
    brain = FakeBrain(replies=["Long initial answer that takes a while to speak aloud.", "Interrupt handled."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)
    a.aec.verified = True  # Explicit AEC verification enables full-duplex barge-in

    transcribed = []
    texts = iter(["tell me a long story", "stop wait check notes"])
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: (
        transcribed.append(audio.copy()),
        {"text": next(texts), "detected_language": "en", "language_probability": 0.99, "confidence": 0.96, "seconds": round(len(audio) / SR, 2), "model": "small", "beam_size": 3, "segments": []},
    )[1])
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(1600, dtype=np.int16)))

    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    stream_active = {"v": False}
    play_gate = threading.Event()
    tts_started = threading.Event()
    stops = {"n": 0}

    def fake_play(audio, sr, device=None):
        stream_active["v"] = True
        tts_started.set()

    def fake_stop():
        stops["n"] += 1
        if stream_active["v"]:
            stream_active["v"] = False
            play_gate.set()

    monkeypatch.setattr(sd, "play", fake_play)
    monkeypatch.setattr(sd, "stop", fake_stop)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": stream_active["v"] and not play_gate.is_set()})())

    async def go():
        await a.start()
        try:
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            # Turn 1 triggers slow-to-finish TTS
            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.32)]))
            assert await asyncio.to_thread(tts_started.wait, 5.0)
            while a.voice.audio_state != VoiceState.SPEAKING:
                await asyncio.sleep(0.01)

            # User barges in with louder speech (amp=12000 > high barge threshold) + trailing silence
            mic.push(np.concatenate([_tone(0.32, amp=12000), _silence(0.40)]))

            # Wait until Turn 2 prompt reaches FakeBrain!
            deadline = time.monotonic() + 6.0
            while len(brain.prompts) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

            assert brain.prompts == ["tell me a long story", "stop wait check notes"]
            assert stops["n"] >= 1, "sd.stop must be called immediately on barge-in"
            assert len(transcribed[1]) >= int(0.24 * SR), "Barge-in frames must be preserved in 2nd utterance"
        finally:
            await a.stop()

    asyncio.run(go())


def test_speaker_fallback_suppress_mic_during_tts_prevents_self_trigger(tmp_path, monkeypatch):
    """In safe half-duplex (or unverified AEC fallback), loud speaker bleed during SPEAKING must NEVER self-trigger."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    s.audio.duplex_mode = "half_duplex"
    s.audio.suppress_mic_during_tts = True
    s.audio.barge_in_enabled = True
    brain = FakeBrain(replies=["Finished without self-interruption."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: {"text": "hello", "detected_language": "en", "language_probability": 0.99, "confidence": 0.9, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []})
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(800, dtype=np.int16)))

    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    play_gate = threading.Event()
    tts_started = threading.Event()
    stream_active = {"v": False}

    def fake_play(audio, sr, device=None):
        stream_active["v"] = True
        tts_started.set()

    def fake_stop():
        if stream_active["v"]:
            stream_active["v"] = False
            play_gate.set()

    monkeypatch.setattr(sd, "play", fake_play)
    monkeypatch.setattr(sd, "stop", fake_stop)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": stream_active["v"] and not play_gate.is_set()})())

    async def go():
        await a.start()
        try:
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.32)]))
            assert await asyncio.to_thread(tts_started.wait, 5.0)
            while a.voice.audio_state != VoiceState.SPEAKING:
                await asyncio.sleep(0.01)

            # Feed loud loudspeaker bleed while SPEAKING -> mic is suspended & discarded!
            mic.push(_tone(0.32, amp=15000))
            await asyncio.sleep(0.12)
            assert a.voice.audio_state == VoiceState.SPEAKING
            assert a.voice.mic_suspended is True
            assert len(brain.prompts) == 1

            play_gate.set()
            deadline = time.monotonic() + 3.0
            while a.voice.audio_state != VoiceState.LISTENING and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert len(brain.prompts) == 1
        finally:
            await a.stop()

    asyncio.run(go())


# ---- 11: duplicate utterance prevention + API session endpoints + PTT + Wake follow-up ----------------

def test_prevent_duplicate_utterance_and_api_session_endpoints(tmp_path, monkeypatch):
    """Covers scenario 11 & API /api/voice/session controls + SSE hello payload."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["Processed once."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: {"text": "unique command", "detected_language": "en", "language_probability": 0.99, "confidence": 0.95, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []})
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(200, dtype=np.int16)))

    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    # 1. Duplicate utt_id guard inside Assistant._drain_voice_queue
    async def dup_check():
        await a.start()
        a._last_utt_id = 5
        a.voice.utterance_queue.put_nowait((5, np.ones(3200, dtype=np.float32)))
        a.voice.utterance_queue.put_nowait((4, np.ones(3200, dtype=np.float32)))
        await a._drain_voice_queue()
        assert brain.prompts == []  # stale/duplicate IDs never processed
        await a.stop()

    asyncio.run(dup_check())

    # 2. HTTP /api/voice/session lifecycle + status
    app = create_app(s, assistant=Assistant(s, data_dir=tmp_path, brain=FakeBrain()))
    app.state.a.voice._loop  # present
    hdr = {"X-Jarvis": "1"}
    with TestClient(app) as client:
        r = client.post("/api/voice/session", json={"action": "start", "mode": "continuous"}, headers=hdr)
        assert r.status_code == 200 and r.json()["audio_state"] == "LISTENING"
        assert r.json()["mode"] == "continuous"

        r = client.post("/api/voice/session", json={"action": "pause"}, headers=hdr)
        assert r.json()["audio_state"] == "PAUSED" and r.json()["paused"] is True

        r = client.post("/api/voice/session", json={"action": "resume"}, headers=hdr)
        assert r.json()["audio_state"] == "LISTENING" and r.json()["paused"] is False

        r = client.post("/api/voice/session", json={"action": "mode", "mode": "ptt"}, headers=hdr)
        assert r.json()["mode"] == "ptt"

        r = client.post("/api/voice/interrupt", headers=hdr)
        assert r.status_code == 200 and r.json()["ok"] is True

        st = client.get("/api/status").json()["voice"]
        assert {"state", "audio_state", "mode", "paused", "barge_in_enabled", "suppress_mic_during_tts"} <= set(st)

        r = client.post("/api/voice/session", json={"action": "stop"}, headers=hdr)
        assert r.json()["audio_state"] == "IDLE" and r.json()["state"] == "off"


def test_ptt_single_turn_and_wake_word_followup_modes(tmp_path, monkeypatch):
    """Mode A (ptt) stops after 1 turn; Mode C (wake_word) opens follow-up LISTENING then returns to WAKE_WORD."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    s.audio.voice_mode = "ptt"
    a = Assistant(s, data_dir=tmp_path, brain=FakeBrain(replies=["PTT reply.", "Wake reply."]))
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: {"text": "test utterance", "detected_language": "en", "language_probability": 0.99, "confidence": 0.95, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []})
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(200, dtype=np.int16)))
    monkeypatch.setattr(a.wake, "load", lambda: None)
    wake_fired = {"done": False}
    def smart_wake_score(frame):
        if wake_fired["done"]:
            return 0.0
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        if rms > 3000.0:
            wake_fired["done"] = True
            return 0.95
        return 0.0
    monkeypatch.setattr(a.wake, "score", smart_wake_score)
    monkeypatch.setattr(type(a.wake), "ready", property(lambda self: True), raising=False)

    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            # Mode A: PTT -> 1 utterance -> returns to IDLE
            a.voice.push_to_talk()
            deadline = time.monotonic() + 3.0
            while a.voice.audio_state not in (VoiceState.LISTENING, VoiceState.SPEECH_DETECTED) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            mic.push(np.concatenate([_tone(0.32), _silence(0.40)]))
            deadline = time.monotonic() + 5.0
            while a.voice.audio_state != VoiceState.IDLE and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert a.voice.audio_state == VoiceState.IDLE

            # Mode C: Wake Word -> wake trigger -> command -> follow-up window -> returns to WAKE_WORD
            s.audio.wake_followup_s = 0.25
            a.voice.start_session("wake_word")
            while a.voice.audio_state != VoiceState.WAKE_WORD:
                await asyncio.sleep(0.01)
            mic.push(np.concatenate([_tone(0.08, amp=9000), _silence(0.24), _tone(0.32), _silence(0.40)]))
            deadline = time.monotonic() + 5.0
            while len(a.brain.prompts) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            deadline = time.monotonic() + 3.0
            while a.voice.audio_state != VoiceState.WAKE_WORD and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert a.voice.audio_state == VoiceState.WAKE_WORD
        finally:
            await a.stop()

    asyncio.run(go())


# ---- 12-15: error handling & recovery (Mic disconnect, Whisper, Ollama, Piper) ------------------------

def test_microphone_disconnect_transitions_to_error_and_releases_stream(tmp_path, monkeypatch):
    """Covers scenario 12: Mic disconnect during active listening sets ERROR and releases InputStream."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    a = Assistant(s, data_dir=tmp_path, brain=FakeBrain())
    import sounddevice as sd
    mic = ScriptedMic()
    closed = threading.Event()
    mic._on_close = closed.set
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)

    async def go():
        await a.start()
        q = a.bus.subscribe()
        a.voice.start_session("continuous")
        while a.voice.audio_state != VoiceState.LISTENING:
            await asyncio.sleep(0.01)

        mic.push_error(RuntimeError("Device disconnected"))
        events = []
        deadline = time.monotonic() + 5.0
        while a.voice.audio_state != VoiceState.ERROR and time.monotonic() < deadline:
            try:
                events.append(await asyncio.wait_for(q.get(), 0.2))
            except asyncio.TimeoutError:
                pass
        assert a.voice.audio_state == VoiceState.ERROR
        assert "disconnected" in (a.voice.last_error or "").lower()
        assert closed.wait(timeout=2.0)
        await a.stop()

    asyncio.run(go())


def test_whisper_error_recovery_returns_to_listening(tmp_path, monkeypatch):
    """Covers scenario 13: Whisper exception reports error and resumes LISTENING for next utterance."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["Recovered after STT glitch."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)

    calls = {"n": 0}
    def flaky_stt(audio, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Whisper decode crashed")
        return {"text": "second try", "detected_language": "en", "language_probability": 0.99, "confidence": 0.9, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []}

    monkeypatch.setattr(a.stt, "transcribe", flaky_stt)
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(200, dtype=np.int16)))
    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            q = a.bus.subscribe()
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            err_seen = False
            deadline = time.monotonic() + 5.0
            while not err_seen and time.monotonic() < deadline:
                ev = await asyncio.wait_for(q.get(), 2.0)
                if ev["type"] == "error" and "transcription failed" in ev["message"]:
                    err_seen = True
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            deadline = time.monotonic() + 5.0
            while not brain.prompts and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert err_seen and brain.prompts == ["second try"]
        finally:
            await a.stop()

    asyncio.run(go())


def test_ollama_error_recovery_returns_to_listening(tmp_path, monkeypatch):
    """Covers scenario 14: Ollama error reports cleanly and continuous loop resumes LISTENING."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["First ignored", "Ollama recovered."])
    brain.fail_next = True
    a = Assistant(s, data_dir=tmp_path, brain=brain)

    texts = iter(["first attempt", "second attempt"])
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: {"text": next(texts), "detected_language": "en", "language_probability": 0.99, "confidence": 0.9, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []})
    monkeypatch.setattr(a.tts, "synth", lambda text: (16000, np.ones(200, dtype=np.int16)))
    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            q = a.bus.subscribe()
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            err_seen = False
            deadline = time.monotonic() + 5.0
            while not err_seen and time.monotonic() < deadline:
                ev = await asyncio.wait_for(q.get(), 2.0)
                if ev["type"] == "error" and "unreachable" in ev["message"].lower():
                    err_seen = True
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            deadline = time.monotonic() + 5.0
            while len(brain.prompts) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert err_seen and brain.prompts == ["first attempt", "second attempt"]
        finally:
            await a.stop()

    asyncio.run(go())


def test_piper_error_recovery_returns_to_listening(tmp_path, monkeypatch):
    """Covers scenario 15: Piper synth error surfaces via bus, wait_idle completes without hang, loop returns to LISTENING."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    brain = FakeBrain(replies=["Reply one.", "Reply two."])
    a = Assistant(s, data_dir=tmp_path, brain=brain)

    texts = iter(["one", "two"])
    monkeypatch.setattr(a.stt, "transcribe", lambda audio, **kw: {"text": next(texts), "detected_language": "en", "language_probability": 0.99, "confidence": 0.9, "seconds": 0.3, "model": "small", "beam_size": 3, "segments": []})

    synth_calls = {"n": 0}
    def flaky_synth(text):
        synth_calls["n"] += 1
        if synth_calls["n"] == 1:
            raise RuntimeError("Missing Piper ONNX")
        return 16000, np.ones(200, dtype=np.int16)

    monkeypatch.setattr(a.tts, "synth", flaky_synth)
    import sounddevice as sd
    mic = ScriptedMic()
    monkeypatch.setattr(sd, "InputStream", lambda **kw: mic)
    monkeypatch.setattr(sd, "play", lambda *a, **kw: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            q = a.bus.subscribe()
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            piper_err = False
            deadline = time.monotonic() + 5.0
            while not piper_err and time.monotonic() < deadline:
                ev = await asyncio.wait_for(q.get(), 2.0)
                if ev["type"] == "error" and "piper" in ev["message"].lower():
                    piper_err = True
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)

            mic.push(np.concatenate([_silence(0.16), _tone(0.32), _silence(0.40)]))
            deadline = time.monotonic() + 5.0
            while len(brain.prompts) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert piper_err and brain.prompts == ["one", "two"]
        finally:
            await a.stop()

    asyncio.run(go())


# ---- 16: zero leaked streams / threads after shutdown -------------------------------------------------

def test_no_active_streams_or_voice_threads_leak_on_shutdown(tmp_path, monkeypatch):
    """Covers scenario 16: multiple start/stop cycles and shutdown leave 0 open streams and 0 voice-loop threads."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    a = Assistant(s, data_dir=tmp_path, brain=FakeBrain())
    import sounddevice as sd
    active = {"in": 0}
    sd_stops = {"n": 0}

    def open_fn():
        active["in"] += 1

    def close_fn():
        active["in"] -= 1

    monkeypatch.setattr(sd, "InputStream", lambda **kw: ScriptedMic(on_open=open_fn, on_close=close_fn))
    monkeypatch.setattr(sd, "stop", lambda: sd_stops.__setitem__("n", sd_stops["n"] + 1))

    async def go():
        await a.start()
        for _ in range(3):
            a.voice.start_session("continuous")
            while a.voice.audio_state != VoiceState.LISTENING:
                await asyncio.sleep(0.01)
            a.voice.stop_session()
            assert a.voice.audio_state == VoiceState.IDLE
            assert active["in"] == 0
        await a.stop()

    asyncio.run(go())
    voice_threads = [t for t in threading.enumerate() if t.name == "voice-loop" and t.is_alive()]
    assert voice_threads == []
    assert active["in"] == 0
    assert sd_stops["n"] >= 3


# ---- Part 7: Echo Prevention, Streaming LLM->TTS, Sentence Chunker, Bluetooth & Queue Bounds ---------

def test_streaming_speech_buffer_english_malayalam_and_code():
    """Verify StreamingSpeechBuffer splits at natural English/Malayalam punctuation, keeps abbreviations/decimals,
    suppresses code fences, and flushes incomplete final tails."""
    from jarvis.tts import StreamingSpeechBuffer

    buf = StreamingSpeechBuffer(min_chunk_chars=18, soft_max_chars=120, max_chunk_chars=200)
    tokens = [
        "Dr. Allen measured ", "3.14 volts today. ",
        "Here is Python:\n```python\nprint('hi')\n```\n",
        "ഇത് മലയാളം വാക്യമാണ്. ",
        "Final partial clause without dot"
    ]
    out = []
    for tok in tokens:
        out.extend(buf.feed(tok))
    out.extend(buf.flush())

    assert any("Dr. Allen measured 3.14 volts today." in c for c in out), out
    assert any("code is on your screen" in c for c in out), out
    assert not any("print(" in c for c in out), out
    assert any("ഇത് മലയാളം വാക്യമാണ്." in c for c in out), out
    assert out[-1] == "Final partial clause without dot", out


def test_streaming_llm_starts_speaking_before_generation_completes(tmp_path, monkeypatch):
    """Critical Part 2 & 3 check: Sentence 1 must enter Piper synthesis & play WHILE Qwen is still generating,
    chunks play strictly in order 1->2->3, final chunk flushes cleanly, and SPEAKING waits for playback start."""
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = _fast_settings(tmp_path)
    a = Assistant(s, data_dir=tmp_path)

    release_rest = asyncio.Event()
    gen_states = {"finished": False}

    class GatedStreamBrain(FakeBrain):
        async def stream(self, messages):
            self.prompts.append("gated")
            yield "Here is the first complete sentence right away. "
            await asyncio.wait_for(release_rest.wait(), timeout=5.0)
            yield "Second sentence streams cleanly afterward. "
            yield "And trailing final words without punctuation"
            gen_states["finished"] = True

    a.brain = GatedStreamBrain()
    synthesized: list[str] = []
    played_chunks: list[int] = []
    tts_first_played = threading.Event()

    def fake_synth(text):
        synthesized.append(text)
        return 16000, np.ones(800, dtype=np.int16) * (len(synthesized) + 1)

    monkeypatch.setattr(a.tts, "synth", fake_synth)
    import sounddevice as sd
    monkeypatch.setattr(sd, "stop", lambda: None)

    def rec_play(audio, sr, device=None):
        played_chunks.append(int(audio[0]))
        tts_first_played.set()

    monkeypatch.setattr(sd, "play", rec_play)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        await a.start()
        try:
            evs = []
            async def consume():
                async for ev in a.stream_turn("explain streaming", speak=True):
                    evs.append(ev)

            task = asyncio.create_task(consume())
            assert await asyncio.to_thread(tts_first_played.wait, 5.0)
            assert gen_states["finished"] is False
            assert played_chunks == [2]

            release_rest.set()
            await task
            await asyncio.to_thread(a.speaker.wait_idle, 5.0)
            return evs
        finally:
            await a.stop()

    asyncio.run(go())
    assert synthesized == [
        "Here is the first complete sentence right away.",
        "Second sentence streams cleanly afterward.",
        "And trailing final words without punctuation",
    ]
    assert played_chunks == [2, 3, 4], f"Chunks must play strictly in order, got {played_chunks}"


def test_unverified_full_duplex_falls_back_to_half_duplex_and_nlms_aec(tmp_path):
    """Full-duplex with unverified speaker/mic MUST safely fall back to half-duplex; NLMS attenuates aligned echo."""
    from jarvis.aec import EchoReferenceBuffer, NLMSEchoCanceller

    s = _fast_settings(tmp_path)
    s.audio.duplex_mode = "full_duplex"
    s.audio.suppress_mic_during_tts = False
    s.audio.echo_cancellation = "auto"
    a = Assistant(s, data_dir=tmp_path, brain=FakeBrain())
    info = a.voice.duplex_info()
    assert info["requested"] == "full_duplex"
    assert info["effective"] == "half_duplex"
    assert info["fallback"] is True
    assert info["barge_in_available"] is False

    aec = NLMSEchoCanceller(filter_taps=64, mu=0.5, min_erle_db=8.0)
    rng = np.random.default_rng(7)
    far = (rng.standard_normal(1600) * 0.4).astype(np.float32)
    near = np.zeros_like(far)
    near[2:] = 0.5 * far[:-2] + 0.25 * np.pad(far[:-3], (1, 0))
    erle_db = aec.calibrate(near, far)
    assert erle_db >= 8.0 and aec.verified is True

    ref = EchoReferenceBuffer(seconds=1.0)
    ref.push_playback((far * 32767).astype(np.int16), 16000)
    assert ref.is_playback_recent() and len(ref.snapshot(512)) == 512


def test_bluetooth_device_handling_and_tts_queue_overflow(tmp_path):
    """Verify Bluetooth name resolution, PortAudio reconnect/default fallback on play error, and bounded queue drop."""
    from jarvis.tts import PiperTTS, Speaker, _resolve_output_device
    from jarvis.voice import _resolve_input_device

    fake_devs = [
        {"name": "Microphone Array (Realtek)", "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 44100.0},
        {"name": "Headphones (CHARGE MINI )", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100.0},
        {"name": "Speaker (Realtek)", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
    ]
    fake_sd = type("FakeSD", (), {
        "query_devices": staticmethod(lambda idx=None, kind=None: fake_devs[idx] if idx is not None else fake_devs),
        "_terminate": staticmethod(lambda: None),
        "_initialize": staticmethod(lambda: None),
        "play": staticmethod(lambda *a, **kw: None),
        "stop": staticmethod(lambda: None),
        "get_stream": staticmethod(lambda: type("S", (), {"active": False})()),
    })()

    assert _resolve_input_device(fake_sd, "Realtek") == 0
    assert _resolve_output_device(fake_sd, "CHARGE MINI") == 1
    assert _resolve_output_device(fake_sd, "Nonexistent BT") is None

    calls = []
    def bt_flaky_play(audio, sr, device=None):
        calls.append(device)
        if device == 1:
            raise OSError("Bluetooth A2DP sink disconnected")

    s = _fast_settings(tmp_path)
    s.tts.output_device = 1
    sp = Speaker(PiperTTS(s.tts))
    fake_sd.play = staticmethod(bt_flaky_play)
    sp._play_with_bt_recovery(fake_sd, np.ones(100, dtype=np.int16), 16000)
    assert calls == [1, 1, None], "Must retry BT device after PortAudio refresh then fall back to default output"

    sp2 = Speaker(PiperTTS(s.tts))
    sp2._started = True  # hold workers so 33rd chunk tests maxsize=32 overflow guard cleanly
    queued = 0
    for i in range(33):
        queued += sp2.say_chunk(f"Sentence number {i}.", precleaned=True)
    assert queued == 32 and sp2._pending == 32
    sp2.stop()
    assert sp2._pending == 0

