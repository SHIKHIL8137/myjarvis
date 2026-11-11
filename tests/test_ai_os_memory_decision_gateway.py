"""Tests for P0-P3: Unicode retrieval, DPAPI/AES secrets, PII firewall, 5-way memory DB, DecisionEngine & Gateway."""
import json
import time

import pytest
from starlette.testclient import TestClient

from jarvis import retrieval
from jarvis.api import create_app
from jarvis.config import get_provider_api_key, set_provider_api_key
from jarvis.db.engine import DatabaseManager
from jarvis.decision.router import DecisionEngine
from jarvis.gateway.gateway import ModelGateway
from jarvis.gateway.interface import GatewayRequest
from jarvis.gateway.providers import CloudGatewayError
from jarvis.memory.five_way import FiveWayMemoryService
from jarvis.memory.legacy_migrator import migrate_legacy_to_db
from jarvis.security.pii_firewall import detect_secret, redact_secrets, scrub_messages
from jarvis.security.secrets import export_encrypted_vault, import_encrypted_vault, load_secrets_map
from jarvis.tts import PiperTTS
from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


def test_unicode_malayalam_tokenization_and_pii_firewall():
    toks = retrieval.tokens("മലയാളം notes fastAPI VRAM!")
    assert "മലയാളം" in toks and "notes" in toks and "fastapi" in toks and "vram" in toks

    assert detect_secret("my key is sk-proj-abc1234567890abcdef") is not None
    scrubbed, hits = scrub_messages(
        [{"role": "user", "content": "Use token AIzaSyDummyKey12345678901234567 and email a@b.com"}],
        mask_email=True,
    )
    assert "AIzaSy" not in scrubbed[0]["content"] and "a@b.com" not in scrubbed[0]["content"]
    assert hits
    clean_txt, _ = redact_secrets("password=supersecretpass")
    assert "supersecretpass" not in clean_txt


def test_dpapi_and_portable_vault_secrets(tmp_path):
    sec_file = tmp_path / "secrets.json"
    set_provider_api_key("openrouter", "sk-or-v1-test1234567890123456", secrets_path=sec_file)
    raw_disk = sec_file.read_text(encoding="utf-8")
    assert "sk-or-v1-test1234567890123456" not in raw_disk  # encrypted at rest via DPAPI on Windows
    assert get_provider_api_key("openrouter", secrets_path=sec_file) == "sk-or-v1-test1234567890123456"
    loaded = load_secrets_map(sec_file)
    assert loaded["openrouter"] == "sk-or-v1-test1234567890123456"

    blob = export_encrypted_vault(loaded, "StrongPassphrase!1")
    assert b"sk-or-v1" not in blob
    assert import_encrypted_vault(blob, "StrongPassphrase!1") == loaded
    with pytest.raises(Exception):
        import_encrypted_vault(blob, "WrongPassphrase!")


def test_five_way_memory_isolation_and_legacy_migration(tmp_path, notes_dir):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    mem = FiveWayMemoryService(db)

    # Personal vs Project A vs Project B strict isolation
    mem.add_personal_memory(content="User prefers Malayalam and English concise answers", category="preference")
    mem.add_personal_memory(content="Temporary note", category="temporary", ttl_seconds=-10)  # already expired
    mem.index_project_knowledge(project_id="proj-a", doc_id="d1", title="ERP Auth", content="Project A uses PostgreSQL JWT tokens")
    mem.index_project_knowledge(project_id="proj-b", doc_id="d2", title="Shop UI", content="Project B uses React canvas dashboard")
    mem.add_experience_lesson(scope="dev", project_id="proj-a", trigger_context="CORS failure", lesson_after="Set X-Jarvis header", approved=True)

    p_items = mem.list_personal_memories()
    assert len(p_items) == 1 and "Malayalam" in p_items[0]["content"]
    assert len(mem.search_personal_memory("Malayalam", k=3)) == 1

    hits_a = mem.search_project_knowledge(project_id="proj-a", query="JWT tokens dashboard", k=5)
    assert len(hits_a) == 1 and hits_a[0]["doc_id"] == "d1"
    hits_b = mem.search_project_knowledge(project_id="proj-b", query="JWT tokens PostgreSQL", k=5)
    assert all(h["doc_id"] != "d1" for h in hits_b)

    # Legacy migration from notes_dir
    data_dir = tmp_path / "legacy_data"
    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "history.jsonl").write_text(json.dumps({"role": "user", "content": "Hello", "t": time.time()}) + "\n", encoding="utf-8")
    counts = migrate_legacy_to_db(db, notes_dir=notes_dir, data_dir=data_dir)
    assert counts["notes"] == 30 and counts["history"] == 1
    db.sync_engine.dispose()


@pytest.mark.anyio
async def test_3layer_decision_engine_and_gateway_offline_guard(tmp_path, settings):
    dec = DecisionEngine(settings)
    p1 = await dec.decide("/status")
    assert p1.decision.layer == 1 and p1.workflow == "fast_command" and p1.decision.latency_ms < 50

    p2 = await dec.decide("Implement a unit test refactor for the backend repo")
    assert p2.decision.department == "dev" and p2.workflow == "workforce_graph"

    p3 = await dec.decide("Generate a PDF monthly summary report")
    assert p3.decision.department == "assistant" and p3.decision.intent == "document_export"

    # Gateway blocks cloud in Local Mode and logs local Ollama usage cleanly
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    brain = FakeBrain(settings)
    gw = ModelGateway(brain, db=db)

    events = [ev async for ev in gw.stream_with_fallback(GatewayRequest(model_key="local", messages=[{"role": "user", "content": "hi"}]))]
    assert any(e.type == "done" for e in events)
    assert gw.usage_summary()["total_calls"] == 1

    with pytest.raises(CloudGatewayError):
        async for _ in gw.stream_with_fallback(GatewayRequest(model_key="openrouter:qwen/qwen3-coder:free", messages=[{"role": "user", "content": "hi"}])):
            pass
    db.sync_engine.dispose()


def test_overview_no_write_side_effect_tts_prune_and_dynamic_di(assistant, settings, tmp_path):
    app = create_app(settings, assistant=assistant)
    client = TestClient(app)

    # GET /api/learning/overview must not create scheduler_state.json
    sched_file = assistant.data_dir / "learning" / "training" / "scheduler_state.json"
    assert not sched_file.exists()
    r = client.get("/api/learning/overview")
    assert r.status_code == 200 and not sched_file.exists()

    # Dynamic Depends replacement when app.state.a is swapped
    from jarvis.assistant import Assistant
    alt = Assistant(settings, data_dir=tmp_path / "alt", brain=FakeBrain(settings))
    alt.memory_store.add("swapped-instance-fact", category="explicit")
    app.state.a = alt
    items = client.get("/api/memory/items").json()["items"]
    assert any("swapped-instance-fact" in i["text"] for i in items)

    # TTS cache LRU pruning
    tts = PiperTTS(settings.tts)
    tts.cache_dir = tmp_path / "tts_cache"
    tts.cache_dir.mkdir()
    for i in range(6):
        (tts.cache_dir / f"c{i}.wav").write_bytes(b"0" * 100)
    tts._prune_cache(max_files=3, max_bytes=10_000)
    assert len(list(tts.cache_dir.glob("*.wav"))) == 3
