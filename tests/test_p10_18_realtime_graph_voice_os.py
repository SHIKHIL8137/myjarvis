"""P10.18 Integration Suite: Versioned real-time graph deltas, SSE replay, security redaction,
unified DecisionEngine + Approval gating, and shared Voice/Text command execution."""
import io
import wave

import numpy as np
from starlette.testclient import TestClient

from jarvis import graph_live
from jarvis.api import create_app

H = {"X-Jarvis": "1"}


def _wav_bytes(seconds: float = 0.25, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    samples = (np.zeros(int(seconds * sr), dtype=np.int16)).tobytes()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples)
    return buf.getvalue()


def test_event_bus_monotonic_seq_and_replay_recovery(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    base_seq = assistant.bus.latest_seq

    ev1 = assistant.bus.publish({"type": "graph_event", "event": "memory.created", "nodes": [{"id": "memory:m1", "type": "memory", "label": "Alpha"}]})
    ev2 = assistant.bus.publish({"type": "assistant_state", "event": "assistant.state.changed", "state": "Thinking"})
    assert ev1["schema_version"] == 1
    assert ev2["seq"] == ev1["seq"] + 1
    assert ev1["event_id"] == f"evt-{ev1['seq']}"
    assert "T" in ev1["occurred_at"]

    r = client.get(f"/api/v2/graph/events?since_seq={base_seq}").json()
    assert r["schema_version"] == 1
    assert r["gap"] is False
    assert [e["seq"] for e in r["events"]] >= [ev1["seq"], ev2["seq"]]


def test_graph_redacts_secrets_and_tokens_from_metadata(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    secret_text = "Use api_key=supersecret999 and sk-proj-abcdef1234567890xyz with Bearer eyJhbGciOiJIUzI1NiJ9token for auth"

    # Layer 1 defense: memory store rejects raw secret tokens at API boundary
    rejected = client.post("/api/v2/memory/personal", json={"content": secret_text, "category": "project"}, headers=H)
    assert rejected.status_code >= 400

    # Layer 2 defense: even if a note on disk contains credentials, graph serializer redacts them completely
    note = assistant.store.add(f"Auth note: {secret_text}", title="Credentials Note")
    g = client.get("/api/v2/graph?types=note").json()
    node = next(n for n in g["nodes"] if n["id"] == f"note:{note.uid}")
    desc = node.get("description", "")
    assert "supersecret999" not in desc
    assert "sk-proj-abcdef1234567890xyz" not in desc
    assert "eyJhbGciOiJIUzI1NiJ9token" not in desc
    assert "[REDACTED]" in desc


def test_unified_decision_routing_and_approval_gate(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    captured: list[dict] = []
    orig_pub = assistant.bus.publish

    def tap(ev):
        captured.append(orig_pub(ev))

    assistant.bus.publish = tap

    # Destructive privileged request must never bypass owner approval.
    r = client.post("/api/chat", json={"message": "run bash rm -rf /tmp/sensitive_dir immediately"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["command"] == "approval_required"
    assert body["ok"] is False
    assert body.get("approval_token")

    states = [e["state"] for e in captured if e.get("event_type") == "assistant.state.changed"]
    assert "Understanding" in states
    assert "Planning" in states
    assert "Waiting for approval" in states

    pending = client.get("/api/v2/approvals").json()["pending"]
    assert any(p["token"] == body["approval_token"] for p in pending)


def test_explicit_document_command_delegates_to_assistant_workflow_and_updates_graph(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    r = client.post("/api/chat", json={"message": "generate a pdf report on P10.18 knowledge graph"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["command"] == "document_export"
    assert body["ok"] is True
    assert body.get("run_id")

    g = client.get("/api/v2/graph").json()
    types_present = {n["type"] for n in g["nodes"]}
    assert "workflow" in types_present
    assert "task" in types_present
    assert "tool" in types_present
    assert "document" in types_present
    assert any(n["id"] == f"task:{body['run_id']}" for n in g["nodes"])
    assert any(e["source"] == f"task:{body['run_id']}" and e["target"].startswith("workflow:") and e["relation"] == "part_of" for e in g["links"])


def test_voice_command_and_text_share_unified_pipeline_and_highlight_graph(assistant, settings, monkeypatch):
    client = TestClient(create_app(settings, assistant=assistant))
    events: list[dict] = []
    orig_pub = assistant.bus.publish
    assistant.bus.publish = lambda ev: events.append(orig_pub(ev))

    title = assistant.store.all()[0].title
    monkeypatch.setattr(assistant.stt, "transcribe", lambda *a, **k: {"text": f"Explain {title}", "language": "en", "confidence": 0.96, "seconds": 0.12})
    monkeypatch.setattr(assistant.speaker, "say", lambda *a, **k: 1)
    monkeypatch.setattr(assistant.speaker, "say_chunk", lambda *a, **k: None)

    resp = client.post("/api/voice/command?speak=true", content=_wav_bytes(), headers={**H, "Content-Type": "audio/wav"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["prompt"] == f"Explain {title}"
    assert data["answer"] == "Certainly, sir. Here is your answer."

    ev_types = [e.get("event_type") for e in events]
    assert "assistant.transcript.final" in ev_types
    assert "decision.routed" in ev_types
    assert "conversation.updated" in ev_types

    # Security guard: mutating graph/voice endpoint requires X-Jarvis header.
    no_hdr = client.post("/api/voice/command", content=_wav_bytes())
    assert no_hdr.status_code == 403


def test_sse_stream_reconnect_replays_missed_events(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    e1 = assistant.bus.publish({"type": "graph_event", "event": "memory.created", "nodes": [{"id": "memory:r1", "type": "memory", "label": "Reconnect Test"}]})
    e2 = assistant.bus.publish({"type": "assistant_state", "event": "assistant.state.changed", "state": "Completed"})

    # Validate graph_live helper sanitization directly
    assert "[REDACTED]" in graph_live.sanitize_graph_text("password=mysecretpass")

    # Fetch missed events since before e1 via REST replay endpoint & verify ordering
    replay = client.get(f"/api/v2/graph/events?since_seq={e1['seq'] - 1}").json()
    seqs = [x["seq"] for x in replay["events"]]
    assert seqs == sorted(seqs)
    assert e1["seq"] in seqs and e2["seq"] in seqs
