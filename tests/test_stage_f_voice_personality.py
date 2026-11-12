"""Stage F tests: Multilingual Voice Safety, Path Preservation, & Orthogonal Personality Presets."""
from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from jarvis.api import create_app
from jarvis.persona import PERSONALITY_PRESETS, build_messages, system_prompt
from jarvis.security.outcomes import HONEST_OUTCOME_RULE
from jarvis.security.untrusted import UNTRUSTED_DATA_POLICY
from jarvis.translator import guard_correction

H = {"X-Jarvis": "1"}


def test_voice_uncertainty_clarification_blocks_unsafe_action_and_preserves_paths(assistant):
    # 1. Path & tech term preservation in transcript correction guard
    raw = r"open file C:\Projects\ERP_V2\backend\config.py"
    cleaned, accepted, flags = guard_correction(raw, r"Open file C:\Projects\ERP_V2\backend\config.py.")
    assert accepted is True
    assert r"C:\Projects\ERP_V2\backend\config.py" in cleaned

    # Hallucinated path drop is rejected cleanly
    kept, ok2, flags2 = guard_correction(raw, "Open file in desktop folder.")
    assert ok2 is False
    assert kept == raw

    # 2. Uncertain low-confidence voice transcript must ask clarification before running destructive command
    seen_turns: list[str] = []

    async def _fake_stream(text, **kw):
        seen_turns.append(text)
        yield {"type": "done", "answer": "executed"}

    assistant.stream_turn = _fake_stream
    since = assistant.bus.latest_seq
    asyncio.run(
        assistant._voice_turn(
            {
                "text": "delete all workspace files",
                "language": "ml",
                "detected_language": "ml",
                "confidence": 0.25,
                "recognition_quality": "low",
                "review_needed": True,
                "quality_flags": ["uncertain_transcript"],
            }
        )
    )
    events, _ = assistant.bus.events_since(since)

    assert seen_turns == []
    assert any(ev.get("type") == "clarification_required" for ev in events)

    # Clear read-only query even if review_needed is high-safe OR high confidence executes normally
    seen_turns.clear()
    asyncio.run(
        assistant._voice_turn(
            {
                "text": "check system status",
                "language": "en",
                "detected_language": "en",
                "confidence": 0.92,
                "recognition_quality": "high",
                "review_needed": False,
            }
        )
    )
    assert seen_turns == ["check system status"]


def test_orthogonal_kv_cache_personality_presets_and_api(assistant, settings, monkeypatch):
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda *a, **kw: None)
    client = TestClient(create_app(settings, assistant=assistant))

    # Every preset preserves security & honesty boundaries and stays deterministic for KV cache reuse
    for preset in PERSONALITY_PRESETS:
        p1 = system_prompt(online=False, personality_preset=preset)
        p2 = system_prompt(online=False, personality_preset=preset)
        assert p1 == p2
        assert HONEST_OUTCOME_RULE in p1
        assert UNTRUSTED_DATA_POLICY in p1

    # Settings API updates personality preset cleanly without breaking defaults
    put = client.put(
        "/api/v2/settings",
        json={"preferences": {"personality_preset": "concise", "custom_instructions": "Use tight bullets."}},
        headers=H,
    )
    assert put.status_code == 200
    prefs = put.json()["settings"]["preferences"]
    assert prefs["personality_preset"] == "concise"
    assert prefs["custom_instructions"] == "Use tight bullets."

    # Assistant._build_messages picks up updated preset automatically
    msgs = assistant._build_messages([], "Explain FastAPI", [], "2026-11-12")
    assert "Concise" in msgs[0]["content"]
    assert "Use tight bullets." in msgs[0]["content"]

    # NL personality switch command works
    nl = client.post("/api/chat", json={"message": "switch personality to technical"}, headers=H)
    assert nl.status_code == 200
    assert nl.json()["command"] == "set_personality"
    assert assistant.settings.preferences.personality_preset == "technical"
    assert build_messages([], "hi", [], "today")[0]["role"] == "system"
