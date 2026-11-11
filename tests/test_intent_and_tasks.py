"""Tests for centralized IntentRouter, multi-step TaskManager, clarifications, and one-shot PTT isolation."""
import time
import types
import numpy as np
import pytest

from jarvis.actions import TaskManager
from jarvis.api import create_app
from jarvis.voice import VoiceLoop, FRAME
from starlette.testclient import TestClient

H = {"X-Jarvis": "1"}


def test_task_manager_multi_step_lifecycle_and_cancel():
    tm = TaskManager()
    t = tm.create("Update local documentation", ["Validate root", "Index chunks", "Verify search"])
    assert t.status == "running" and len(t.steps) == 3

    tm.advance(t.id, 1, "running")
    cur = tm.get(t.id)
    assert cur["status"] == "running" and cur["current_step"] == 1

    cancelled = tm.cancel(t.id)
    assert cancelled.status == "cancelled"
    assert tm.is_cancelled(t.id) is True
    assert tm.cancel_active() == 0


def test_clarifying_prompt_on_ambiguous_model_switch(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    with TestClient(create_app(settings, assistant)) as c:
        r = c.post("/api/chat", json={"message": "switch to model"}, headers=H).json()
        assert r["command"] == "clarify_model" and r["ok"] is False
        assert "local" in r["answer"] and assistant.brain.seen == []


def test_intent_routing_for_history_and_stop(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    with TestClient(create_app(settings, assistant)) as c:
        assistant.history.add("user", "Earlier we discussed PostgreSQL connection pooling")
        assistant.history.add("assistant", "Yes, pgBouncer works nicely.")
        r = c.post("/api/chat", json={"message": "what did we discuss about postgresql"}, headers=H).json()
        assert r["command"] == "search_history" and r["ok"] and "PostgreSQL" in r["answer"]

        s = c.post("/api/chat", json={"message": "stop talking"}, headers=H).json()
        assert s["command"] == "stop" and s["ok"] is True


def test_push_to_talk_one_shot_does_not_clobber_continuous_config(settings, monkeypatch):
    import sounddevice as sd
    loud = (np.ones((FRAME, 1), dtype=np.float32) * 3500).astype(np.int16)
    silence = np.zeros((FRAME, 1), dtype=np.int16)

    class OneShotStream:
        instances = []

        def __init__(self, **k):
            self.reads = 0
            OneShotStream.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, n):
            self.reads += 1
            time.sleep(0.004)
            return (loud if 5 <= self.reads <= 18 else silence), False

    monkeypatch.setattr(sd, "InputStream", OneShotStream)
    settings.audio.voice_mode = "continuous"
    settings.wake.enabled = True
    settings.audio.speech_confirm_ms = 120
    settings.audio.silence_ms = 240
    heard = []
    ev = []
    v = VoiceLoop(
        settings,
        types.SimpleNamespace(load=lambda: None, score=lambda f: 0.0, reset=lambda: None),
        types.SimpleNamespace(transcribe=lambda a: {"text": "check status", "seconds": 0.1}),
        types.SimpleNamespace(speaking=False, stop=lambda: None),
        ev.append,
        heard.append,
    )
    v.push_to_talk()
    deadline = time.time() + 4.0
    while time.time() < deadline and not heard:
        time.sleep(0.03)
    time.sleep(0.15)
    v.disable()
    assert heard == ["check status"]
    # Settings must NOT have been mutated to "ptt" / wake disabled
    assert settings.audio.voice_mode == "continuous"
    assert settings.wake.enabled is True
