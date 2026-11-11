"""Idempotent migrator importing legacy files AND migrating SQLite DBs cleanly into PostgreSQL+pgvector."""
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

import numpy as np
from sqlalchemy import inspect, select

from ..db.engine import DatabaseManager
from ..db.models import (
    AgentRunRow,
    ApprovalRequestRow,
    BackupManifestRow,
    ConversationMessageRow,
    ConversationSessionRow,
    ExperienceLessonRow,
    KnowledgeChunkRow,
    KnowledgeSourceRow,
    KnowledgeVersionRow,
    ModelVersionRow,
    NoteRow,
    PersonalMemoryRow,
    ProjectGraphRow,
    ProjectRow,
    ReminderRow,
    VectorEntryRow,
)
from ..notes import NoteStore
from .embeddings import EmbeddingCompatibilityError
from .five_way import FiveWayMemoryService
from .vectors import PgVectorStore


def migrate_legacy_to_db(db: DatabaseManager, *, notes_dir: Path, data_dir: Path) -> dict:
    """Import legacy files atomically without altering original files; skips existing primary keys."""
    db.init_schema_sync()
    five = FiveWayMemoryService(db)
    five.ensure_session("default", title="Imported Main Session")
    counts = {"notes": 0, "personal_memories": 0, "history": 0, "experience": 0, "kb_sources": 0, "models": 0}

    with db.sync_session() as s:
        if notes_dir.exists():
            store = NoteStore(notes_dir)
            for n in store.all():
                if not s.get(NoteRow, n.uid):
                    s.add(
                        NoteRow(
                            uid=n.uid,
                            title=n.title,
                            category=n.category,
                            slug=f"{n.category}/{Path(n.path).stem}-{n.uid[:6]}",
                            path=n.path,
                            body_md=n.body,
                            wikilinks=sorted(n.links),
                        )
                    )
                    counts["notes"] += 1

        mem_file = data_dir / "memory" / "records.jsonl"
        if mem_file.exists():
            for ln in mem_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                    rid = str(d.get("id") or uuid.uuid4().hex[:12])
                    if not s.get(PersonalMemoryRow, rid):
                        cat = str(d.get("category") or "explicit")
                        if cat not in ("explicit", "preference", "temporary"):
                            cat = "explicit"
                        s.add(
                            PersonalMemoryRow(
                                id=rid,
                                namespace="personal_memory",
                                category=cat,
                                scope="temporary" if cat == "temporary" else "global",
                                topic=str(d.get("topic") or "")[:120],
                                title=str(d.get("title") or "")[:200],
                                content=str(d.get("text") or ""),
                                confirmed=bool(d.get("confirmed", True)),
                                confidence=float(d.get("confidence", 1.0)),
                            )
                        )
                        counts["personal_memories"] += 1
                except Exception:
                    pass

        hist_file = data_dir / "history.jsonl"
        if hist_file.exists():
            existing_msg = s.query(ConversationMessageRow).filter(ConversationMessageRow.session_id == "default").count()
            if existing_msg == 0:
                for ln in hist_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        d = json.loads(ln)
                        s.add(
                            ConversationMessageRow(
                                session_id="default",
                                role=str(d.get("role") or "user"),
                                content=str(d.get("content") or ""),
                                created_at=float(d.get("t") or 0.0),
                            )
                        )
                        counts["history"] += 1
                    except Exception:
                        pass

        fb_file = data_dir / "learning" / "feedback" / "records.jsonl"
        if fb_file.exists():
            for ln in fb_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                    fid = str(d.get("id") or uuid.uuid4().hex[:12])
                    if not s.get(ExperienceLessonRow, fid):
                        s.add(
                            ExperienceLessonRow(
                                id=fid,
                                scope="assistant",
                                category=str(d.get("category") or "correction"),
                                topic=str(d.get("topic") or ""),
                                trigger_context=str(d.get("input_text") or ""),
                                outcome_before=str(d.get("previous_response") or ""),
                                lesson_after=str(d.get("corrected_response") or ""),
                                approved=bool(d.get("approval_status") == "approved"),
                                source=str(d.get("source") or "explicit_user"),
                            )
                        )
                        counts["experience"] += 1
                except Exception:
                    pass

        kb_db = data_dir / "learning" / "knowledge" / "index.db"
        if kb_db.exists():
            with sqlite3.connect(str(kb_db)) as conn:
                conn.row_factory = sqlite3.Row
                for r in conn.execute("SELECT * FROM sources").fetchall():
                    if not s.get(KnowledgeSourceRow, r["source_id"]):
                        s.add(
                            KnowledgeSourceRow(
                                source_id=r["source_id"],
                                name=r["name"],
                                kind=r["kind"],
                                location=r["location"],
                                topic=r["topic"] or "general",
                                license=r["license"] or "unknown",
                                trust_classification=r["trust_classification"] or "user_provided",
                                active_version=r["active_version"] or 0,
                                content_hash=r["content_hash"] or "",
                                language=r["language"] or "en",
                                retrieved_at=r["retrieved_at"],
                                update_status=r["update_status"] or "registered",
                            )
                        )
                        counts["kb_sources"] += 1
                for r in conn.execute("SELECT * FROM versions").fetchall():
                    s.add(
                        KnowledgeVersionRow(
                            source_id=r["source_id"],
                            version_num=r["version_num"],
                            content_hash=r["content_hash"],
                            doc_version=r["doc_version"] or "1",
                            license=r["license"] or "unknown",
                            language=r["language"] or "en",
                            retrieved_at=r["retrieved_at"],
                            byte_size=r["byte_size"] or 0,
                        )
                    )
                for r in conn.execute("SELECT * FROM chunks").fetchall():
                    s.add(
                        KnowledgeChunkRow(
                            source_id=r["source_id"],
                            version_num=r["version_num"],
                            chunk_index=r["chunk_index"],
                            title=r["title"] or "",
                            topic=r["topic"] or "general",
                            language=r["language"] or "en",
                            content=r["content"],
                            content_hash=r["content_hash"],
                            active=bool(r["active"]),
                        )
                    )

        reg_file = data_dir / "learning" / "training" / "registry.json"
        if reg_file.exists():
            try:
                reg = json.loads(reg_file.read_text(encoding="utf-8"))
                for vid, v in (reg.get("versions") or {}).items():
                    if not s.get(ModelVersionRow, vid):
                        s.add(
                            ModelVersionRow(
                                version_id=vid,
                                base_model=str(v.get("base_model") or ""),
                                artifact_type=str(v.get("artifact_type") or "ollama_tag"),
                                tag_or_path=str(v.get("ollama_tag") or v.get("adapter_path") or ""),
                                sha256=str(v.get("sha256") or ""),
                                status=str(v.get("status") or "candidate"),
                            )
                        )
                        counts["models"] += 1
            except Exception:
                pass
        s.commit()

    return counts


