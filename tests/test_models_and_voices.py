"""Tests for runtime Piper voice switching, independent model switching, and gated cloud providers."""
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.brain import Brain, BrainError
from jarvis.config import ROOT, TTSSpec
from jarvis.tts import PiperTTS

H = {"X-Jarvis": "1"}


def test_piper_voice_discovery_switching_and_rate():
    voices = PiperTTS.list_voices()
    if not voices:
        pytest.skip("No Piper .onnx voices installed locally")
    spec = TTSSpec(voice=voices[0]["name"], speed=1.0)
    tts = PiperTTS(spec)
    assert tts.health()["installed_count"] == len(voices)

    switched = tts.switch_voice(voices[0]["name"])
    assert switched["name"] == voices[0]["name"]
    assert tts.set_rate(1.25) == 1.25
    with pytest.raises(ValueError):
        tts.set_rate(5.0)
    with pytest.raises(KeyError):
        tts.switch_voice("nonexistent-voice-xyz")


@pytest.mark.anyio
async def test_brain_cloud_adapter_blocked_in_local_mode(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testdummy12345")
    settings.providers.allow_cloud = False
    b = Brain(settings)
    with pytest.raises(BrainError, match="Local Mode"):
        await b.switch("openai:gpt-4o-mini")


@pytest.mark.anyio
async def test_brain_resolve_choice_aliases(fake_brain):
    assert await fake_brain.resolve_choice("thinking") == "local_thinking"
    assert await fake_brain.resolve_choice("fast") == "local"
    assert await fake_brain.resolve_choice("vision") in ("vision", None)


def test_voice_and_model_switching_are_independent_via_api_and_chat(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    voices = PiperTTS.list_voices()
    if not voices:
        pytest.skip("No Piper voices installed locally")
    vid = voices[0]["name"]

    with TestClient(create_app(settings, assistant)) as c:
        res = c.get("/api/voices").json()
        assert res["active"] == settings.tts.voice and len(res["voices"]) == len(voices)

        # Switch voice without touching brain model
        orig_model = assistant.brain.current
        v_cmd = c.post("/api/chat", json={"message": f"switch voice to {vid}"}, headers=H).json()
        assert v_cmd["command"] == "switch_voice" and v_cmd["ok"] is True
        assert assistant.brain.current == orig_model

        # Adjust rate
        rate_r = c.post("/api/voices/rate", json={"speed": 1.15}, headers=H).json()
        assert rate_r["speed"] == 1.15 and assistant.settings.tts.speed == 1.15

        # Switch model without changing voice
        m_cmd = c.post("/api/chat", json={"message": "switch to thinking"}, headers=H).json()
        assert m_cmd["command"] == "switch" and assistant.brain.current == "local_thinking"
        assert assistant.settings.tts.voice == vid
