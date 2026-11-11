"""Live integration: real Ollama + real qwen3:4b-instruct. Skipped (with reason) if Ollama is not running."""
import asyncio
import json

import httpx
import pytest

from conftest import needs_ollama
from jarvis.assistant import Assistant
from jarvis.brain import Brain
from jarvis.config import ROOT, load_settings
from jarvis.persona import build_messages

pytestmark = [pytest.mark.integration, needs_ollama]


def test_ollama_connectivity_and_models_installed():
    b = Brain(load_settings())
    h = asyncio.run(b.health())
    assert h["up"] and {"qwen3:4b-instruct", "qwen3:4b"} <= set(h["models"])


def test_live_chat_streaming_identity_and_no_reasoning_leak():
    async def go():
        b = Brain(load_settings())
        pieces = [p async for p in b.stream(build_messages([], "Who are you, and which company made you?", [], "2026-11-11"))]
        await b.client.aclose()
        return b, pieces
    b, pieces = asyncio.run(go())
    text = "".join(pieces)
    assert len(pieces) > 3, "reply must stream in several pieces"
    assert "jarvis" in text.lower() and "qwen" not in text.lower() and "alibaba" not in text.lower()
    assert not text.lower().lstrip().startswith(("okay, the user", "hmm", "<think")) and b.last_stats["tps"] > 5


def test_live_multi_turn_context(tmp_path, monkeypatch):
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = load_settings()
    s.notes_dir = str(tmp_path / "n")

    async def go():
        a = Assistant(s, data_dir=tmp_path)
        await a.start()
        await a.chat("My cat is called Biscuit. Please just acknowledge.")
        r = await a.chat("What is my cat called?")
        await a.stop()
        return r
    r = asyncio.run(go())
    assert "biscuit" in r["answer"].lower()


def test_live_model_switch_loads_and_unloads_in_ollama():
    async def go():
        b = Brain(load_settings())
        await b.warm()
        loaded1 = [m["name"] for m in await b.running()]
        await b.switch("local_thinking")
        await b.warm()
        loaded2 = [m["name"] for m in await b.running()]
        await b.switch("local")
        await b.unload("qwen3:4b")
        await b.client.aclose()
        return loaded1, loaded2
    l1, l2 = asyncio.run(go())
    assert "qwen3:4b-instruct" in l1 and l2 == ["qwen3:4b"]  # previous model was unloaded, VRAM freed
    with httpx.Client() as c:
        c.post("http://127.0.0.1:11434/api/generate", json={"model": "qwen3:4b-instruct", "keep_alive": 0})


def test_live_thinking_model_never_exposes_reasoning():
    async def go():
        b = Brain(load_settings())
        await b.switch("local_thinking")
        t = await b.complete(build_messages([], "Say hello in one short sentence.", [], "2026-11-11"))
        await b.unload("qwen3:4b")
        await b.switch("local")
        await b.client.aclose()
        return t
    t = asyncio.run(go())
    assert t and len(t) < 200 and "user" not in t.lower()


@pytest.mark.skipif(not (ROOT / "models" / "wake" / "hey_jarvis_v0.1.onnx").exists() or not (ROOT / "models" / "piper" / "en_GB-alan-medium.onnx").exists()
                    or not any((ROOT / "models" / "whisper").glob("models--*")), reason="speech models not downloaded")