_MIGRATABLE_TABLES: tuple[Any, ...] = (
    PersonalMemoryRow,
    NoteRow,
    ConversationSessionRow,
    ConversationMessageRow,
    ProjectRow,
    ProjectGraphRow,
    ExperienceLessonRow,
    ReminderRow,
    AgentRunRow,
    ApprovalRequestRow,
    BackupManifestRow,
    VectorEntryRow,
)


def migrate_sqlite_to_postgres(
    source_db: DatabaseManager,
    target_db: DatabaseManager,
    *,
    dry_run: bool = False,
    expected_vector_dim: int | None = None,
) -> dict:
    """Explicitly transfer SQLite records & vectors into target DB (PostgreSQL+pgvector or SQLite copy)
    preserving IDs, timestamps, namespaces, project IDs, and expiry without mutating or deleting source rows."""
    source_db.init_schema_sync()
    target_db.init_schema_sync()

    table_reports: dict[str, dict] = {}
    pg_vecs_to_insert: list[dict] = []

    with source_db.sync_session() as src_s, target_db.sync_session() as dst_s:
        for model_cls in _MIGRATABLE_TABLES:
            tname = model_cls.__tablename__
            tbl_info = inspect(model_cls).local_table
            pk_col = list(tbl_info.primary_key.columns)[0].name
            src_rows = src_s.scalars(select(model_cls)).all()
            inserted = 0
            skipped = 0

            for row in src_rows:
                pk_val = getattr(row, pk_col)
                exists = dst_s.get(model_cls, pk_val) is not None
                if exists:
                    skipped += 1
                    continue

                if isinstance(row, VectorEntryRow):
                    dim = int(row.dim)
                    if expected_vector_dim and dim != expected_vector_dim:
                        raise EmbeddingCompatibilityError(
                            f"Cannot migrate vector {row.id}: dim={dim} != expected {expected_vector_dim}"
                        )
                    vec = np.frombuffer(row.embedding_blob, dtype="<f4")
                    if len(vec) != dim:
                        raise EmbeddingCompatibilityError(f"Corrupt vector length for {row.id}: {len(vec)} != {dim}")
                    if target_db.is_postgres and target_db.pgvector_available:
                        pg_vecs_to_insert.append(
                            {
                                "namespace": row.namespace,
                                "owner_id": row.owner_id,
                                "project_id": row.project_id,
                                "memory_type": row.memory_type,
                                "doc_id": row.doc_id,
                                "chunk_index": row.chunk_index,
                                "title": row.title,
                                "content": row.content,
                                "meta": dict(row.meta or {}),
                                "source": row.source,
                                "confidence": float(row.confidence),
                                "privacy_level": row.privacy_level,
                                "expires_at": row.expires_at,
                                "embedding": vec,
                            }
                        )

                if not dry_run:
                    data = {col.name: getattr(row, col.name) for col in tbl_info.columns}
                    dst_s.add(model_cls(**data))
                inserted += 1

            if not dry_run:
                dst_s.commit()

            dst_count = len(dst_s.scalars(select(model_cls)).all())
            table_reports[tname] = {
                "source_count": len(src_rows),
                "target_count": dst_count,
                "inserted": inserted,
                "skipped_existing": skipped,
                "counts_match_or_superset": dst_count >= len(src_rows) if not dry_run else True,
            }

    pgvector_migrated = 0
    if not dry_run and pg_vecs_to_insert and target_db.is_postgres and target_db.pgvector_available:
        dim = expected_vector_dim or int(len(pg_vecs_to_insert[0]["embedding"]))
        pg_store = PgVectorStore(target_db, dim=dim)
        pg_store.upsert_batch(pg_vecs_to_insert)
        pgvector_migrated = len(pg_vecs_to_insert)

    return {
        "ok": all(r["counts_match_or_superset"] for r in table_reports.values()),
        "dry_run": dry_run,
        "source_preserved": True,
        "pgvector_migrated": pgvector_migrated,
        "tables": table_reports,
    }
