"""Unified 5-Way Memory & Session Service backed by SQLAlchemy (PostgreSQL + pgvector or SQLite WAL)."""
import math
import time
import uuid

import numpy as np
from sqlalchemy import delete, or_, select

from ..config import MemorySpec, Settings
from ..db.engine import DatabaseManager
from ..db.models import (
    AuditEventRow,
    ConversationMessageRow,
    ConversationSessionRow,
    ExperienceLessonRow,
    NoteRow,
    PersonalMemoryRow,
    VectorEntryRow,
)
from ..retrieval import tokens
from ..security.pii_firewall import detect_secret
from .embeddings import EmbeddingProvider, l2_normalize
from .vectors import IsolatedVectorStore, VectorHit, VectorStore

PRIVACY_LEVELS = ("public", "internal", "private", "confidential", "restricted")
PERSONAL_CATEGORIES = (
    "explicit",
    "preference",
    "temporary",
    "user_preferences",
    "project_memory",
    "command_memory",
    "conversation_memory",
)


def _normalize_personal_category(category: str | None) -> str:
    raw = str(category or "explicit").strip().lower()
    aliases = {
        "user_preference": "user_preferences",
        "preferences": "user_preferences",
        "project": "project_memory",
        "command": "command_memory",
        "conversation": "conversation_memory",
    }
    mapped = aliases.get(raw, raw)
    return mapped if mapped in PERSONAL_CATEGORIES else "explicit"


class DuplicateMemoryError(ValueError):
    """Raised when a near-identical personal memory already exists and duplicates are disallowed."""

    def __init__(self, existing_id: str, similarity: float):
        super().__init__(f"Near-duplicate personal memory already exists (id={existing_id}, similarity={similarity:.3f})")
        self.existing_id = existing_id
        self.similarity = similarity


