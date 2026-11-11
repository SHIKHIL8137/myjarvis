"""VectorStore interface with SQLiteVectorStore and native PostgreSQL pgvector PgVectorStore backends."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import logging
import math
import time
import uuid

import numpy as np
from sqlalchemy import delete, or_, select, text

from ..db.engine import DatabaseManager
from ..db.models import VectorEntryRow
from ..retrieval import tokens
from .embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingCompatibilityError,
    EmbeddingProvider,
    create_embedding_provider,
    validate_embedding_vector,
)

log = logging.getLogger("jarvis.memory.vectors")

MEMORY_NAMESPACES = (
    "personal_memory",
    "project_knowledge",
    "experience_memory",
    "external_knowledge",
    "personal_documents",
)

_PG_METRIC_OPS = {
    "cosine": ("<=>", "vector_cosine_ops"),
    "l2": ("<->", "vector_l2_ops"),
    "ip": ("<#>", "vector_ip_ops"),
}


def format_pgvector_literal(vec: np.ndarray) -> str:
    """Serialize float32 1-D vector to pgvector literal `[0.1,0.2,...]`."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vec.reshape(-1)) + "]"


@dataclass
class VectorHit:
    id: str
    namespace: str
    project_id: str | None
    doc_id: str
    title: str
    content: str
    score: float
    meta: dict
    memory_type: str = "fact"
    source: str = "system"
    confidence: float = 1.0
    privacy_level: str = "private"
    embedding_model: str = "hash-v1:256"
    expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class VectorStore(ABC):
    """Unified semantic vector retrieval interface across SQLite and PostgreSQL+pgvector."""

    embedder: EmbeddingProvider
    dim: int
    metric: str

    @abstractmethod
    def upsert(
        self,
        *,
        namespace: str,
        doc_id: str,
        content: str,
        title: str = "",
        project_id: str | None = None,
        owner_id: str = "default",
        chunk_index: int = 0,
        meta: dict | None = None,
        memory_type: str = "fact",
        source: str = "system",
        confidence: float = 1.0,
        privacy_level: str = "private",
        expires_at: float | None = None,
        embedding: np.ndarray | None = None,
    ) -> str: ...

    @abstractmethod
    def upsert_batch(self, items: list[dict]) -> list[str]: ...

    @abstractmethod
    def delete_doc(self, namespace: str, doc_id: str, project_id: str | None = None) -> int: ...

    @abstractmethod
    def purge_expired(self, now: float | None = None) -> int: ...

    @abstractmethod
    def search(
        self,
        *,
        namespace: str,
        query: str,
        k: int = 5,
        project_id: str | None = None,
        owner_id: str | None = None,
        memory_type: str | None = None,
        meta_filter: dict | None = None,
        min_score: float = 0.05,
        include_expired: bool = False,
        now: float | None = None,
    ) -> list[VectorHit]: ...

    @abstractmethod
    def count(self, namespace: str | None = None) -> int: ...


def _validate_scope(namespace: str, project_id: str | None) -> None:
    if namespace not in MEMORY_NAMESPACES:
        raise ValueError(f"Unsupported vector namespace: {namespace!r}")
    if namespace == "project_knowledge" and not project_id:
        raise ValueError("project_knowledge vectors MUST include project_id for strict isolation")


