"""HTTP API with a scripted brain: startup, chat, streaming, history, models, memory, notes, graph, config, security, errors."""
import json
import re
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis import audit
from jarvis.api import create_app
from jarvis.config import ROOT

H = {"X-Jarvis": "1"}


@pytest.fixture
def client(settings, assistant, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda patch, *a: None)  # never touch the real data/settings.json
    with TestClient(create_app(settings, assistant)) as c:
        c.a = assistant
        yield c


def test_backend_starts_and_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["notes"] == 30
    assert client.get("/").status_code == 200 and "Jarvis" in client.get("/").text  # frontend served by the same app


def test_chat_returns_answer_and_sources(client):
    r = client.post("/api/chat", json={"message": "How much VRAM do I need for a local model?"}, headers=H).json()
    assert r["answer"] == "Certainly, sir. Here is your answer." and r["ok"] and r["paths"]
    assert any("GPU VRAM Basics" in p for p in r["paths"])
    sent = client.a.brain.seen[-1]
    assert sent[0]["role"] == "system" and "Notes:" in sent[-1]["content"]


def test_small_talk_has_no_sources(client):
    r = client.post("/api/chat", json={"message": "Hello there, good morning"}, headers=H).json()
    assert r["nodes"] == [] and "Notes:" not in client.a.brain.seen[-1][-1]["content"]


def test_streaming_events_in_order(client):
    with client.stream("POST", "/api/chat/stream", json={"message": "Tell me about quantization"}, headers=H) as s:
        evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
    assert [e["type"] for e in evs] == ["meta", "token", "token", "done"] and "".join(e["text"] for e in evs if e["type"] == "token") == evs[-1]["answer"]


def test_conversation_history_is_used_and_clearable(client):
    client.post("/api/chat", json={"message": "first question about pomodoro"}, headers=H)
    client.post("/api/chat", json={"message": "and a follow-up"}, headers=H)
    msgs = client.a.brain.seen[-1]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"] and msgs[1]["content"] == "first question about pomodoro"
    assert len(client.get("/api/history").json()["messages"]) == 4
    client.delete("/api/history", headers=H)
    assert client.get("/api/history").json()["messages"] == []


def test_model_listing_and_switching(client):
    m = client.get("/api/models").json()
    assert m["current"] == "local" and [x["key"] for x in m["models"]][:2] == ["local", "local_thinking"]
    assert client.post("/api/models/switch", json={"model": "local_thinking"}, headers=H).json()["model"] == "qwen3:4b"
    assert client.a.brain.unloaded == ["qwen3:4b-instruct"] and client.get("/api/models").json()["current"] == "local_thinking"
    r = client.post("/api/models/switch", json={"model": "no-such"}, headers=H)
    assert r.status_code == 503 and "not installed" in r.json()["error"]


def test_voice_command_model_switch_and_receipt(client):
    r = client.post("/api/chat", json={"message": "switch to the thinking model"}, headers=H).json()
    assert r["command"] == "switch" and "qwen3:4b" in r["answer"] and client.a.brain.current == "local_thinking"
    r = client.post("/api/chat", json={"message": "switch to the banana model"}, headers=H).json()
    assert r["ok"] is False and "no model called banana" in r["answer"] and client.a.brain.current == "local_thinking"  # honest failure


def test_memory_create_search_and_voice_remember(client):
    r = client.post("/api/memory", json={"text": "Neovim is my editor of choice"}, headers=H).json()
    assert r["category"] == "captures"
    assert client.get("/api/memory/search", params={"q": "neovim editor"}).json()["results"][0]["title"].lower().startswith("neovim")
    c = client.post("/api/chat", json={"message": "Remember that the finish window should be 900 milliseconds"}, headers=H).json()
    assert c["command"] == "remember" and c["ok"] and c["paths"] and "Noted" in c["answer"]
    assert client.get("/api/memory/search", params={"q": "finish window milliseconds"}).json()["results"][0]["title"].startswith("The finish window")


