"""Hybrid Unicode lexical + dense vector retriever with strict 5-way namespace & project isolation."""
from dataclasses import dataclass
import hashlib
import math
import time
import uuid

import numpy as np
from sqlalchemy import delete, select

from ..db.engine import DatabaseManager
from ..db.models import VectorEntryRow
from ..retrieval import tokens

MEMORY_NAMESPACES = (
    "personal_memory",
    "project_knowledge",
    "experience_memory",
    "external_knowledge",
    "personal_documents",
)


def hash_embed(text: str, dim: int = 256) -> np.ndarray:
    """Deterministic local multilingual embedding using word + character trigram feature hashing.
    Supports English, Malayalam, Hindi, and code symbols with zero GPU VRAM cost."""
    vec = np.zeros(dim, dtype=np.float32)
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return vec
    toks = tokens(cleaned)
    for tok in toks:
        idx = int.from_bytes(hashlib.blake2b(f"w:{tok}".encode("utf-8"), digest_size=4).digest(), "little") % dim
        vec[idx] += 2.0
    # Character 3-grams preserve Malayalam/Indic morphology and code identifiers
    for i in range(max(0, len(cleaned) - 2)):
        tri = cleaned[i : i + 3]
        if tri.strip():
            idx = int.from_bytes(hashlib.blake2b(f"c:{tri}".encode("utf-8"), digest_size=4).digest(), "little") % dim
            vec[idx] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec


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


class IsolatedVectorStore:
    """SQL-backed isolated hybrid vector store guaranteeing strict namespace & project scoping."""

    def __init__(self, db: DatabaseManager, dim: int = 256):
        self.db = db
        self.dim = dim

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
    ) -> str:
        if namespace not in MEMORY_NAMESPACES:
            raise ValueError(f"Unsupported vector namespace: {namespace!r}")
        if namespace == "project_knowledge" and not project_id:
            raise ValueError("project_knowledge vectors MUST include project_id for strict isolation")

        emb = hash_embed(f"{title}\n{content}".strip(), dim=self.dim)
        blob = emb.astype("<f4").tobytes()
        entry_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{project_id or ''}:{doc_id}:{chunk_index}").hex[:32]

        with self.db.sync_session() as s:
            existing = s.get(VectorEntryRow, entry_id)
            now = round(time.time(), 3)
            if existing:
                existing.title = title[:250]
                existing.content = content
                existing.meta = meta or {}
                existing.embedding_blob = blob
                existing.updated_at = now
            else:
                s.add(
                    VectorEntryRow(
                        id=entry_id,
                        namespace=namespace,
                        owner_id=owner_id,
                        project_id=project_id,
                        doc_id=doc_id,
                        chunk_index=chunk_index,
                        title=title[:250],
                        content=content,
                        meta=meta or {},
                        dim=self.dim,
                        embedding_blob=blob,
                        updated_at=now,
                    )
                )
            s.commit()
        return entry_id

    def delete_doc(self, namespace: str, doc_id: str, project_id: str | None = None) -> int:
        with self.db.sync_session() as s:
            stmt = delete(VectorEntryRow).where(VectorEntryRow.namespace == namespace, VectorEntryRow.doc_id == doc_id)
            if project_id is not None:
                stmt = stmt.where(VectorEntryRow.project_id == project_id)
            res = s.execute(stmt)
            s.commit()
            return int(getattr(res, "rowcount", 0) or 0)

    def search(
        self,
        *,
        namespace: str,
        query: str,
        k: int = 5,
        project_id: str | None = None,
        meta_filter: dict | None = None,
        min_score: float = 0.05,
    ) -> list[VectorHit]:
        if namespace not in MEMORY_NAMESPACES:
            raise ValueError(f"Unsupported vector namespace: {namespace!r}")
        if namespace == "project_knowledge" and not project_id:
            raise ValueError("project_knowledge queries MUST specify project_id")

        q_vec = hash_embed(query, dim=self.dim)
        q_toks = set(tokens(query))
        with self.db.sync_session() as s:
            stmt = select(VectorEntryRow).where(VectorEntryRow.namespace == namespace)
            if project_id is not None:
                stmt = stmt.where(VectorEntryRow.project_id == project_id)
            rows = s.scalars(stmt).all()

        scored: list[VectorHit] = []
        for row in rows:
            if meta_filter and any(row.meta.get(mk) != mv for mk, mv in meta_filter.items()):
                continue
            r_vec = np.frombuffer(row.embedding_blob, dtype="<f4")
            cos = float(np.dot(q_vec, r_vec)) if len(r_vec) == len(q_vec) else 0.0
            doc_toks = set(tokens(f"{row.title} {row.content}"))
            lex = (len(q_toks & doc_toks) / math.sqrt(max(1, len(q_toks) * len(doc_toks)))) if (q_toks and doc_toks) else 0.0
            hybrid = 0.65 * max(0.0, cos) + 0.35 * lex
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
                    )
                )
        scored.sort(key=lambda h: -h.score)
        return scored[:k]