def _entry_id(namespace: str, project_id: str | None, doc_id: str, chunk_index: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{project_id or ''}:{doc_id}:{chunk_index}").hex[:32]


def _compute_distance_score(q_vec: np.ndarray, r_vec: np.ndarray, metric: str) -> float:
    if metric == "l2":
        dist = float(np.linalg.norm(q_vec - r_vec))
        return max(0.0, 1.0 / (1.0 + dist))
    if metric == "ip":
        return max(0.0, float(np.dot(q_vec, r_vec)))
    # default cosine (both normalized)
    return max(0.0, float(np.dot(q_vec, r_vec)))


class SQLiteVectorStore(VectorStore):
    """SQLite-backed vector store enforcing strict scope, expiry, & embedding compatibility."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        dim: int = 256,
        embedder: EmbeddingProvider | None = None,
        metric: str = "cosine",
    ):
        self.db = db
        self.embedder = embedder or DeterministicHashEmbeddingProvider(dim=dim)
        self.dim = int(self.embedder.dim)
        if dim != self.dim and embedder is None:
            self.embedder = DeterministicHashEmbeddingProvider(dim=dim)
            self.dim = int(dim)
        self.metric = metric if metric in _PG_METRIC_OPS else "cosine"

    def _prepare_vec(self, text_val: str, provided: np.ndarray | None = None) -> np.ndarray:
        if provided is not None:
            return validate_embedding_vector(provided, self.dim)
        vec = self.embedder.embed_one(text_val)
        return validate_embedding_vector(vec, self.dim)

    def upsert(
        self,
        *,
        namespace: str,
        doc_id: str,
        content: str,
        title: str = "",
        project_id: str | None = None,
        owner_id: str = "default",
        chunk_index: int = 0,
        meta: dict | None = None,
        memory_type: str = "fact",
        source: str = "system",
        confidence: float = 1.0,
        privacy_level: str = "private",
        expires_at: float | None = None,
        embedding: np.ndarray | None = None,
    ) -> str:
        _validate_scope(namespace, project_id)
        emb = self._prepare_vec(f"{title}\n{content}".strip(), provided=embedding)
        blob = emb.astype("<f4").tobytes()
        entry_id = _entry_id(namespace, project_id, doc_id, chunk_index)
        now = round(time.time(), 3)

        with self.db.sync_session() as s:
            existing = s.get(VectorEntryRow, entry_id)
            if existing:
                existing.owner_id = owner_id
                existing.memory_type = memory_type
                existing.title = title[:250]
                existing.content = content
                existing.meta = meta or {}
                existing.source = source[:64]
                existing.confidence = float(confidence)
                existing.privacy_level = privacy_level
                existing.embedding_model = self.embedder.version_tag
                existing.dim = self.dim
                existing.embedding_blob = blob
                existing.expires_at = expires_at
                existing.updated_at = now
            else:
                s.add(
                    VectorEntryRow(
                        id=entry_id,
                        namespace=namespace,
                        owner_id=owner_id,
                        project_id=project_id,
                        memory_type=memory_type,
                        doc_id=doc_id,
                        chunk_index=chunk_index,
                        title=title[:250],
                        content=content,
                        meta=meta or {},
                        source=source[:64],
                        confidence=float(confidence),
                        privacy_level=privacy_level,
                        embedding_model=self.embedder.version_tag,
                        dim=self.dim,
                        embedding_blob=blob,
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
            s.commit()
        return entry_id

    def upsert_batch(self, items: list[dict]) -> list[str]:
        if not items:
            return []
        to_embed: list[tuple[int, str]] = []
        vectors: list[np.ndarray | None] = [None] * len(items)
        for idx, it in enumerate(items):
            _validate_scope(it["namespace"], it.get("project_id"))
            if it.get("embedding") is not None:
                vectors[idx] = validate_embedding_vector(it["embedding"], self.dim)
            else:
                to_embed.append((idx, f"{it.get('title', '')}\n{it['content']}".strip()))
        if to_embed:
            batch_vecs = self.embedder.embed_batch([t for _, t in to_embed])
            for (idx, _), vec in zip(to_embed, batch_vecs, strict=True):
                vectors[idx] = validate_embedding_vector(vec, self.dim)

        now = round(time.time(), 3)
        ids: list[str] = []
        with self.db.sync_session() as s:
            for idx, it in enumerate(items):
                ns = it["namespace"]
                pid = it.get("project_id")
                doc_id = it["doc_id"]
                cidx = int(it.get("chunk_index", 0))
                eid = _entry_id(ns, pid, doc_id, cidx)
                item_vec = vectors[idx]
                assert item_vec is not None
                blob = item_vec.astype("<f4").tobytes()
                existing = s.get(VectorEntryRow, eid)
                if existing:
                    existing.title = str(it.get("title", ""))[:250]
                    existing.content = it["content"]
                    existing.meta = it.get("meta") or {}
                    existing.memory_type = str(it.get("memory_type", "fact"))
                    existing.source = str(it.get("source", "system"))[:64]
                    existing.confidence = float(it.get("confidence", 1.0))
                    existing.privacy_level = str(it.get("privacy_level", "private"))
                    existing.embedding_model = self.embedder.version_tag
                    existing.dim = self.dim
                    existing.embedding_blob = blob
                    existing.expires_at = it.get("expires_at")
                    existing.updated_at = now
                else:
                    s.add(
                        VectorEntryRow(
                            id=eid,
                            namespace=ns,
                            owner_id=str(it.get("owner_id", "default")),
                            project_id=pid,
                            memory_type=str(it.get("memory_type", "fact")),
                            doc_id=doc_id,
                            chunk_index=cidx,
                            title=str(it.get("title", ""))[:250],
                            content=it["content"],
                            meta=it.get("meta") or {},
                            source=str(it.get("source", "system"))[:64],
                            confidence=float(it.get("confidence", 1.0)),
                            privacy_level=str(it.get("privacy_level", "private")),
                            embedding_model=self.embedder.version_tag,
                            dim=self.dim,
                            embedding_blob=blob,
                            expires_at=it.get("expires_at"),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                ids.append(eid)
            s.commit()
        return ids

    def delete_doc(self, namespace: str, doc_id: str, project_id: str | None = None) -> int:
        with self.db.sync_session() as s:
            stmt = delete(VectorEntryRow).where(VectorEntryRow.namespace == namespace, VectorEntryRow.doc_id == doc_id)
            if project_id is not None:
                stmt = stmt.where(VectorEntryRow.project_id == project_id)
            res = s.execute(stmt)
            s.commit()
            return int(getattr(res, "rowcount", 0) or 0)

    def purge_expired(self, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        with self.db.sync_session() as s:
            res = s.execute(delete(VectorEntryRow).where(VectorEntryRow.expires_at.is_not(None), VectorEntryRow.expires_at <= ts))
            s.commit()
            return int(getattr(res, "rowcount", 0) or 0)

    def search(
        self,
        *,
        namespace: str,
        query: str,
        k: int = 5,
        project_id: str | None = None,
        owner_id: str | None = None,
        memory_type: str | None = None,
        meta_filter: dict | None = None,
        min_score: float = 0.05,
        include_expired: bool = False,
        now: float | None = None,
    ) -> list[VectorHit]:
        _validate_scope(namespace, project_id)
        ts = time.time() if now is None else float(now)
        q_vec = validate_embedding_vector(self.embedder.embed_one(query), self.dim)
        q_toks = set(tokens(query))

        with self.db.sync_session() as s:
            stmt = select(VectorEntryRow).where(
                VectorEntryRow.namespace == namespace,
                VectorEntryRow.embedding_model == self.embedder.version_tag,
                VectorEntryRow.dim == self.dim,
            )
            if owner_id is not None:
                stmt = stmt.where(VectorEntryRow.owner_id == owner_id)
            if project_id is not None:
                stmt = stmt.where(VectorEntryRow.project_id == project_id)
            if memory_type is not None:
                stmt = stmt.where(VectorEntryRow.memory_type == memory_type)
            if not include_expired:
                stmt = stmt.where(or_(VectorEntryRow.expires_at.is_(None), VectorEntryRow.expires_at > ts))
            rows = s.scalars(stmt).all()

        scored: list[VectorHit] = []
        for row in rows:
            if meta_filter and any((row.meta or {}).get(mk) != mv for mk, mv in meta_filter.items()):
                continue
            r_vec = np.frombuffer(row.embedding_blob, dtype="<f4")
            if len(r_vec) != self.dim:
                raise EmbeddingCompatibilityError(f"Stored vector dimension {len(r_vec)} != index dimension {self.dim}")
            sim = _compute_distance_score(q_vec, r_vec, self.metric)
            doc_toks = set(tokens(f"{row.title} {row.content}"))
            lex = (len(q_toks & doc_toks) / math.sqrt(max(1, len(q_toks) * len(doc_toks)))) if (q_toks and doc_toks) else 0.0
            hybrid = 0.65 * sim + 0.35 * lex
            if hybrid >= min_score:
                scored.append(
                    VectorHit(
                        id=row.id,
                        namespace=row.namespace,
                        project_id=row.project_id,
                        doc_id=row.doc_id,
                        title=row.title,
                        content=row.content,
                        score=round(hybrid, 4),
                        meta=dict(row.meta or {}),
                        memory_type=row.memory_type,
                        source=row.source,
                        confidence=float(row.confidence),
                        privacy_level=row.privacy_level,
                        embedding_model=row.embedding_model,
                        expires_at=row.expires_at,
                        created_at=float(row.created_at),
                        updated_at=float(row.updated_at),
                    )
                )
        scored.sort(key=lambda h: -h.score)
        return scored[:k]

    def count(self, namespace: str | None = None) -> int:
        with self.db.sync_session() as s:
            stmt = select(VectorEntryRow)
            if namespace:
                stmt = stmt.where(VectorEntryRow.namespace == namespace)
            return len(s.scalars(stmt).all())


class PgVectorStore(VectorStore):
    """Native PostgreSQL + pgvector store performing DB-side ANN similarity & metadata/expiry SQL filtering."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        dim: int = 256,
        embedder: EmbeddingProvider | None = None,
        index_type: str = "hnsw",
        metric: str = "cosine",
        table_name: str = "pg_vector_entries",
    ):
        self.db = db
        self.embedder = embedder or DeterministicHashEmbeddingProvider(dim=dim)
        self.dim = int(self.embedder.dim)
        if dim != self.dim and embedder is None:
            self.embedder = DeterministicHashEmbeddingProvider(dim=dim)
            self.dim = int(dim)
        self.index_type = index_type if index_type in ("hnsw", "ivfflat") else "hnsw"
        self.metric = metric if metric in _PG_METRIC_OPS else "cosine"
        self.table_name = table_name
        self._schema_ready = False

    def ensure_schema(self) -> dict:
        if not self.db.is_postgres:
            raise RuntimeError("PgVectorStore requires a PostgreSQL database URL")
        _, ops_cls = _PG_METRIC_OPS[self.metric]
        idx_name = f"ix_{self.table_name}_{self.index_type}_{self.metric}"
        with self.db.sync_engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.table_name} (
                        id VARCHAR(36) PRIMARY KEY,
                        namespace VARCHAR(48) NOT NULL,
                        owner_id VARCHAR(64) NOT NULL DEFAULT 'default',
                        project_id VARCHAR(36),
                        memory_type VARCHAR(32) NOT NULL DEFAULT 'fact',
                        doc_id VARCHAR(120) NOT NULL,
                        chunk_index INTEGER NOT NULL DEFAULT 0,
                        title VARCHAR(250) NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        meta JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        source VARCHAR(64) NOT NULL DEFAULT 'system',
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                        privacy_level VARCHAR(24) NOT NULL DEFAULT 'private',
                        embedding_model VARCHAR(120) NOT NULL,
                        dim INTEGER NOT NULL,
                        embedding vector({self.dim}) NOT NULL,
                        expires_at DOUBLE PRECISION,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )
                    """
                )
            )
            # Validate dimension compatibility if table already existed
            existing_dim = conn.execute(text(f"SELECT dim FROM {self.table_name} LIMIT 1")).scalar()
            if existing_dim is not None and int(existing_dim) != self.dim:
                raise EmbeddingCompatibilityError(
                    f"Existing pgvector table {self.table_name} holds dim={existing_dim}, cannot mix with dim={self.dim}"
                )
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{self.table_name}_scope ON {self.table_name} (namespace, owner_id, project_id, memory_type)"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{self.table_name}_exp ON {self.table_name} (expires_at)"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{self.table_name}_meta ON {self.table_name} USING gin (meta)"))
            try:
                if self.index_type == "hnsw":
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table_name} USING hnsw (embedding {ops_cls})"))
                else:
                    conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table_name} USING ivfflat (embedding {ops_cls}) WITH (lists = 100)")
                    )
            except Exception as e:
                log.warning("pgvector %s index creation deferred/skipped: %s", self.index_type, e)
        self._schema_ready = True
        return {"table": self.table_name, "dim": self.dim, "index_type": self.index_type, "metric": self.metric}

    def _ensure_ready(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def build_search_query(
        self,
        *,
        namespace: str,
        project_id: str | None = None,
        owner_id: str | None = None,
        memory_type: str | None = None,
        meta_filter: dict | None = None,
        include_expired: bool = False,
        candidate_limit: int = 15,
    ) -> tuple[str, dict]:
        """Build parameterized PostgreSQL pgvector similarity SQL without loading full table into Python."""
        op_sym, _ = _PG_METRIC_OPS[self.metric]
        if self.metric == "l2":
            sim_expr = f"(1.0 / (1.0 + (embedding {op_sym} CAST(:qvec AS vector))))"
        elif self.metric == "ip":
            sim_expr = f"(-(embedding {op_sym} CAST(:qvec AS vector)))"
        else:
            sim_expr = f"(1.0 - (embedding {op_sym} CAST(:qvec AS vector)))"

        clauses = ["namespace = :namespace", "embedding_model = :embedding_model", "dim = :dim"]
        params: dict = {"namespace": namespace, "embedding_model": self.embedder.version_tag, "dim": self.dim, "limit": candidate_limit}
        if owner_id is not None:
            clauses.append("owner_id = :owner_id")
            params["owner_id"] = owner_id
        if project_id is not None:
            clauses.append("project_id = :project_id")
            params["project_id"] = project_id
        if memory_type is not None:
            clauses.append("memory_type = :memory_type")
            params["memory_type"] = memory_type
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > :now_ts)")
        if meta_filter:
            clauses.append("meta @> CAST(:meta_json AS jsonb)")
            params["meta_json"] = json.dumps(meta_filter)

        sql = (
            f"SELECT id, namespace, project_id, doc_id, title, content, meta, memory_type, source, "
            f"confidence, privacy_level, embedding_model, expires_at, created_at, updated_at, {sim_expr} AS similarity "
            f"FROM {self.table_name} WHERE {' AND '.join(clauses)} "
            f"ORDER BY embedding {op_sym} CAST(:qvec AS vector) ASC LIMIT :limit"
        )
        return sql, params

    def upsert_batch(self, items: list[dict]) -> list[str]:
        if not items:
            return []
        self._ensure_ready()
        to_embed: list[tuple[int, str]] = []
        vectors: list[np.ndarray | None] = [None] * len(items)
        for idx, it in enumerate(items):
            _validate_scope(it["namespace"], it.get("project_id"))
            if it.get("embedding") is not None:
                vectors[idx] = validate_embedding_vector(it["embedding"], self.dim)
            else:
                to_embed.append((idx, f"{it.get('title', '')}\n{it['content']}".strip()))
        if to_embed:
            batch_vecs = self.embedder.embed_batch([t for _, t in to_embed])
            for (idx, _), vec in zip(to_embed, batch_vecs, strict=True):
                vectors[idx] = validate_embedding_vector(vec, self.dim)

        now = round(time.time(), 3)
        sql = text(
            f"""
            INSERT INTO {self.table_name} (
                id, namespace, owner_id, project_id, memory_type, doc_id, chunk_index, title, content,
                meta, source, confidence, privacy_level, embedding_model, dim, embedding, expires_at, created_at, updated_at
            ) VALUES (
                :id, :namespace, :owner_id, :project_id, :memory_type, :doc_id, :chunk_index, :title, :content,
                CAST(:meta AS jsonb), :source, :confidence, :privacy_level, :embedding_model, :dim,
                CAST(:embedding AS vector), :expires_at, :created_at, :updated_at
            )
            ON CONFLICT (id) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                memory_type = EXCLUDED.memory_type,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                meta = EXCLUDED.meta,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                privacy_level = EXCLUDED.privacy_level,
                embedding_model = EXCLUDED.embedding_model,
                dim = EXCLUDED.dim,
                embedding = EXCLUDED.embedding,
                expires_at = EXCLUDED.expires_at,
                updated_at = EXCLUDED.updated_at
            """
        )
        rows_payload: list[dict] = []
        ids: list[str] = []
        for idx, it in enumerate(items):
            ns = it["namespace"]
            pid = it.get("project_id")
            doc_id = it["doc_id"]
            cidx = int(it.get("chunk_index", 0))
            eid = _entry_id(ns, pid, doc_id, cidx)
            item_vec = vectors[idx]
            assert item_vec is not None
            rows_payload.append(
                {
                    "id": eid,
                    "namespace": ns,
                    "owner_id": str(it.get("owner_id", "default")),
                    "project_id": pid,
                    "memory_type": str(it.get("memory_type", "fact")),
                    "doc_id": doc_id,
                    "chunk_index": cidx,
                    "title": str(it.get("title", ""))[:250],
                    "content": it["content"],
                    "meta": json.dumps(it.get("meta") or {}),
                    "source": str(it.get("source", "system"))[:64],
                    "confidence": float(it.get("confidence", 1.0)),
                    "privacy_level": str(it.get("privacy_level", "private")),
                    "embedding_model": self.embedder.version_tag,
                    "dim": self.dim,
                    "embedding": format_pgvector_literal(item_vec),
                    "expires_at": it.get("expires_at"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            ids.append(eid)
        with self.db.sync_engine.begin() as conn:
            conn.execute(sql, rows_payload)
        return ids

    def upsert(
        self,
        *,
        namespace: str,
        doc_id: str,
        content: str,
        title: str = "",
        project_id: str | None = None,
        owner_id: str = "default",
        chunk_index: int = 0,
        meta: dict | None = None,
        memory_type: str = "fact",
        source: str = "system",
        confidence: float = 1.0,
        privacy_level: str = "private",
        expires_at: float | None = None,
        embedding: np.ndarray | None = None,
    ) -> str:
        return self.upsert_batch(
            [
                {
                    "namespace": namespace,
                    "doc_id": doc_id,
                    "content": content,
                    "title": title,
                    "project_id": project_id,
                    "owner_id": owner_id,
                    "chunk_index": chunk_index,
                    "meta": meta or {},
                    "memory_type": memory_type,
                    "source": source,
                    "confidence": confidence,
                    "privacy_level": privacy_level,
                    "expires_at": expires_at,
                    "embedding": embedding,
                }
            ]
        )[0]

    def delete_doc(self, namespace: str, doc_id: str, project_id: str | None = None) -> int:
        self._ensure_ready()
        sql = f"DELETE FROM {self.table_name} WHERE namespace = :ns AND doc_id = :doc_id"
        params: dict = {"ns": namespace, "doc_id": doc_id}
        if project_id is not None:
            sql += " AND project_id = :pid"
            params["pid"] = project_id
        with self.db.sync_engine.begin() as conn:
            res = conn.execute(text(sql), params)
            return int(getattr(res, "rowcount", 0) or 0)

    def purge_expired(self, now: float | None = None) -> int:
        self._ensure_ready()
        ts = time.time() if now is None else float(now)
        with self.db.sync_engine.begin() as conn:
            res = conn.execute(
                text(f"DELETE FROM {self.table_name} WHERE expires_at IS NOT NULL AND expires_at <= :ts"),
                {"ts": ts},
            )
            return int(getattr(res, "rowcount", 0) or 0)

    def search(
        self,
        *,
        namespace: str,
        query: str,
        k: int = 5,
        project_id: str | None = None,
        owner_id: str | None = None,
        memory_type: str | None = None,
        meta_filter: dict | None = None,
        min_score: float = 0.05,
        include_expired: bool = False,
        now: float | None = None,
    ) -> list[VectorHit]:
        _validate_scope(namespace, project_id)
        self._ensure_ready()
        ts = time.time() if now is None else float(now)
        q_vec = validate_embedding_vector(self.embedder.embed_one(query), self.dim)
        q_toks = set(tokens(query))
        sql, params = self.build_search_query(
            namespace=namespace,
            project_id=project_id,
            owner_id=owner_id,
            memory_type=memory_type,
            meta_filter=meta_filter,
            include_expired=include_expired,
            candidate_limit=max(k * 3, k),
        )
        params["qvec"] = format_pgvector_literal(q_vec)
        if not include_expired:
            params["now_ts"] = ts

        with self.db.sync_engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()

        hits: list[VectorHit] = []
        for r in rows:
            sim = max(0.0, float(r["similarity"] or 0.0))
            doc_toks = set(tokens(f"{r['title']} {r['content']}"))
            lex = (len(q_toks & doc_toks) / math.sqrt(max(1, len(q_toks) * len(doc_toks)))) if (q_toks and doc_toks) else 0.0
            hybrid = 0.75 * sim + 0.25 * lex
            if hybrid >= min_score:
                hits.append(
                    VectorHit(
                        id=r["id"],
                        namespace=r["namespace"],
                        project_id=r["project_id"],
                        doc_id=r["doc_id"],
                        title=r["title"],
                        content=r["content"],
                        score=round(hybrid, 4),
                        meta=dict(r["meta"] or {}),
                        memory_type=r["memory_type"],
                        source=r["source"],
                        confidence=float(r["confidence"]),
                        privacy_level=r["privacy_level"],
                        embedding_model=r["embedding_model"],
                        expires_at=r["expires_at"],
                        created_at=float(r["created_at"]),
                        updated_at=float(r["updated_at"]),
                    )
                )
        hits.sort(key=lambda h: -h.score)
        return hits[:k]

    def count(self, namespace: str | None = None) -> int:
        self._ensure_ready()
        with self.db.sync_engine.connect() as conn:
            if namespace:
                return int(conn.execute(text(f"SELECT COUNT(*) FROM {self.table_name} WHERE namespace = :ns"), {"ns": namespace}).scalar() or 0)
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {self.table_name}")).scalar() or 0)


def create_vector_store(db: DatabaseManager, *, settings=None, embedder: EmbeddingProvider | None = None) -> VectorStore:
    """Factory choosing native PgVectorStore when PostgreSQL+pgvector is available, else SQLiteVectorStore."""
    emb = embedder or create_embedding_provider(settings)
    db_spec = getattr(settings, "database", None)
    idx_type = str(getattr(db_spec, "vector_index_type", "hnsw") or "hnsw")
    metric = str(getattr(db_spec, "vector_metric", "cosine") or "cosine")
    if db.is_postgres and db.pgvector_available:
        return PgVectorStore(db, dim=emb.dim, embedder=emb, index_type=idx_type, metric=metric)
    return SQLiteVectorStore(db, dim=emb.dim, embedder=emb, metric=metric)


class IsolatedVectorStore:
    """Backwards-compatible adapter delegating seamlessly to SQLiteVectorStore or PgVectorStore."""

    def __init__(self, db: DatabaseManager, dim: int = 256, *, settings=None, embedder: EmbeddingProvider | None = None):
        if settings is None and embedder is None:
            embedder = DeterministicHashEmbeddingProvider(dim=dim)
        self._backend: VectorStore = create_vector_store(db, settings=settings, embedder=embedder)
        self.db = db
        self.dim = self._backend.dim
        self.embedder = self._backend.embedder

    def __getattr__(self, name: str):
        return getattr(self._backend, name)
