"""P10.2 & P10.3 Verification: Embeddings, PgVector SQL builder/live PG, expiry in vectors, lifecycle, ranking & migration."""
import os
import time

import numpy as np
import pytest

from jarvis.db.engine import DatabaseManager
from jarvis.db.models import AuditEventRow
from jarvis.memory.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingCompatibilityError,
    create_embedding_provider,
    validate_embedding_vector,
)
from jarvis.memory.five_way import DuplicateMemoryError, FiveWayMemoryService
from jarvis.memory.legacy_migrator import migrate_sqlite_to_postgres
from jarvis.memory.vectors import PgVectorStore, SQLiteVectorStore, create_vector_store

PG_TEST_URL = os.environ.get("JARVIS_PG_TEST_URL", "").strip()


def test_embedding_validation_normalization_and_dim_mismatch_block(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    vs = SQLiteVectorStore(db, dim=64, metric="cosine")

    v = validate_embedding_vector(np.array([3.0] + [0.0] * 63, dtype=np.float32), 64)
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5

    with pytest.raises(EmbeddingCompatibilityError):
        vs.upsert(namespace="personal_memory", doc_id="bad", content="x", embedding=np.zeros(128, dtype=np.float32))

    # Switching configured embedding provider defaults cleanly to fallback when model unset
    fb = create_embedding_provider(None)
    assert isinstance(fb, DeterministicHashEmbeddingProvider) and fb.is_fallback is True
    db.sync_engine.dispose()


def test_memory_expiry_filtering_in_vector_and_sql_and_transactional_purge(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    mem = FiveWayMemoryService(db)

    now = time.time()
    expired_rec = mem.add_personal_memory(
        content="Temporary secret meeting code alpha-999 expires quickly",
        category="temporary",
        topic="temp_meeting",
        expires_at=now - 5.0,
    )
    active_rec = mem.add_personal_memory(
        content="Temporary active code beta-777 remains searchable",
        category="temporary",
        topic="temp_meeting",
        expires_at=now + 600.0,
    )

    # Even though vector was indexed, vector search AND personal search MUST exclude expired item!
    vec_hits = mem.vectors.search(namespace="personal_memory", query="alpha-999 secret meeting", k=5, now=now)
    assert all(h.doc_id != expired_rec["id"] for h in vec_hits)

    res = mem.search_personal_memory("code", k=10, now=now)
    ids = {r["id"] for r in res}
    assert active_rec["id"] in ids
    assert expired_rec["id"] not in ids
    assert mem.get_personal_memory(expired_rec["id"]) is None

    # Transactional purge removes both expired SQL row AND its vector row
    purge = mem.purge_expired(now=now)
    assert purge["sql_purged"] == 1
    assert purge["vectors_purged"] == 1
    assert mem.vectors.count("personal_memory") == 1
    db.sync_engine.dispose()


def test_memory_lifecycle_duplicates_supersession_audit_and_ranking(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()
    mem = FiveWayMemoryService(db)

    r1 = mem.add_personal_memory(
        content="User prefers pytest for FastAPI regression testing.",
        category="preference",
        topic="testing",
        confidence=0.95,
        privacy_level="private",
        allow_duplicate=False,
    )
    with pytest.raises(DuplicateMemoryError):
        mem.add_personal_memory(
            content="User prefers pytest for FastAPI regression testing.",
            category="preference",
            topic="testing",
            allow_duplicate=False,
        )

    # Distinct topic must NOT be auto-merged even with similar wording
    r_other = mem.add_personal_memory(
        content="User prefers pytest for React integration tests.",
        category="preference",
        topic="frontend_testing",
        allow_duplicate=False,
    )
    assert r_other["id"] != r1["id"]

    # Update, archive, restore, supersede, and soft/hard delete
    upd = mem.update_personal_memory(r1["id"], content="User requires pytest -v with coverage.")
    assert "coverage" in upd["content"]

    mem.archive_personal_memory(r1["id"])
    assert mem.get_personal_memory(r1["id"]) is None
    mem.restore_personal_memory(r1["id"])
    assert mem.get_personal_memory(r1["id"]) is not None

    r2 = mem.supersede_personal_memory(r1["id"], new_content="User migrated test runner to pytest strict mode.")
    assert r2["supersedes_id"] == r1["id"]
    assert mem.get_personal_memory(r1["id"]) is None
    assert "strict mode" in mem.search_personal_memory("pytest strict mode", k=3)[0]["content"]

    # Check multi-factor transparent ranking breakdown
    ranked_hits = mem.search_personal_memory("pytest", k=5)
    assert ranked_hits and "ranking" in ranked_hits[0]
    assert set(ranked_hits[0]["ranking"]) >= {"similarity", "recency", "confidence", "relevance", "project_boost"}

    mem.delete_personal_memory(r_other["id"], hard=False)
    assert mem.get_personal_memory(r_other["id"]) is None

    # Audit events logged
    with db.sync_session() as s:
        cats = [a.decision for a in s.query(AuditEventRow).filter(AuditEventRow.category == "memory").all()]
    assert {"create", "update", "archive", "restore", "supersede", "soft_deleted"} <= set(cats)
    db.sync_engine.dispose()


def test_pgvector_sql_builder_operators_and_sqlite_scale_benchmark(tmp_path):
    db = DatabaseManager("", data_dir=tmp_path)
    db.init_schema_sync()

    # Verify PgVectorStore parameterizes DB-side similarity operators & expiry/meta filters cleanly
    for metric, op in (("cosine", "<=>"), ("l2", "<->"), ("ip", "<#>")):
        pg = PgVectorStore(db, dim=256, metric=metric)
        sql, params = pg.build_search_query(
            namespace="project_knowledge",
            project_id="proj-1",
            memory_type="fact",
            meta_filter={"lang": "py"},
            candidate_limit=12,
        )
        assert op in sql
        assert "ORDER BY embedding" in sql
        assert "LIMIT :limit" in sql
        assert "(expires_at IS NULL OR expires_at > :now_ts)" in sql
        assert "meta @> CAST(:meta_json AS jsonb)" in sql
        assert params["project_id"] == "proj-1" and params["limit"] == 12

    # Real batch insert & filtered retrieval timing check on 1,000 SQLite vectors
    vs = SQLiteVectorStore(db, dim=128)
    now = time.time()
    items = [
        {
            "namespace": "external_knowledge",
            "doc_id": f"doc-{i}",
            "title": f"Item {i}",
            "content": f"FastAPI pgvector benchmark vector number {i}" if i % 10 == 0 else f"Misc entry {i}",
            "memory_type": "kb_chunk",
            "meta": {"shard": i % 4},
            "expires_at": (now - 10) if i % 20 == 0 else (now + 3600),
        }
        for i in range(1000)
    ]
    t0 = time.perf_counter()
    ids = vs.upsert_batch(items)
    insert_ms = (time.perf_counter() - t0) * 1000.0
    assert len(ids) == 1000

    t1 = time.perf_counter()
    hits = vs.search(namespace="external_knowledge", query="FastAPI pgvector benchmark", k=5, meta_filter={"shard": 0}, now=now)
    search_ms = (time.perf_counter() - t1) * 1000.0
    assert hits and all(h.expires_at is None or h.expires_at > now for h in hits)
    assert all(h.meta.get("shard") == 0 for h in hits)
    assert insert_ms < 15000 and search_ms < 5000

    # Migration verification SQLite -> second SQLite preserving IDs, timestamps, expiry & counts
    db2 = DatabaseManager("", data_dir=tmp_path / "target")
    rep = migrate_sqlite_to_postgres(db, db2)
    assert rep["ok"] is True and rep["tables"]["vector_entries"]["source_count"] == 1000
    assert rep["tables"]["vector_entries"]["target_count"] == 1000
    db.sync_engine.dispose()
    db2.sync_engine.dispose()


@pytest.mark.pg
@pytest.mark.skipif(not PG_TEST_URL, reason="JARVIS_PG_TEST_URL not configured for live PostgreSQL+pgvector test")
def test_live_postgres_pgvector_crud_hnsw_expiry_and_migration(tmp_path):
    src = DatabaseManager("", data_dir=tmp_path / "src")
    src.init_schema_sync()
    src_mem = FiveWayMemoryService(src)
    now = time.time()
    src_mem.add_personal_memory(content="Live PG vector memory alpha", category="explicit", topic="pg")
    src_mem.add_personal_memory(content="Live PG expired vector beta", category="temporary", expires_at=now - 2.0)

    pg_db = DatabaseManager(PG_TEST_URL)
    pg_db.init_schema_sync()
    assert pg_db.is_postgres and pg_db.pgvector_available
    rep = migrate_sqlite_to_postgres(src, pg_db)
    assert rep["ok"] is True and rep["pgvector_migrated"] >= 1

    store = create_vector_store(pg_db)
    assert isinstance(store, PgVectorStore)
    hits = store.search(namespace="personal_memory", query="Live PG vector memory alpha", k=5, now=now)
    assert hits and "alpha" in hits[0].content
    assert all("expired" not in h.content for h in hits)
    src.sync_engine.dispose()
    pg_db.sync_engine.dispose()