def test_remember_failure_is_reported_not_silent(client, monkeypatch):
    monkeypatch.setattr(client.a.store, "add", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = client.post("/api/chat", json={"message": "remember that this will fail"}, headers=H).json()
    assert r["ok"] is False and "could not save" in r["answer"] and "disk full" in r["answer"]


def test_notes_crud_and_graph(client):
    ns = client.get("/api/notes").json()["notes"]
    assert len(ns) == 30
    n = client.get(f"/api/notes/{ns[0]['id']}").json()
    assert n["body"]
    created = client.post("/api/memory", json={"text": "scratch note"}, headers=H).json()
    assert client.put(f"/api/notes/{created['id']}", json={"text": "# Scratch\n\nedited"}, headers=H).status_code == 200
    d = client.get("/api/graph").json()
    assert len(d["nodes"]) == 31 and d["links"]
    scratch = next(x for x in d["nodes"] if x["path"] == created["path"])
    assert client.delete(f"/api/notes/{scratch['id']}", headers=H).json()["moved_to"].startswith(".trash/")
    assert len(client.get("/api/graph").json()["nodes"]) == 30
    assert client.get("/api/notes/9999").status_code == 404


def test_config_get_put_and_validation(client):
    c = client.get("/api/config").json()
    assert c["tts"]["voice"] == "en_GB-alan-medium" and "brains" not in c  # no brain endpoints/keys exposed
    assert client.put("/api/config", json={"tts": {"speed": 1.2}}, headers=H).status_code == 200 and client.a.settings.tts.speed == 1.2
    assert client.put("/api/config", json={"tts": {"speed": 99}}, headers=H).status_code == 422
    assert client.put("/api/config", json={"brains": {}}, headers=H).status_code == 422  # not editable remotely
    assert client.put("/api/config", json={"host": "0.0.0.0"}, headers=H).status_code == 422


def test_status_has_real_system_metrics(client):
    client.get("/api/status")  # first call primes the CPU delta
    s = client.get("/api/status").json()
    sysm = s["system"]
    assert 0 <= sysm["cpu_pct"] <= 100 and sysm["ram"]["total_gb"] > 1 and set(sysm["audio"]) == {"input", "output"}
    assert s["tts"]["voice"] == "en_GB-alan-medium" and s["offline_enforced"] is True and s["brain"]["num_ctx"] == 4096


def test_context_size_and_voice_settings(client):
    c = client.get("/api/config").json()
    assert c["num_ctx"] == 4096 and "en_GB-alan-medium" in c["voices"]
    assert client.put("/api/config", json={"num_ctx": 2048}, headers=H).status_code == 200
    assert client.a.brain.spec.num_ctx == 2048 and client.get("/api/config").json()["num_ctx"] == 2048
    for bad in (100, 99999, "4096", True):
        assert client.put("/api/config", json={"num_ctx": bad}, headers=H).status_code == 422, bad
    assert client.put("/api/config", json={"tts": {"voice": "no-such-voice"}}, headers=H).status_code == 422


def test_show_notes_and_find_commands(client):
    r = client.post("/api/chat", json={"message": "Show my notes"}, headers=H).json()
    assert r["command"] == "show_notes" and r["ui"] == {"open": "notes"} and "30" in r["answer"]
    r = client.post("/api/chat", json={"message": "Find information about spaced repetition"}, headers=H).json()
    assert r["command"] == "find" and r["ui"] == {"search": "spaced repetition"} and any("Spaced Repetition" in p for p in r["paths"])
    r = client.post("/api/chat", json={"message": "search my knowledge"}, headers=H).json()
    assert r["command"] == "find" and r["ui"] == {"focus": "search"} and r["paths"] == []
    r = client.post("/api/chat", json={"message": "find information about zzzqqq"}, headers=H).json()
    assert r["ok"] is False and "nothing" in r["answer"]
    assert client.a.brain.seen == []  # none of these reached the model
    r = client.post("/api/chat", json={"message": "Explain how notes are searched"}, headers=H).json()
    assert r["command"] is None  # an ordinary question still goes to the brain


def test_input_validation(client):
    assert client.post("/api/chat", json={"message": ""}, headers=H).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 5000}, headers=H).status_code == 422
    assert client.post("/api/voice/hear", content=b"garbage", headers=H).status_code == 422
    assert client.post("/api/voice/hear", content=b"", headers=H).status_code == 422


def test_central_error_handling_when_ollama_down(client):
    client.a.brain.fail = True
    r = client.post("/api/chat", json={"message": "hello"}, headers=H)
    assert r.status_code == 503 and "not reachable" in r.json()["error"]
    assert client.get("/api/health").json()["status"] == "degraded"
    with client.stream("POST", "/api/chat/stream", json={"message": "hello"}, headers=H) as s:
        evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
    assert evs[-1]["type"] == "error"


# ---- security ----
def test_security_guards(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403  # no X-Jarvis header (CSRF)
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403  # DNS rebinding
    assert client.get("/api/health", headers={"origin": "http://evil.example"}).status_code == 403
    r = client.get("/api/health")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"


def test_private_files_not_served(client):
    for p in ("/config.json", "/preflight.py", "/data/history.jsonl", "/notes/learning/Active%20Recall.md", "/../config.json", "/.env", "/%2e%2e/config.json"):
        assert client.get(p).status_code in (404, 400), p


def test_offline_enforcement_blocks_remote_connections(client):
    audit.set_enforce(True)
    with pytest.raises(ConnectionError, match="offline mode"):
        socket.create_connection(("example.com", 80), timeout=2)
    with pytest.raises(ConnectionError):
        socket.getaddrinfo("api.anthropic.com", 443)
    t = audit.mark()
    socket.getaddrinfo("localhost", 4700)
    assert audit.since(t, only_remote=True) == [] and client.get("/api/drill/connections").json()["enforced"] is True


def test_viewer_has_no_cdn_or_external_urls():
    for f in ["index.html", "style.css", *[f"js/{p.name}" for p in sorted((ROOT / "viewer" / "js").glob("*.js"))]]:
        txt = (ROOT / "viewer" / f).read_text(encoding="utf-8")
        urls = re.findall(r"""(?:src|href)=["']?(https?://[^"' >]+)|url\((https?://[^)]+)\)|fetch\(["'](https?://)""", txt)
        assert not any(any(u) for u in urls), (f, urls)
        assert "//cdn" not in txt and "unpkg" not in txt and "jsdelivr" not in txt
    assert (ROOT / "viewer" / "vendor" / "3d-force-graph.min.js").exists() and (ROOT / "viewer" / "vendor" / "VERSIONS.txt").exists()