class FiveWayMemoryService:
    """Enforces strict isolation, expiry, lifecycle auditing, & transparent multi-factor ranking across all 5 stores."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        settings: Settings | None = None,
        mem_spec: MemorySpec | None = None,
        vector_store: VectorStore | IsolatedVectorStore | None = None,
        embedder: EmbeddingProvider | None = None,
    ):
        self.db = db
        self.settings = settings
        self.mem_spec = mem_spec or (settings.memory if settings else MemorySpec())
        self.vectors = vector_store or IsolatedVectorStore(db, settings=settings, embedder=embedder)

    def _audit(self, category: str, decision: str, target: str, details: dict | None = None, actor: str = "system") -> None:
        try:
            with self.db.sync_session() as s:
                s.add(
                    AuditEventRow(
                        actor=actor,
                        category=category,
                        decision=decision,
                        target=str(target)[:500],
                        details=details or {},
                    )
                )
                s.commit()
        except Exception:
            pass

    @staticmethod
    def _personal_dict(r: PersonalMemoryRow) -> dict:
        return {
            "id": r.id,
            "namespace": r.namespace,
            "owner_scope": r.owner_scope,
            "project_id": r.project_id,
            "category": r.category,
            "scope": r.scope,
            "topic": r.topic,
            "title": r.title,
            "content": r.content,
            "meta": dict(r.meta or {}),
            "source": r.source,
            "confirmed": r.confirmed,
            "confidence": float(r.confidence),
            "privacy_level": r.privacy_level,
            "embedding_model": r.embedding_model,
            "archived": r.archived,
            "deleted_at": r.deleted_at,
            "superseded_by": r.superseded_by,
            "supersedes_id": r.supersedes_id,
            "expires_at": r.expires_at,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    def check_duplicate_personal(
        self,
        *,
        content: str,
        category: str,
        topic: str = "",
        owner_scope: str = "default",
        project_id: str | None = None,
        threshold: float | None = None,
    ) -> tuple[str | None, float]:
        """Semantic + metadata similarity duplicate check (never auto-merges unrelated memories)."""
        thresh = float(threshold or self.mem_spec.duplicate_threshold)
        q_vec = l2_normalize(self.vectors.embedder.embed_one(content))
        q_toks = set(tokens(content))
        hits = self.vectors.search(
            namespace="personal_memory",
            query=content,
            k=5,
            owner_id=owner_scope,
            project_id=project_id,
            min_score=0.4,
        )
        for h in hits:
            # Metadata boundary: distinct categories / non-empty differing topics stay separate
            if (h.meta or {}).get("category") and h.meta["category"] != category:
                continue
            h_topic = (h.meta or {}).get("topic") or ""
            if topic and h_topic and topic.strip().lower() != h_topic.strip().lower():
                continue
            r_vec = l2_normalize(self.vectors.embedder.embed_one(h.content))
            cos = float(np.dot(q_vec, r_vec))
            h_toks = set(tokens(h.content))
            jacc = (len(q_toks & h_toks) / max(1, len(q_toks | h_toks))) if (q_toks and h_toks) else 0.0
            sim = 0.7 * cos + 0.3 * jacc
            if sim >= thresh or content.strip().lower() == h.content.strip().lower():
                return h.doc_id, round(sim, 4)
        return None, 0.0

    # ---- 1. Personal Memory (User Facts, Preferences, Temporary Context) ------------------------------
    def add_personal_memory(
        self,
        *,
        content: str,
        category: str = "explicit",
        topic: str = "",
        title: str = "",
        owner_scope: str = "default",
        project_id: str | None = None,
        meta: dict | None = None,
        source: str = "explicit",
        confirmed: bool = True,
        confidence: float = 1.0,
        privacy_level: str = "private",
        ttl_seconds: float | None = None,
        expires_at: float | None = None,
        supersedes_id: str | None = None,
        allow_duplicate: bool = True,
    ) -> dict:
        if sec := detect_secret(content):
            raise ValueError(f"Refused to store sensitive credential in personal memory ({sec})")
        priv = privacy_level if privacy_level in PRIVACY_LEVELS else "private"
        cat = _normalize_personal_category(category)
        scope = "temporary" if cat in ("temporary", "conversation_memory") else "global"
        now = round(time.time(), 3)
        expires = expires_at if expires_at is not None else ((now + ttl_seconds) if ttl_seconds else None)

        if not allow_duplicate:
            dup_id, sim = self.check_duplicate_personal(
                content=content,
                category=cat,
                topic=topic,
                owner_scope=owner_scope,
                project_id=project_id,
            )
            if dup_id and dup_id != supersedes_id:
                raise DuplicateMemoryError(dup_id, sim)

        mid = uuid.uuid4().hex
        item_meta = {**(meta or {}), "category": cat, "confirmed": confirmed, "topic": topic[:120], "privacy_level": priv}

        with self.db.sync_session() as s:
            if supersedes_id:
                old = s.get(PersonalMemoryRow, supersedes_id)
                if old:
                    old.archived = True
                    old.superseded_by = mid
                    old.updated_at = now
            row = PersonalMemoryRow(
                id=mid,
                namespace="personal_memory",
                owner_scope=owner_scope,
                project_id=project_id,
                category=cat,
                scope=scope,
                topic=topic[:120],
                title=(title or content[:80])[:200],
                content=content.strip(),
                meta=item_meta,
                source=source[:32],
                confirmed=confirmed,
                confidence=float(confidence),
                privacy_level=priv,
                embedding_model=self.vectors.embedder.version_tag,
                supersedes_id=supersedes_id,
                expires_at=expires,
                created_at=now,
                updated_at=now,
            )
            s.add(row)
            s.commit()
            rec = self._personal_dict(row)

        if supersedes_id:
            self.vectors.delete_doc("personal_memory", supersedes_id, project_id=project_id)
        if confirmed:
            self.vectors.upsert(
                namespace="personal_memory",
                owner_id=owner_scope,
                project_id=project_id,
                memory_type=cat,
                doc_id=mid,
                title=title or topic,
                content=content.strip(),
                meta=item_meta,
                source=source,
                confidence=float(confidence),
                privacy_level=priv,
                expires_at=expires,
            )
        self._audit("memory", "create", mid, {"category": cat, "supersedes": supersedes_id, "privacy": priv})
        return rec

    def get_personal_memory(self, memory_id: str, *, include_archived: bool = False, include_expired: bool = False) -> dict | None:
        now = time.time()
        with self.db.sync_session() as s:
            r = s.get(PersonalMemoryRow, memory_id)
            if r is None or r.deleted_at is not None:
                return None
            if r.archived and not include_archived:
                return None
            if not include_expired and r.expires_at is not None and r.expires_at <= now:
                return None
            return self._personal_dict(r)

    def list_personal_memories(
        self,
        *,
        category: str | None = None,
        owner_scope: str | None = None,
        project_id: str | None = None,
        include_pending: bool = True,
        include_archived: bool = False,
        include_expired: bool = False,
    ) -> list[dict]:
        now = time.time()
        with self.db.sync_session() as s:
            stmt = select(PersonalMemoryRow).where(PersonalMemoryRow.deleted_at.is_(None))
            if not include_archived:
                stmt = stmt.where(PersonalMemoryRow.archived.is_(False))
            if not include_expired:
                stmt = stmt.where(or_(PersonalMemoryRow.expires_at.is_(None), PersonalMemoryRow.expires_at > now))
            if category:
                stmt = stmt.where(PersonalMemoryRow.category == category)
            if owner_scope:
                stmt = stmt.where(PersonalMemoryRow.owner_scope == owner_scope)
            if project_id is not None:
                stmt = stmt.where(PersonalMemoryRow.project_id == project_id)
            if not include_pending:
                stmt = stmt.where(PersonalMemoryRow.confirmed.is_(True))
            rows = s.scalars(stmt.order_by(PersonalMemoryRow.updated_at.desc())).all()
        return [self._personal_dict(r) for r in rows]

    def update_personal_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        title: str | None = None,
        topic: str | None = None,
        confidence: float | None = None,
        privacy_level: str | None = None,
        expires_at: float | None = None,
        meta: dict | None = None,
    ) -> dict:
        if content is not None:
            if sec := detect_secret(content):
                raise ValueError(f"Refused secret content in memory update ({sec})")
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            r = s.get(PersonalMemoryRow, memory_id)
            if not r or r.deleted_at is not None:
                raise KeyError(memory_id)
            if content is not None:
                r.content = content.strip()
            if title is not None:
                r.title = title[:200]
            if topic is not None:
                r.topic = topic[:120]
            if confidence is not None:
                r.confidence = float(confidence)
            if privacy_level in PRIVACY_LEVELS:
                r.privacy_level = str(privacy_level)
            if expires_at is not None:
                r.expires_at = expires_at
            if meta is not None:
                r.meta = {**(r.meta or {}), **meta}
            r.updated_at = now
            r.meta = {**(r.meta or {}), "topic": r.topic, "category": r.category, "confirmed": r.confirmed, "privacy_level": r.privacy_level}
            s.commit()
            rec = self._personal_dict(r)

        if rec["confirmed"] and not rec["archived"]:
            self.vectors.upsert(
                namespace="personal_memory",
                owner_id=rec["owner_scope"],
                project_id=rec["project_id"],
                memory_type=rec["category"],
                doc_id=rec["id"],
                title=rec["title"] or rec["topic"],
                content=rec["content"],
                meta=rec["meta"],
                source=rec["source"],
                confidence=rec["confidence"],
                privacy_level=rec["privacy_level"],
                expires_at=rec["expires_at"],
            )
        self._audit("memory", "update", memory_id, {"privacy": rec["privacy_level"]})
        return rec

    def supersede_personal_memory(self, old_memory_id: str, *, new_content: str, title: str = "", topic: str = "", confidence: float = 1.0) -> dict:
        with self.db.sync_session() as s:
            old = s.get(PersonalMemoryRow, old_memory_id)
            if not old or old.deleted_at is not None:
                raise KeyError(old_memory_id)
            cat = old.category
            owner_scope = old.owner_scope
            project_id = old.project_id
            old_topic = topic or old.topic
            old_priv = old.privacy_level
        new_rec = self.add_personal_memory(
            content=new_content,
            category=cat,
            topic=old_topic,
            title=title,
            owner_scope=owner_scope,
            project_id=project_id,
            privacy_level=old_priv,
            confidence=confidence,
            supersedes_id=old_memory_id,
            allow_duplicate=True,
        )
        self._audit("memory", "supersede", old_memory_id, {"superseded_by": new_rec["id"]})
        return new_rec

    def archive_personal_memory(self, memory_id: str) -> dict:
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            r = s.get(PersonalMemoryRow, memory_id)
            if not r or r.deleted_at is not None:
                raise KeyError(memory_id)
            r.archived = True
            r.updated_at = now
            pid = r.project_id
            s.commit()
            rec = self._personal_dict(r)
        self.vectors.delete_doc("personal_memory", memory_id, project_id=pid)
        self._audit("memory", "archive", memory_id)
        return rec

    def restore_personal_memory(self, memory_id: str) -> dict:
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            r = s.get(PersonalMemoryRow, memory_id)
            if not r:
                raise KeyError(memory_id)
            r.archived = False
            r.deleted_at = None
            r.updated_at = now
            s.commit()
            rec = self._personal_dict(r)
        if rec["confirmed"] and (rec["expires_at"] is None or rec["expires_at"] > now):
            self.vectors.upsert(
                namespace="personal_memory",
                owner_id=rec["owner_scope"],
                project_id=rec["project_id"],
                memory_type=rec["category"],
                doc_id=rec["id"],
                title=rec["title"] or rec["topic"],
                content=rec["content"],
                meta=rec["meta"],
                source=rec["source"],
                confidence=rec["confidence"],
                privacy_level=rec["privacy_level"],
                expires_at=rec["expires_at"],
            )
        self._audit("memory", "restore", memory_id)
        return rec

    def delete_personal_memory(self, memory_id: str, *, hard: bool = False) -> dict:
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            r = s.get(PersonalMemoryRow, memory_id)
            if not r:
                raise KeyError(memory_id)
            pid = r.project_id
            if hard:
                s.delete(r)
                status = "hard_deleted"
            else:
                r.deleted_at = now
                r.archived = True
                r.updated_at = now
                status = "soft_deleted"
            s.commit()
        vec_del = self.vectors.delete_doc("personal_memory", memory_id, project_id=pid)
        self._audit("memory", status, memory_id, {"vectors_removed": vec_del})
        return {"id": memory_id, "status": status, "vectors_removed": vec_del}

    def purge_expired(self, *, now: float | None = None) -> dict:
        """Transactionally purge expired SQL memories and their indexed vectors."""
        ts = time.time() if now is None else float(now)
        expired_ids: list[str] = []
        with self.db.sync_session() as s:
            rows = s.scalars(select(PersonalMemoryRow).where(PersonalMemoryRow.expires_at.is_not(None), PersonalMemoryRow.expires_at <= ts)).all()
            for r in rows:
                expired_ids.append(r.id)
                s.delete(r)
            s.commit()
        vec_removed = self.vectors.purge_expired(now=ts)
        if expired_ids or vec_removed:
            self._audit("memory", "purge_expired", "personal_memory", {"sql_purged": len(expired_ids), "vectors_purged": vec_removed})
        return {"sql_purged": len(expired_ids), "vectors_purged": vec_removed, "purged_ids": expired_ids}

    def rank_hits(
        self,
        query: str,
        hits: list[VectorHit],
        *,
        active_project_id: str | None = None,
        type_weights: dict[str, float] | None = None,
        now: float | None = None,
    ) -> list[dict]:
        """Apply transparent multi-factor ranking: similarity + recency + confidence + relevance + project + type."""
        ts = time.time() if now is None else float(now)
        q_toks = set(tokens(query))
        w = self.mem_spec
        t_mods = type_weights or {
            "explicit": 1.0,
            "preference": 1.05,
            "user_preferences": 1.08,
            "project_memory": 1.06,
            "command_memory": 1.02,
            "conversation_memory": 0.92,
            "temporary": 0.9,
            "fact": 1.0,
        }
        ranked: list[dict] = []
        freq_w = float(getattr(w, "ranking_frequency_weight", 0.12))
        for h in hits:
            age_days = max(0.0, (ts - float(h.updated_at or h.created_at or ts)) / 86400.0)
            recency = math.exp(-age_days / 30.0)
            conf = max(0.0, min(1.0, float(h.confidence or 1.0)))
            d_toks = set(tokens(f"{h.title} {h.content}"))
            rel = (len(q_toks & d_toks) / max(1, len(q_toks))) if q_toks else 0.0
            proj_boost = w.ranking_project_boost if (active_project_id and h.project_id == active_project_id) else 0.0
            ret_count = max(0, int((h.meta or {}).get("retrieval_count") or 0))
            freq_boost = freq_w * min(1.0, math.log1p(ret_count) / 3.0) if ret_count > 0 else 0.0
            type_mod = float(t_mods.get(h.memory_type, 1.0))
            base_score = (
                w.ranking_similarity_weight * float(h.score)
                + w.ranking_recency_weight * recency
                + w.ranking_confidence_weight * conf
                + w.ranking_relevance_weight * rel
                + proj_boost
                + freq_boost
            )
            final = round(base_score * type_mod, 4)
            ranked.append(
                {
                    "id": h.doc_id,
                    "doc_id": h.doc_id,
                    "namespace": h.namespace,
                    "project_id": h.project_id,
                    "title": h.title,
                    "content": h.content,
                    "score": final,
                    "memory_type": h.memory_type,
                    "source": h.source,
                    "confidence": conf,
                    "privacy_level": h.privacy_level,
                    "expires_at": h.expires_at,
                    "meta": h.meta,
                    "ranking": {
                        "similarity": round(float(h.score), 4),
                        "recency": round(recency, 4),
                        "confidence": round(conf, 4),
                        "relevance": round(rel, 4),
                        "project_boost": round(proj_boost, 4),
                        "frequency_boost": round(freq_boost, 4),
                        "type_modifier": type_mod,
                    },
                }
            )
        ranked.sort(key=lambda r: -r["score"])
        return ranked

    def is_category_enabled(self, category: str | None) -> bool:
        w = self.mem_spec
        if not bool(getattr(w, "enabled", True)):
            return False
        cat = _normalize_personal_category(category)
        if cat in ("preference", "user_preferences"):
            return bool(getattr(w, "enable_user_preferences", True))
        if cat == "project_memory":
            return bool(getattr(w, "enable_project_memory", True))
        if cat == "command_memory":
            return bool(getattr(w, "enable_command_memory", True))
        if cat in ("temporary", "conversation_memory"):
            return bool(getattr(w, "enable_conversation_memory", True))
        return True

    def mark_retrieved(self, memory_ids: list[str]) -> int:
        """Increment retrieval_count in DB and vector metadata for adaptive frequency ranking."""
        ids = [mid for mid in dict.fromkeys(memory_ids) if mid]
        if not ids:
            return 0
        now = round(time.time(), 3)
        updated: list[dict] = []
        with self.db.sync_session() as s:
            rows = s.scalars(select(PersonalMemoryRow).where(PersonalMemoryRow.id.in_(ids))).all()
            for r in rows:
                meta = dict(r.meta or {})
                meta["retrieval_count"] = int(meta.get("retrieval_count") or 0) + 1
                meta["last_retrieved_at"] = now
                r.meta = meta
                updated.append(self._personal_dict(r))
            s.commit()
        for rec in updated:
            self.vectors.upsert(
                namespace="personal_memory",
                owner_id=rec["owner_scope"],
                project_id=rec["project_id"],
                memory_type=rec["category"],
                doc_id=rec["id"],
                title=rec["title"] or rec["topic"],
                content=rec["content"],
                meta=rec["meta"],
                source=rec["source"],
                confidence=rec["confidence"],
                privacy_level=rec["privacy_level"],
                expires_at=rec["expires_at"],
            )
        return len(updated)

    def record_adaptive_memory(
        self,
        *,
        content: str,
        category: str,
        topic: str = "",
        title: str = "",
        project_id: str | None = None,
        source: str = "command_derived",
        meta: dict | None = None,
        strict_secret: bool = False,
    ) -> dict | None:
        """Safely record adaptive preference/project/command/conversation memory respecting toggles & secret exclusions."""
        cleaned = (content or "").strip()
        if not cleaned:
            return None
        cat = _normalize_personal_category(category)
        if not self.is_category_enabled(cat):
            return None
        if sec := detect_secret(cleaned):
            self._audit("memory", "rejected_secret", cat, {"kind": sec, "source": source})
            if strict_secret:
                raise ValueError(f"Refused to store sensitive credential in adaptive memory ({sec})")
            return None
        dup_id, _ = self.check_duplicate_personal(
            content=cleaned,
            category=cat,
            topic=topic,
            project_id=project_id,
        )
        if dup_id:
            self.mark_retrieved([dup_id])
            return self.get_personal_memory(dup_id)
        return self.add_personal_memory(
            content=cleaned,
            category=cat,
            topic=topic[:120],
            title=(title or topic or cleaned[:60])[:200],
            project_id=project_id,
            meta={"retrieval_count": 1, **(meta or {})},
            source=source,
            confirmed=True,
            confidence=0.85 if source.endswith("_derived") else 1.0,
            allow_duplicate=True,
        )

    def clear_conversation_memory(self) -> dict:
        """Delete all conversation-derived and temporary personal memories."""
        now = round(time.time(), 3)
        removed: list[tuple[str, str | None]] = []
        with self.db.sync_session() as s:
            rows = s.scalars(
                select(PersonalMemoryRow).where(
                    PersonalMemoryRow.deleted_at.is_(None),
                    or_(
                        PersonalMemoryRow.category.in_(("conversation_memory", "temporary")),
                        PersonalMemoryRow.source == "conversation_derived",
                    ),
                )
            ).all()
            for r in rows:
                removed.append((r.id, r.project_id))
                r.deleted_at = now
                r.archived = True
                r.updated_at = now
            s.commit()
        for mid, pid in removed:
            self.vectors.delete_doc("personal_memory", mid, project_id=pid)
        self._audit("memory", "clear_conversation", "personal_memory", {"cleared_count": len(removed)})
        return {"ok": True, "cleared_count": len(removed)}

    def category_summary(self) -> dict:
        now = time.time()
        counts: dict[str, int] = {c: 0 for c in ("user_preferences", "project_memory", "command_memory", "conversation_memory", "explicit")}
        with self.db.sync_session() as s:
            rows = s.scalars(
                select(PersonalMemoryRow).where(
                    PersonalMemoryRow.deleted_at.is_(None),
                    PersonalMemoryRow.archived.is_(False),
                    or_(PersonalMemoryRow.expires_at.is_(None), PersonalMemoryRow.expires_at > now),
                )
            ).all()
        for r in rows:
            c = _normalize_personal_category(r.category)
            bucket = "user_preferences" if c in ("preference", "user_preferences") else ("conversation_memory" if c in ("temporary", "conversation_memory") else c)
            counts[bucket] = counts.get(bucket, 0) + 1
        return {
            "enabled": bool(getattr(self.mem_spec, "enabled", True)),
            "toggles": {
                "user_preferences": bool(getattr(self.mem_spec, "enable_user_preferences", True)),
                "project_memory": bool(getattr(self.mem_spec, "enable_project_memory", True)),
                "command_memory": bool(getattr(self.mem_spec, "enable_command_memory", True)),
                "conversation_memory": bool(getattr(self.mem_spec, "enable_conversation_memory", True)),
            },
            "counts": counts,
        }

    def search_personal_memory(
        self,
        query: str,
        k: int = 5,
        *,
        owner_scope: str | None = None,
        project_id: str | None = None,
        category: str | None = None,
        now: float | None = None,
    ) -> list[dict]:
        if not bool(getattr(self.mem_spec, "enabled", True)):
            return []
        ts = time.time() if now is None else float(now)
        hits = self.vectors.search(
            namespace="personal_memory",
            query=query,
            k=max(k * 3, k),
            owner_id=owner_scope,
            project_id=project_id,
            meta_filter={"confirmed": True},
            now=ts,
        )
        # Double-verify SQL row status, category toggles & expiry in case an operation was interrupted
        valid_ids = set()
        if hits:
            doc_ids = [h.doc_id for h in hits]
            with self.db.sync_session() as s:
                rows = s.scalars(
                    select(PersonalMemoryRow).where(
                        PersonalMemoryRow.id.in_(doc_ids),
                        PersonalMemoryRow.archived.is_(False),
                        PersonalMemoryRow.deleted_at.is_(None),
                        or_(PersonalMemoryRow.expires_at.is_(None), PersonalMemoryRow.expires_at > ts),
                    )
                ).all()
                norm_cat = _normalize_personal_category(category) if category else None
                valid_ids = {
                    r.id
                    for r in rows
                    if self.is_category_enabled(r.category)
                    and (not norm_cat or _normalize_personal_category(r.category) == norm_cat)
                }
        filtered = [h for h in hits if h.doc_id in valid_ids]
        return self.rank_hits(query, filtered, active_project_id=project_id, now=ts)[:k]

    # ---- 2. Project Knowledge (Strictly Isolated per project_id) --------------------------------------
    def index_project_knowledge(
        self,
        *,
        project_id: str,
        doc_id: str,
        title: str,
        content: str,
        meta: dict | None = None,
        privacy_level: str = "private",
        expires_at: float | None = None,
    ) -> str:
        if not project_id:
            raise ValueError("project_id is strictly required for project_knowledge")
        return self.vectors.upsert(
            namespace="project_knowledge",
            project_id=project_id,
            doc_id=doc_id,
            title=title,
            content=content,
            meta=meta or {},
            source="project_code",
            privacy_level=privacy_level,
            expires_at=expires_at,
        )

    def search_project_knowledge(self, *, project_id: str, query: str, k: int = 5, now: float | None = None) -> list[dict]:
        if not project_id:
            raise ValueError("project_id is strictly required for isolated project search")
        hits = self.vectors.search(namespace="project_knowledge", project_id=project_id, query=query, k=k, now=now)
        # Defense in depth: strictly assert project_id equality
        isolated = [h for h in hits if h.project_id == project_id]
        return self.rank_hits(query, isolated, active_project_id=project_id, now=now)[:k]

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
        expires_at: float | None = None,
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
                memory_type="lesson",
                doc_id=lid,
                title=f"[{scope}/{category}] {topic}",
                content=f"Situation: {trigger_context}\nLesson: {lesson_after}",
                meta={"scope": scope, "approved": True, "category": category},
                source=source,
                expires_at=expires_at,
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
        return self.rank_hits(query, hits, active_project_id=project_id)[:k]

    # ---- 4. Personal Documents Index ------------------------------------------------------------------
    def index_document_chunk(self, *, doc_id: str, chunk_index: int, title: str, content: str, meta: dict | None = None, expires_at: float | None = None) -> str:
        return self.vectors.upsert(
            namespace="personal_documents",
            memory_type="document_chunk",
            doc_id=doc_id,
            chunk_index=chunk_index,
            title=title,
            content=content,
            meta=meta or {},
            source="document",
            expires_at=expires_at,
        )

    def search_documents(self, query: str, k: int = 5) -> list[dict]:
        hits = self.vectors.search(namespace="personal_documents", query=query, k=k)
        return self.rank_hits(query, hits)[:k]

    # ---- 5. Multi-Session Conversations & General Scoped Search ---------------------------------------
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
        now = time.time()
        with self.db.sync_session() as s:
            pm = len(
                s.scalars(
                    select(PersonalMemoryRow).where(
                        PersonalMemoryRow.deleted_at.is_(None),
                        PersonalMemoryRow.archived.is_(False),
                        or_(PersonalMemoryRow.expires_at.is_(None), PersonalMemoryRow.expires_at > now),
                    )
                ).all()
            )
            notes = len(s.scalars(select(NoteRow).where(NoteRow.is_trashed.is_(False))).all())
            exp = len(s.scalars(select(ExperienceLessonRow)).all())
            sess = len(s.scalars(select(ConversationSessionRow)).all())
        return {
            "personal_memories": pm,
            "notes": notes,
            "experience_lessons": exp,
            "sessions": sess,
            "vector_backend": type(getattr(self.vectors, "_backend", self.vectors)).__name__,
            "embedding_model": self.vectors.embedder.version_tag,
        }

    def clear_namespace(self, namespace: str) -> int:
        with self.db.sync_session() as s:
            res = s.execute(delete(VectorEntryRow).where(VectorEntryRow.namespace == namespace))
            s.commit()
            return int(getattr(res, "rowcount", 0) or 0)
