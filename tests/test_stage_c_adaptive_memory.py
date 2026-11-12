"""Stage C tests: Adaptive 5-Way Memory Integration, Categories, Toggles, Ranking, Secret Exclusion & Clear."""
from __future__ import annotations

from starlette.testclient import TestClient

from jarvis.api import create_app
from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


def test_stage_c_five_way_integrated_into_chat_and_categories(assistant, settings):
    class MemoryCheckingBrain(FakeBrain):
        async def stream_events(self, messages, tools=None):
            self.seen.append({"messages": messages})
            joined = "\n".join(str(m.get("content") or "") for m in messages)
            assert "PostgreSQL 16 and FastAPI" in joined
            yield {"type": "token", "text": "Noted: you prefer PostgreSQL 16 and FastAPI."}
            self.last_stats = {"ttft": 0.05, "tokens": 9, "tps": 45.0, "total": 0.1, "model": self.spec.model, "truncated": False}

    assistant.brain = MemoryCheckingBrain(settings)
    client = TestClient(create_app(settings, assistant=assistant))

    # 1. Store structured user_preferences & project_memory via API
    r1 = client.post(
        "/api/v2/memory/personal",
        json={"category": "user_preferences", "topic": "stack", "content": "User prefers PostgreSQL 16 and FastAPI over MongoDB."},
        headers=H,
    )
    assert r1.status_code == 200
    mem_id = r1.json()["record"]["id"]

    r2 = client.post(
        "/api/v2/memory/personal",
        json={"category": "project_memory", "topic": "erp", "content": "ERP_V2 uses Alembic migrations for SQL schema."},
        headers=H,
    )
    assert r2.status_code == 200

    cats = client.get("/api/v2/memory/categories").json()
    assert cats["counts"]["user_preferences"] >= 1
    assert cats["counts"]["project_memory"] >= 1

    # 2. Chat retrieval integrates FiveWayMemoryService into prompt & increments retrieval_count
    rc = client.post("/api/chat", json={"message": "Which database stack do I prefer?"}, headers=H)
    assert rc.status_code == 200
    assert "PostgreSQL 16" in rc.json()["answer"]
    assert mem_id in rc.json().get("memory_ids", [])

    updated = assistant.orchestrator.memory.get_personal_memory(mem_id)
    assert int(updated["meta"].get("retrieval_count") or 0) >= 1

    # 3. Correct & delete memory cleanly
    r_upd = client.put(
        f"/api/v2/memory/personal/{mem_id}",
        json={"content": "User prefers PostgreSQL 16, FastAPI, and SQLAlchemy 2."},
        headers=H,
    )
    assert r_upd.status_code == 200
    assert "SQLAlchemy 2" in r_upd.json()["record"]["content"]

    r_del = client.delete(f"/api/v2/memory/personal/{mem_id}", headers=H)
    assert r_del.status_code == 200
    assert assistant.orchestrator.memory.get_personal_memory(mem_id) is None


def test_stage_c_category_toggles_frequency_ranking_secrets_and_clear(assistant, settings, monkeypatch):
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda *a, **kw: None)
    client = TestClient(create_app(settings, assistant=assistant))

    # 1. Secret exclusion across API & adaptive helper
    r_sec = client.post(
        "/api/v2/memory/personal",
        json={"category": "explicit", "content": "OPENAI_API_KEY=sk-proj-secret1234567890abcdef1234"},
        headers=H,
    )
    assert r_sec.status_code == 422

    # URI credential rejected silently by record_adaptive_memory without crashing
    assert (
        assistant.orchestrator.memory.record_adaptive_memory(
            content="postgres://admin:SuperSecretPass99@localhost:5432/prod",
            category="project_memory",
        )
        is None
    )

    # 2. Adaptive frequency ranking: frequently retrieved memory outranks identical peer
    m = assistant.orchestrator.memory
    a_rec = m.add_personal_memory(category="user_preferences", topic="theme", content="Use concise bullet formatting for deployment reports.")
    b_rec = m.add_personal_memory(category="user_preferences", topic="theme", content="Use concise bullet formatting for backend reports.")
    m.mark_retrieved([b_rec["id"], b_rec["id"], b_rec["id"]])
    hits = m.search_personal_memory("concise bullet formatting reports", k=5)
    assert len(hits) == 2
    assert hits[0]["id"] == b_rec["id"]
    assert hits[0]["ranking"]["frequency_boost"] > hits[1]["ranking"]["frequency_boost"]

    # 3. Conversation-derived memory + clear-conversation preserves user_preferences
    conv_rec = m.record_adaptive_memory(
        category="conversation_memory",
        topic="session",
        content="Discussed upgrading invoice exporter typography this afternoon.",
        source="conversation_derived",
    )
    assert conv_rec is not None

    # Turn recording respects category toggles
    r_tog = client.put(
        "/api/v2/settings",
        json={"memory": {"enable_conversation_memory": False}},
        headers=H,
    )
    assert r_tog.status_code == 200
    assert assistant.orchestrator.memory.is_category_enabled("conversation_memory") is False
    # Disabled category is skipped on record & excluded from search
    assert (
        m.record_adaptive_memory(
            category="conversation_memory",
            content="Should not be stored while disabled in memory settings.",
            source="conversation_derived",
        )
        is None
    )
    assert [h for h in m.search_personal_memory("invoice exporter typography", k=5) if h["id"] == conv_rec["id"]] == []
    assert m.search_personal_memory("invoice exporter typography", category="conversation_memory", k=5) == []

    # Re-enable, verify present, then clear conversation memory while preserving preference b_rec
    assert client.put("/api/v2/settings", json={"memory": {"enable_conversation_memory": True}}, headers=H).status_code == 200
    assert any(h["id"] == conv_rec["id"] for h in m.search_personal_memory("invoice exporter typography", category="conversation_memory", k=5))

    r_clr = client.post("/api/v2/memory/clear-conversation", headers=H)
    assert r_clr.status_code == 200
    assert r_clr.json()["cleared_count"] >= 1
    assert m.get_personal_memory(conv_rec["id"]) is None
    assert m.get_personal_memory(a_rec["id"]) is not None
    assert m.get_personal_memory(b_rec["id"]) is not None

