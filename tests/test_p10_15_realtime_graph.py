"""P10.15: unified knowledge graph snapshot + live `graph_event` deltas (memory, conversation, agent runs)."""
from starlette.testclient import TestClient

from jarvis.api import create_app

H = {"X-Jarvis": "1"}


def _capture(assistant):
    events = []
    assistant.bus.publish = events.append  # the bus has no loop in unit tests: record instead of fan-out
    return events


def test_graph_snapshot_is_real_data_with_stable_ids(assistant, settings):
    client = TestClient(create_app(settings, assistant=assistant))
    g = client.get("/api/v2/graph").json()
    ids = [n["id"] for n in g["nodes"]]
    assert len(ids) == len(set(ids))
    notes = [n for n in g["nodes"] if n["type"] == "note"]
    assert len(notes) == len(assistant.store.all()) and all(n["id"].startswith("note:") for n in notes)
    assert all(e["source"] in ids and e["target"] in ids for e in g["links"])  # no dangling / fabricated edges
    assert g["counts"]["note"] == len(notes)
    assert client.get("/api/v2/graph").json()["nodes"][0]["id"] == ids[0]  # ids stable across requests
    only_models = client.get("/api/v2/graph?types=model").json()
    assert {n["type"] for n in only_models["nodes"]} <= {"model"}
    assert client.get("/api/v2/graph?limit=2").json()["truncated"].get("note") == len(notes)


def test_memory_lifecycle_emits_deltas_and_updates_snapshot(assistant, settings):
    events = _capture(assistant)
    client = TestClient(create_app(settings, assistant=assistant))
    rec = client.post("/api/v2/memory/personal", json={"content": "Owner prefers concise answers", "category": "preference"}, headers=H).json()["record"]
    nid = f"memory:{rec['id']}"
    created = [e for e in events if e.get("event") == "memory.created"]
    assert created and created[-1]["type"] == "graph_event" and created[-1]["nodes"][0]["id"] == nid
    assert nid in [n["id"] for n in client.get("/api/v2/graph?types=memory").json()["nodes"]]

    client.put(f"/api/v2/memory/personal/{rec['id']}", json={"content": "Owner prefers very concise answers"}, headers=H)
    upd = [e for e in events if e.get("event") == "memory.updated"][-1]
    assert upd["nodes"][0]["id"] == nid and "very concise" in upd["nodes"][0]["description"]  # same id, refreshed metadata

    client.delete(f"/api/v2/memory/personal/{rec['id']}", headers=H)
    assert [e for e in events if e.get("event") == "memory.deleted"][-1]["removed"] == [nid]
    assert nid not in [n["id"] for n in client.get("/api/v2/graph?types=memory").json()["nodes"]]


def test_chat_turn_links_and_highlights_retrieved_notes(assistant, settings):
    events = _capture(assistant)
    client = TestClient(create_app(settings, assistant=assistant))
    title = assistant.store.all()[0].title
    assert client.post("/api/chat", json={"message": f"Tell me about {title}"}, headers=H).status_code == 200
    ev = [e for e in events if e.get("event") == "conversation.updated"][-1]
    note_ids = {n["id"] for n in client.get("/api/v2/graph?types=note").json()["nodes"]}
    assert ev["highlight"][0] == "conversation:current" and set(ev["highlight"][1:]) <= note_ids
    g = client.get("/api/v2/graph").json()
    assert any(e["source"] == "conversation:current" and e["relation"] == "references" for e in g["links"])
    client.delete("/api/history", headers=H)
    assert events[-1]["removed"] == ["conversation:current"]


def test_agent_run_publishes_started_progress_and_completed(assistant, settings):
    events = _capture(assistant)
    client = TestClient(create_app(settings, assistant=assistant))
    res = client.post("/api/v2/assistant/runs", json={"goal": "Summarise my notes", "task_kind": "research"}, headers=H).json()
    run_events = [e for e in events if e.get("type") == "graph_event" and e.get("run_id") == res["run_id"]]
    names = [e["event"] for e in run_events]
    assert names[0] == "agent.started" and "agent.progress" in names and names[-1] in ("agent.completed", "agent.failed")
    assert {e["step"] for e in run_events if e["step"]} >= {"gather_context"}  # real LangGraph node names, not percentages
    tid = f"task:{res['run_id']}"
    assert run_events[0]["nodes"][0]["id"] == tid and tid in run_events[0]["highlight"]
    assert run_events[-1]["highlight"] == []  # finished runs stop glowing
    g = client.get("/api/v2/graph?types=task,agent").json()
    assert any(e["target"] == tid and e["relation"] == "executes" for e in g["links"])
    runs = client.get("/api/v2/workforce/runs").json()["runs"]
    assert runs[0]["id"] == res["run_id"] and "last_activity" in runs[0] and "model" in runs[0]


def test_skill_and_project_mutations_request_resync(assistant, settings, tmp_path):
    events = _capture(assistant)
    client = TestClient(create_app(settings, assistant=assistant))
    r = client.post("/api/v2/projects", json={"name": "demo", "root_path": str(tmp_path)}, headers=H)
    assert r.status_code == 200
    assert events[-1]["event"] == "project.updated" and events[-1]["resync"] is True
    assert f"project:{r.json()['project']['id']}" in [n["id"] for n in client.get("/api/v2/graph?types=project").json()["nodes"]]
    n = len(events)
    assert client.post("/api/v2/projects", json={"name": "bad", "root_path": str(tmp_path / "missing")}, headers=H).status_code >= 400
    assert len(events) == n  # failed mutations publish nothing
