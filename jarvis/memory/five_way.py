"""Unified 5-Way Memory & Session Service backed by SQLAlchemy (PostgreSQL 18 or SQLite WAL)."""
import time
import uuid

from sqlalchemy import delete, select

from ..db.engine import DatabaseManager
from ..db.models import (
    ConversationMessageRow,
    ConversationSessionRow,
    ExperienceLessonRow,
    NoteRow,
    PersonalMemoryRow,
    VectorEntryRow,
)
from ..security.pii_firewall import detect_secret
from .vectors import IsolatedVectorStore


class FiveWayMemoryService:
    """Enforces strict separation across Personal, Project, Experience, External KB, and Training data."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.vectors = IsolatedVectorStore(db)

    # ---- 1. Personal Memory (User Facts, Preferences, Temporary Context) ------------------------------
    def add_personal_memory(
        self,
        *,
        content: str,
        category: str = "explicit",
        topic: str = "",
        title: str = "",
        confirmed: bool = True,
        ttl_seconds: float | None = None,
    ) -> dict:
        if sec := detect_secret(content):
            raise ValueError(f"Refused to store sensitive credential in personal memory ({sec})")
        cat = category if category in ("explicit", "preference", "temporary") else "explicit"
        scope = "temporary" if cat == "temporary" else "global"
        expires = (time.time() + ttl_seconds) if ttl_seconds else None
        mid = uuid.uuid4().hex
        with self.db.sync_session() as s:
            row = PersonalMemoryRow(
                id=mid,
                category=cat,
                scope=scope,
                topic=topic[:120],
                title=(title or content[:80])[:200],
                content=content.strip(),
                confirmed=confirmed,
                expires_at=expires,
            )
            s.add(row)
            s.commit()
        if confirmed:
            self.vectors.upsert(
                namespace="personal_memory",
                doc_id=mid,
                title=title or topic,
                content=content,
                meta={"category": cat, "confirmed": True},
            )
        return {"id": mid, "category": cat, "scope": scope, "topic": topic, "title": title, "content": content, "confirmed": confirmed}

    def list_personal_memories(self, *, category: str | None = None, include_pending: bool = True) -> list[dict]:
        now = time.time()
        with self.db.sync_session() as s:
            stmt = select(PersonalMemoryRow)
            if category:
                stmt = stmt.where(PersonalMemoryRow.category == category)
            if not include_pending:
                stmt = stmt.where(PersonalMemoryRow.confirmed.is_(True))
            rows = s.scalars(stmt.order_by(PersonalMemoryRow.updated_at.desc())).all()
        return [
            {
                "id": r.id,
                "category": r.category,
                "scope": r.scope,
                "topic": r.topic,
                "title": r.title,
                "content": r.content,
                "confirmed": r.confirmed,
                "created_at": r.created_at,
            }
            for r in rows
            if r.expires_at is None or r.expires_at > now
        ]

    def search_personal_memory(self, query: str, k: int = 5) -> list[dict]:
        hits = self.vectors.search(namespace="personal_memory", query=query, k=k, meta_filter={"confirmed": True})
        return [{"id": h.doc_id, "title": h.title, "content": h.content, "score": h.score, "meta": h.meta} for h in hits]

    # ---- 2. Project Knowledge (Strictly Isolated per project_id) --------------------------------------
    def index_project_knowledge(self, *, project_id: str, doc_id: str, title: str, content: str, meta: dict | None = None) -> str:
        return self.vectors.upsert(
            namespace="project_knowledge",
            project_id=project_id,
            doc_id=doc_id,
            title=title,
            content=content,
            meta=meta or {},
        )

    def search_project_knowledge(self, *, project_id: str, query: str, k: int = 5) -> list[dict]:
        hits = self.vectors.search(namespace="project_knowledge", project_id=project_id, query=query, k=k)
        return [{"doc_id": h.doc_id, "title": h.title, "content": h.content, "score": h.score, "meta": h.meta} for h in hits]

    # ---- 3. Experience & Lesson Memory (Assistant Corrections + Coding Post-Mortems) -----------------
    def add_experience_lesson(
        self,
        *,
        scope: str = "assistant",
        category: str = "correction",
        trigger_context: str,
        lesson_after: str,
        outcome_before: str = "",
        topic: str = "",
        project_id: str | None = None,
        approved: bool = False,
        source: str = "explicit_user",
    ) -> dict:
        if sec := detect_secret(f"{trigger_context}\n{lesson_after}"):
            raise ValueError(f"Sensitive credential rejected from experience store ({sec})")
        lid = uuid.uuid4().hex
        with self.db.sync_session() as s:
            row = ExperienceLessonRow(
                id=lid,
                scope=scope,
                project_id=project_id,
                category=category,
                topic=topic[:120],
                trigger_context=trigger_context,
                outcome_before=outcome_before,
                lesson_after=lesson_after,
                approved=approved,
                source=source,
            )
            s.add(row)
            s.commit()
        if approved:
            self.vectors.upsert(
                namespace="experience_memory",
                project_id=project_id,
                doc_id=lid,
                title=f"[{scope}/{category}] {topic}",
                content=f"Situation: {trigger_context}\nLesson: {lesson_after}",
                meta={"scope": scope, "approved": True, "category": category},
            )
        return {
            "id": lid,
            "scope": scope,
            "project_id": project_id,
            "category": category,
            "topic": topic,
            "trigger_context": trigger_context,
            "lesson_after": lesson_after,
            "approved": approved,
        }

    def list_experience_lessons(self, *, scope: str | None = None, project_id: str | None = None) -> list[dict]:
        with self.db.sync_session() as s:
            stmt = select(ExperienceLessonRow)
            if scope:
                stmt = stmt.where(ExperienceLessonRow.scope == scope)
            if project_id:
                stmt = stmt.where(ExperienceLessonRow.project_id == project_id)
            rows = s.scalars(stmt.order_by(ExperienceLessonRow.created_at.desc())).all()
        return [
            {
                "id": r.id,
                "scope": r.scope,
                "project_id": r.project_id,
                "category": r.category,
                "topic": r.topic,
                "trigger_context": r.trigger_context,
                "outcome_before": r.outcome_before,
                "lesson_after": r.lesson_after,
                "approved": r.approved,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    def search_experience(self, query: str, *, scope: str | None = None, project_id: str | None = None, k: int = 4) -> list[dict]:
        mf: dict = {"approved": True}
        if scope:
            mf["scope"] = scope
        hits = self.vectors.search(namespace="experience_memory", project_id=project_id, query=query, k=k, meta_filter=mf)
        return [{"id": h.doc_id, "title": h.title, "content": h.content, "score": h.score, "meta": h.meta} for h in hits]

    # ---- 4. Personal Documents Index ------------------------------------------------------------------
    def index_document_chunk(self, *, doc_id: str, chunk_index: int, title: str, content: str, meta: dict | None = None) -> str:
        return self.vectors.upsert(
            namespace="personal_documents",
            doc_id=doc_id,
            chunk_index=chunk_index,
            title=title,
            content=content,
            meta=meta or {},
        )

    def search_documents(self, query: str, k: int = 5) -> list[dict]:
        hits = self.vectors.search(namespace="personal_documents", query=query, k=k)
        return [{"doc_id": h.doc_id, "title": h.title, "content": h.content, "score": h.score, "meta": h.meta} for h in hits]

    # ---- 5. Multi-Session Conversations ---------------------------------------------------------------
    def ensure_session(self, session_id: str | None = None, *, department: str = "assistant", project_id: str | None = None, title: str = "Main Session") -> str:
        sid = session_id or "default"
        with self.db.sync_session() as s:
            row = s.get(ConversationSessionRow, sid)
            if not row:
                s.add(ConversationSessionRow(id=sid, title=title, department=department, project_id=project_id))
                s.commit()
        return sid

    def append_message(self, session_id: str, role: str, content: str, *, agent_name: str = "", meta: dict | None = None) -> None:
        self.ensure_session(session_id)
        with self.db.sync_session() as s:
            s.add(ConversationMessageRow(session_id=session_id, role=role, agent_name=agent_name, content=content, meta=meta or {}))
            s.commit()

    def list_sessions(self) -> list[dict]:
        with self.db.sync_session() as s:
            rows = s.scalars(select(ConversationSessionRow).order_by(ConversationSessionRow.updated_at.desc())).all()
        return [{"id": r.id, "title": r.title, "department": r.department, "project_id": r.project_id, "active_agent": r.active_agent} for r in rows]

    def summary(self) -> dict:
        with self.db.sync_session() as s:
            pm = len(s.scalars(select(PersonalMemoryRow)).all())
            notes = len(s.scalars(select(NoteRow).where(NoteRow.is_trashed.is_(False))).all())
            exp = len(s.scalars(select(ExperienceLessonRow)).all())
            sess = len(s.scalars(select(ConversationSessionRow)).all())
        return {"personal_memories": pm, "notes": notes, "experience_lessons": exp, "sessions": sess}

    def clear_namespace(self, namespace: str) -> int:
        with self.db.sync_session() as s:
            res = s.execute(delete(VectorEntryRow).where(VectorEntryRow.namespace == namespace))
            s.commit()
            return int(getattr(res, "rowcount", 0) or 0)
