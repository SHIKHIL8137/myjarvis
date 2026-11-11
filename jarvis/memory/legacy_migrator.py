"""Idempotent migrator importing existing JSONL, Markdown notes, and SQLite knowledge into unified DB."""
import json
from pathlib import Path
import sqlite3
import uuid

from ..db.engine import DatabaseManager
from ..db.models import (
    ConversationMessageRow,
    ExperienceLessonRow,
    KnowledgeChunkRow,
    KnowledgeSourceRow,
    KnowledgeVersionRow,
    ModelVersionRow,
    NoteRow,
    PersonalMemoryRow,
)
from ..notes import NoteStore
from .five_way import FiveWayMemoryService


def migrate_legacy_to_db(db: DatabaseManager, *, notes_dir: Path, data_dir: Path) -> dict:
    """Import legacy files atomically without altering original files; skips existing primary keys."""
    db.init_schema_sync()
    five = FiveWayMemoryService(db)
    five.ensure_session("default", title="Imported Main Session")
    counts = {"notes": 0, "personal_memories": 0, "history": 0, "experience": 0, "kb_sources": 0, "models": 0}

    with db.sync_session() as s:
        # 1. Markdown notes
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

        # 2. Personal memory JSONL
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

        # 3. Conversation history JSONL
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

        # 4. Feedback JSONL -> Experience lessons
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

        # 5. Knowledge SQLite index.db
        kb_db = data_dir / "learning" / "knowledge" / "index.db"
        if kb_db.exists():
            con = sqlite3.connect(str(kb_db))
            try:
                for row in con.execute("SELECT source_id, name, kind, location, topic, license, trust_classification, active_version, content_hash, language, retrieved_at, update_status FROM sources"):
                    if not s.get(KnowledgeSourceRow, row[0]):
                        s.add(
                            KnowledgeSourceRow(
                                source_id=row[0], name=row[1], kind=row[2], location=row[3], topic=row[4] or "general",
                                license=row[5] or "unknown", trust_classification=row[6] or "user_provided",
                                active_version=row[7] or 0, content_hash=row[8] or "", language=row[9] or "en",
                                retrieved_at=row[10], update_status=row[11] or "imported",
                            )
                        )
                        counts["kb_sources"] += 1
                for vrow in con.execute("SELECT source_id, version_num, content_hash, doc_version, license, language, retrieved_at, byte_size FROM versions"):
                    s.add(KnowledgeVersionRow(
                        source_id=vrow[0], version_num=vrow[1], content_hash=vrow[2], doc_version=vrow[3] or "1",
                        license=vrow[4] or "unknown", language=vrow[5] or "en", retrieved_at=vrow[6], byte_size=vrow[7] or 0,
                    ))
                for crow in con.execute("SELECT source_id, version_num, chunk_index, title, topic, language, content, content_hash, active FROM chunks"):
                    s.add(KnowledgeChunkRow(
                        source_id=crow[0], version_num=crow[1], chunk_index=crow[2], title=crow[3] or "",
                        topic=crow[4] or "general", language=crow[5] or "en", content=crow[6], content_hash=crow[7], active=bool(crow[8]),
                    ))
            except Exception:
                pass
            finally:
                con.close()

        # 6. Model Registry JSON
        reg_file = data_dir / "learning" / "registry.json"
        if reg_file.exists():
            try:
                reg = json.loads(reg_file.read_text(encoding="utf-8"))
                for vid, v in (reg.get("versions") or {}).items():
                    if not s.get(ModelVersionRow, vid):
                        s.add(
                            ModelVersionRow(
                                version_id=vid,
                                base_model=str(v.get("base_model") or "qwen3:4b-instruct"),
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