def test_hands_free_end_to_end_wake_hear_think_speak(tmp_path, monkeypatch):
    """Synthetic 'Hey Jarvis, what is quantization?' fed as microphone audio -> real wake word, Whisper, retrieval, Ollama, Piper."""
    import threading
    import time
    import numpy as np
    import sounddevice as sd
    from jarvis.tts import PiperTTS
    from jarvis.wakeword import FRAME

    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = load_settings()
    s.audio.voice_mode = "wake_word"
    s.wake.enabled = True
    s.audio.silence_ms = 500
    tts = PiperTTS(s.tts)

    def pcm16k(text):
        sr, a = tts.synth(text)
        return np.interp(np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr)), np.arange(len(a)), a.astype(np.float32)).astype(np.int16)
    z = lambda sec: np.zeros(int(16000 * sec), np.int16)  # noqa: E731
    audio = np.concatenate([z(1.0), pcm16k("Hey Jarvis."), z(0.5), pcm16k("What is quantization?"), z(30)])
    pos = {"i": 0}
    played = []

    class Mic:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n):
            i = pos["i"]; pos["i"] += n
            time.sleep(0.004)
            return audio[i:i + n].reshape(-1, 1).copy(), False
    monkeypatch.setattr(sd, "InputStream", Mic)
    monkeypatch.setattr(sd, "play", lambda a, sr, device=None: played.append(len(a) / sr))
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "get_stream", lambda: type("S", (), {"active": False})())

    async def go():
        a = Assistant(s, data_dir=tmp_path)
        await a.start()
        q = a.bus.subscribe()
        t0 = time.time()
        a.voice.enable()
        events = []
        while time.time() - t0 < 120:
            ev = await asyncio.wait_for(q.get(), 120)
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
        await asyncio.sleep(1.0)
        a.voice.disable()
        a.speaker.wait_idle(30)
        await a.stop()
        return events, time.time() - t0
    events, secs = asyncio.run(go())
    types_ = [e["type"] for e in events]
    states = [e["state"] for e in events if e["type"] == "voice_state"]
    audio_states = [e["audio_state"] for e in events if e["type"] == "voice_state"]
    dedup_states = [s_ for i, s_ in enumerate(states) if i == 0 or s_ != states[i - 1]]
    assert dedup_states[:3] == ["listening", "recording", "transcribing"], states
    assert audio_states[:4] == ["WAKE_WORD", "LISTENING", "SPEECH_DETECTED", "PROCESSING"], audio_states
    user = next(e for e in events if e["type"] == "user")
    assert "quantization" in user["text"].lower(), user
    done = events[-1]
    assert done["type"] == "done" and done["answer"] and done["paths"], done
    assert any("Quantization" in p for p in done["paths"]) and played, (done, played)
    print(f"\nhands-free chain: heard {user['text']!r} -> {done['answer'][:80]!r}, spoke {len(played)} chunk(s), {secs:.1f}s total")


def test_ollama_endpoint_bound_to_loopback_only():
    out = __import__("subprocess").run(["powershell", "-NoProfile", "-Command",
                                        "(Get-NetTCPConnection -LocalPort 11434 -State Listen | Select -Expand LocalAddress) -join ','"],
                                       capture_output=True, text=True).stdout.strip()
    assert out, "Ollama is not listening"
    if not all(a in ("127.0.0.1", "::1") for a in out.split(",")):
        # Environment finding, not a Jarvis bug (Jarvis itself only talks to localhost). Reported, never hidden.
        pytest.xfail(f"Ollama listens on {out} (OLLAMA_HOST={__import__('os').environ.get('OLLAMA_HOST')}): reachable from LAN/Tailscale "
                     "without authentication. Set OLLAMA_HOST=127.0.0.1:11434 and restart Ollama to fix.")


def test_live_qwen_translation_and_tool_calling(tmp_path, monkeypatch):
    """Live Ollama test: local Qwen translation + Online Assisted Mode tool calling (search provider mocked)."""
    from jarvis import audit
    from jarvis.search import WebSearchService
    import httpx

    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)
    s = load_settings()
    s.notes_dir = str(tmp_path / "n")
    s.internet.enabled = True
    audit.set_online_enabled(True)

    def mock_search(req):
        html = '<a class="result__a" href="https://docs.python.org/3.14/">Python 3.14 Documentation</a><a class="result__snippet">Python 3.14 includes template strings and improved error messages.</a>'
        return httpx.Response(200, text=html)

    async def go():
        a = Assistant(s, data_dir=tmp_path, search_service=WebSearchService(s.internet, transport_factory=lambda target: httpx.MockTransport(mock_search)))
        await a.start()
        tr_en = await a.translator.translate_text("ഇന്ന് നല്ല ദിവസമാണ്.", "ml", "en")
        tr_ml = await a.translator.translate_text("Good morning, how are you?", "en", "ml")
        chat_res = await a.chat("What is the latest news on Python 3.14?")
        await a.stop()
        audit.set_online_enabled(False)
        return tr_en, tr_ml, chat_res

    tr_en, tr_ml, chat_res = asyncio.run(go())
    assert tr_en and ("good" in tr_en.lower() or "day" in tr_en.lower()), tr_en
    assert tr_ml and any("\u0d00" <= ch <= "\u0d7f" for ch in tr_ml), tr_ml  # contains Malayalam Unicode script
    assert chat_res["web_sources"] and chat_res["web_sources"][0]["url"] == "https://docs.python.org/3.14/", chat_res
    assert "3.14" in chat_res["answer"] or "python" in chat_res["answer"].lower(), chat_res
