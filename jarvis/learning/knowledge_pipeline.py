"""Automatic knowledge update pipeline (Phase 3): incremental multi-source ingestion,
versioning, deduplication, corrupt/credential filtering, SQLite FTS5 indexing, optional local embeddings,
and rollback WITHOUT modifying model weights."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import quote, urlsplit

from ..config import DATA, InternetSpec, ROOT, LearningSpec
from ..memory_store import detect_secret
from ..netsec import NetworkSecurityError, safe_request
from ..search import SearchOfflineError, extract_readable_text

log = logging.getLogger("jarvis.learning.knowledge")

_ALLOWED_TEXT_EXT = {".md", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".py", ".js", ".ts", ".csv", ".html", ".htm"}


def detect_script_language(text: str) -> str:
    """Lightweight offline script detector for English, Malayalam, Hindi, or mixed text."""
    s = text or ""
    if re.search(r"[\u0d00-\u0d7f]", s):
        return "ml" if not re.search(r"[A-Za-z]{4,}", s) else "mixed"
    if re.search(r"[\u0900-\u097f]", s):
        return "hi"
    if re.search(r"[A-Za-z]", s):
        return "en"
    return "unknown"


def chunk_text(text: str, chunk_chars: int = 650, overlap: int = 80) -> list[str]:
    """Sentence-aware overlapping text chunker that avoids mid-word splits."""
    cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
    if not cleaned:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", cleaned) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        if len(para) > chunk_chars:
            sents = re.split(r"(?<=[.!?।])\s+", para)
            for s in sents:
                s = s.strip()
                if not s:
                    continue
                if cur and len(cur) + 1 + len(s) > chunk_chars:
                    chunks.append(cur)
                    tail = cur[-overlap:] if overlap > 0 and len(cur) > overlap else ""
                    cur = f"{tail} {s}".strip()
                else:
                    cur = f"{cur} {s}".strip() if cur else s
        else:
            if cur and len(cur) + 2 + len(para) > chunk_chars:
                chunks.append(cur)
                tail = cur[-overlap:] if overlap > 0 and len(cur) > overlap else ""
                cur = f"{tail}\n{para}".strip()
            else:
                cur = f"{cur}\n\n{para}".strip() if cur else para
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= 15]


@dataclass
class KnowledgeSourceSpec:
    id: str
    name: str
    kind: str  # "local_markdown" | "local_repo" | "user_doc" | "public_docs" | "huggingface_dataset" | "huggingface_model" | "configured_api"
    location: str
    topic: str = "general"
    license: str = "unknown"
    trust_classification: str = "user_provided"  # "official" | "verified_local" | "public_community" | "user_provided"
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class KnowledgePipeline:
    """Incremental versioned knowledge store & discovery/ingestion runner."""

    def __init__(self, spec: LearningSpec | None = None, data_dir: Path | None = None, *, is_online_fn=lambda: False):
        self.spec = spec or LearningSpec()
        self.base_dir = Path(data_dir) if data_dir else (DATA / "learning" / "knowledge")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "index.db"
        self.status_path = self.base_dir / "freshness.json"
        self.is_online_fn = is_online_fn
        self._lock = threading.RLock()
        self._sources: dict[str, KnowledgeSourceSpec] = {}
        self._freshness: dict = {
            "last_attempted_update": None,
            "last_successful_update": None,
            "sources_checked": 0,
            "new_documents_found": 0,
            "documents_indexed": 0,
            "failed_documents": 0,
            "indexing_duration_s": 0.0,
            "kb_chunks": 0,
            "kb_bytes": 0,
            "last_error": None,
        }
        self._init_db()
        self._load_configured_sources()
        if self.status_path.exists():
            try:
                self._freshness.update(json.loads(self.status_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        self._refresh_size_stats()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    location TEXT NOT NULL,
                    topic TEXT,
                    license TEXT,
                    trust_classification TEXT,
                    active_version INTEGER DEFAULT 0,
                    content_hash TEXT,
                    language TEXT,
                    retrieved_at REAL,
                    update_status TEXT
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    version_num INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    doc_version TEXT,
                    license TEXT,
                    language TEXT,
                    retrieved_at REAL NOT NULL,
                    byte_size INTEGER DEFAULT 0,
                    UNIQUE(source_id, version_num)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    version_num INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    title TEXT,
                    topic TEXT,
                    language TEXT,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    active INTEGER DEFAULT 1
                );
                """
            )

    def _load_configured_sources(self):
        for raw in self.spec.knowledge_sources:
            try:
                s = KnowledgeSourceSpec(**{k: raw[k] for k in KnowledgeSourceSpec.__dataclass_fields__ if k in raw})
                self._sources[s.id] = s
            except Exception as e:
                log.warning("Invalid knowledge source config skipped: %s", e)

    def register_source(self, src: KnowledgeSourceSpec) -> dict:
        with self._lock:
            self._sources[src.id] = src
            return src.to_dict()

    def list_sources(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM sources").fetchall()}
        out = []
        for sid, src in self._sources.items():
            d = src.to_dict()
            d["state"] = rows.get(sid)
            out.append(d)
        for sid, row in rows.items():
            if sid not in self._sources:
                out.append({
                    "id": sid,
                    "name": row["name"],
                    "kind": row["kind"],
                    "location": row["location"],
                    "topic": row.get("topic") or "general",
                    "license": row.get("license") or "unknown",
                    "trust_classification": row.get("trust_classification") or "user_provided",
                    "enabled": True,
                    "state": row,
                })
        return out

    def _save_freshness(self):
        with self._lock:
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._freshness, indent=2), encoding="utf-8")
            os.replace(tmp, self.status_path)

    def _refresh_size_stats(self):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(content)), 0) AS b FROM chunks WHERE active=1").fetchone()
            self._freshness["kb_chunks"] = int(row["n"])
            self._freshness["kb_bytes"] = int(row["b"])

    def freshness(self) -> dict:
        with self._lock:
            self._refresh_size_stats()
            return dict(self._freshness)

    def _check_online_allowed(self, url: str):
        if not self.is_online_fn():
            raise SearchOfflineError(f"Offline Local Mode blocks fetching remote knowledge source: {url}")
        host = (urlsplit(url).hostname or "").lower()
        allowed = {h.lower() for h in self.spec.allowed_knowledge_hosts}
        if allowed and host not in allowed:
            raise NetworkSecurityError(f"Knowledge host {host!r} is not in learning.allowed_knowledge_hosts")

    async def _fetch_remote_source(self, src: KnowledgeSourceSpec) -> tuple[str, str, str, str]:
        """Return `(text, doc_version, license, language)` from approved HTTPS sources."""
        net_spec = InternetSpec(enabled=True, max_fetch_bytes=self.spec.max_download_bytes, timeout_s=10.0)
        if src.kind == "huggingface_dataset":
            repo = src.location.strip("/")
            api_url = f"https://huggingface.co/api/datasets/{quote(repo, safe='/')}"
            readme_url = f"https://huggingface.co/datasets/{quote(repo, safe='/')}/raw/main/README.md"
            self._check_online_allowed(api_url)
            meta_res = await safe_request("GET", api_url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=2_000_000)
            meta = json.loads(meta_res.body.decode("utf-8", errors="replace"))
            sha = str(meta.get("sha") or meta.get("lastModified") or "latest")[:32]
            lic = "unknown"
            for tag in meta.get("tags") or []:
                if isinstance(tag, str) and tag.startswith("license:"):
                    lic = tag.split(":", 1)[1]
                    break
            body = meta.get("description") or ""
            try:
                rd = await safe_request("GET", readme_url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=2_000_000)
                body = rd.body.decode("utf-8", errors="replace")
            except Exception:
                pass
            txt = f"# Dataset: {repo}\nLicense: {lic}\nVersion: {sha}\n\n{body}".strip()
            return txt, sha, lic, detect_script_language(txt)

        if src.kind == "huggingface_model":
            repo = src.location.strip("/")
            api_url = f"https://huggingface.co/api/models/{quote(repo, safe='/')}"
            readme_url = f"https://huggingface.co/{quote(repo, safe='/')}/raw/main/README.md"
            self._check_online_allowed(api_url)
            meta_res = await safe_request("GET", api_url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=2_000_000)
            meta = json.loads(meta_res.body.decode("utf-8", errors="replace"))
            sha = str(meta.get("sha") or meta.get("lastModified") or "latest")[:32]
            lic = "unknown"
            for tag in meta.get("tags") or []:
                if isinstance(tag, str) and tag.startswith("license:"):
                    lic = tag.split(":", 1)[1]
                    break
            body = ""
            try:
                rd = await safe_request("GET", readme_url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=2_000_000)
                body = rd.body.decode("utf-8", errors="replace")
            except Exception:
                body = json.dumps(meta.get("cardData") or {}, ensure_ascii=False, indent=2)
            txt = f"# Model: {repo}\nLicense: {lic}\nVersion: {sha}\n\n{body}".strip()
            return txt, sha, lic, detect_script_language(txt)

        if src.kind in ("public_docs", "configured_api"):
            self._check_online_allowed(src.location)
            res = await safe_request("GET", src.location, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=min(self.spec.max_download_bytes, 2_000_000))
            ctype = res.headers.get("content-type", "").lower()
            if "html" in ctype or res.body.lstrip()[:15].lower().startswith(b"<!doctype html"):
                title, text = extract_readable_text(res.body, max_chars=25_000)
                combined = f"{title}\n\n{text}".strip()
            else:
                combined = res.body.decode("utf-8", errors="replace").strip()
            return combined, str(int(time.time())), src.license, detect_script_language(combined)

        raise ValueError(f"Unsupported remote knowledge source kind: {src.kind}")

    def _read_local_source(self, src: KnowledgeSourceSpec) -> tuple[str, str, str, str]:
        p = Path(src.location)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        else:
            p = p.resolve()
        if not p.exists():
            raise FileNotFoundError(f"Local knowledge path not found: {p}")

        parts: list[str] = []
        total = 0
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _ALLOWED_TEXT_EXT)
        for fp in files:
            if any(bad in fp.parts for bad in (".git", ".venv", "venv", "node_modules", "__pycache__", ".trash")):
                continue
            sz = fp.stat().st_size
            if total + sz > self.spec.max_download_bytes:
                break
            raw = fp.read_bytes()
            if b"\x00" in raw[:512]:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
            if text.count("\ufffd") > max(10, len(text) * 0.1):
                raise ValueError(f"Corrupt or malformed encoding detected in {fp.name}")
            if detect_secret(text):
                log.warning("Skipping file with sensitive secret pattern during knowledge indexing: %s", fp.name)
                continue
            total += sz
            parts.append(f"## {fp.name}\n{text.strip()}")

        combined = "\n\n".join(parts).strip()
        if not combined:
            raise ValueError(f"No readable non-empty documents found at {p}")
        return combined, f"mtime-{int(p.stat().st_mtime)}", src.license, detect_script_language(combined)

    def ingest_text(
        self,
        source_id: str,
        name: str,
        text: str,
        *,
        kind: str = "user_doc",
        location: str = "",
        topic: str = "general",
        license_name: str = "user-provided",
        doc_version: str = "1",
        trust: str = "verified_local",
        force: bool = False,
    ) -> dict:
        """Validate, deduplicate, chunk, and incrementally index raw text with version rollback history."""
        if not text or not text.strip():
            raise ValueError("Document content is empty")
        if len(text.encode("utf-8")) > self.spec.max_download_bytes:
            raise ValueError("Document exceeds max_download_bytes")
        if detect_secret(text):
            raise ValueError("Refused knowledge ingestion containing sensitive credential or private key")
        if text.count("\ufffd") > max(10, len(text) * 0.1):
            raise ValueError("Corrupt or unreadable replacement-character payload rejected")

        norm = text.strip()
        c_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        lang = detect_script_language(norm)
        now = round(time.time(), 3)

        with self._lock, self._connect() as conn:
            cur = conn.execute("SELECT active_version, content_hash FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if cur and cur["content_hash"] == c_hash and not force:
                conn.execute("UPDATE sources SET retrieved_at=?, update_status='unchanged' WHERE source_id=?", (now, source_id))
                return {"source_id": source_id, "status": "unchanged", "version": int(cur["active_version"]), "chunks": 0, "language": lang}

            # Global cross-source duplicate chunk check + incremental chunk write
            new_ver = (int(cur["active_version"]) + 1) if cur else 1
            chunks = chunk_text(norm)
            if not chunks:
                raise ValueError("No valid text chunks extracted from document")

            conn.execute("UPDATE chunks SET active=0 WHERE source_id=?", (source_id,))
            conn.execute(
                """
                INSERT INTO sources (source_id, name, kind, location, topic, license, trust_classification,
                                     active_version, content_hash, language, retrieved_at, update_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed')
                ON CONFLICT(source_id) DO UPDATE SET
                    name=excluded.name, kind=excluded.kind, location=excluded.location, topic=excluded.topic,
                    license=excluded.license, trust_classification=excluded.trust_classification,
                    active_version=excluded.active_version, content_hash=excluded.content_hash,
                    language=excluded.language, retrieved_at=excluded.retrieved_at, update_status='indexed'
                """,
                (source_id, name, kind, location, topic, license_name, trust, new_ver, c_hash, lang, now),
            )
            conn.execute(
                "INSERT INTO versions (source_id, version_num, content_hash, doc_version, license, language, retrieved_at, byte_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, new_ver, c_hash, doc_version, license_name, lang, now, len(norm.encode("utf-8"))),
            )
            for idx, c in enumerate(chunks):
                ch_hash = hashlib.sha256(c.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO chunks (source_id, version_num, chunk_index, title, topic, language, content, content_hash, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (source_id, new_ver, idx, name, topic, lang, c, ch_hash),
                )
            self._refresh_size_stats()
            return {"source_id": source_id, "status": "indexed", "version": new_ver, "chunks": len(chunks), "language": lang, "hash": c_hash[:12]}

    def rollback_source(self, source_id: str, target_version: int | None = None) -> dict:
        """Roll back an indexed knowledge source to its prior (or specified) snapshot cleanly."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT active_version FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row:
                raise KeyError(source_id)
            cur_ver = int(row["active_version"])
            if target_version is None:
                prev = conn.execute(
                    "SELECT version_num, content_hash, language FROM versions WHERE source_id=? AND version_num < ? ORDER BY version_num DESC LIMIT 1",
                    (source_id, cur_ver),
                ).fetchone()
            else:
                prev = conn.execute(
                    "SELECT version_num, content_hash, language FROM versions WHERE source_id=? AND version_num=?",
                    (source_id, target_version),
                ).fetchone()
            if not prev:
                raise ValueError(f"No prior version available to roll back for {source_id!r}")
            v_num = int(prev["version_num"])
            conn.execute("UPDATE chunks SET active=0 WHERE source_id=?", (source_id,))
            conn.execute("UPDATE chunks SET active=1 WHERE source_id=? AND version_num=?", (source_id, v_num))
            conn.execute(
                "UPDATE sources SET active_version=?, content_hash=?, language=?, update_status='rolled_back' WHERE source_id=?",
                (v_num, prev["content_hash"], prev["language"], source_id),
            )
            self._refresh_size_stats()
            return {"source_id": source_id, "rolled_back_from": cur_ver, "active_version": v_num}

    async def sync(self, topic_filter: str | None = None, source_id: str | None = None, force: bool = False) -> dict:
        """Run incremental update check + ingestion across registered sources."""
        t0 = time.monotonic()
        now = round(time.time(), 3)
        checked = new_docs = indexed = failed = 0
        details = []
        last_err = None

        with self._lock:
            targets = list(self._sources.values())
        if source_id:
            targets = [s for s in targets if s.id == source_id]
        elif topic_filter:
            tf = topic_filter.strip().lower()
            targets = [s for s in targets if tf in s.topic.lower() or tf in s.name.lower()]

        for src in targets:
            if not src.enabled:
                continue
            checked += 1
            try:
                if src.kind in ("local_markdown", "local_repo", "user_doc"):
                    text, ver, lic, _ = self._read_local_source(src)
                else:
                    text, ver, lic, _ = await self._fetch_remote_source(src)
                res = self.ingest_text(
                    src.id,
                    src.name,
                    text,
                    kind=src.kind,
                    location=src.location,
                    topic=src.topic,
                    license_name=lic,
                    doc_version=ver,
                    trust=src.trust_classification,
                    force=force,
                )
                details.append(res)
                if res["status"] == "indexed":
                    new_docs += 1
                    indexed += res["chunks"]
            except Exception as e:
                failed += 1
                last_err = str(e)
                details.append({"source_id": src.id, "status": "failed", "error": str(e)})
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE sources SET retrieved_at=?, update_status='failed' WHERE source_id=?",
                        (now, src.id),
                    )

        dur = round(time.monotonic() - t0, 3)
        with self._lock:
            self._freshness["last_attempted_update"] = now
            if failed == 0 or new_docs > 0:
                self._freshness["last_successful_update"] = now
            self._freshness["sources_checked"] = checked
            self._freshness["new_documents_found"] = new_docs
            self._freshness["documents_indexed"] = indexed
            self._freshness["failed_documents"] = failed
            self._freshness["indexing_duration_s"] = dur
            self._freshness["last_error"] = last_err
            self._refresh_size_stats()
            self._save_freshness()
        return {**self._freshness, "details": details}

    def search(self, query: str, k: int = 4) -> list[dict]:
        """Fast lexical search over active indexed knowledge chunks with source/trust provenance."""
        q_toks = set(re.findall(r"[a-z0-9]+|[\u0d00-\u0d7f]+|[\u0900-\u097f]+", (query or "").lower()))
        q_toks = {t for t in q_toks if len(t) > 1}
        if not q_toks:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.source_id, c.version_num, c.title, c.topic, c.language, c.content,
                       s.kind, s.location, s.trust_classification
                FROM chunks c
                JOIN sources s ON c.source_id = s.source_id
                WHERE c.active = 1
                """
            ).fetchall()
        scored = []
        for r in rows:
            txt = r["content"]
            title_low = (r["title"] or "").lower()
            topic_low = (r["topic"] or "").lower()
            d_toks = set(re.findall(r"[a-z0-9]+|[\u0d00-\u0d7f]+|[\u0900-\u097f]+", txt.lower()))
            overlap = len(q_toks & d_toks)
            if not overlap:
                continue
            bonus = sum(1.5 for t in q_toks if t in title_low or t in topic_low)
            score = overlap + bonus
            scored.append((score, {
                "source_id": r["source_id"],
                "title": r["title"],
                "topic": r["topic"],
                "kind": r["kind"],
                "location": r["location"],
                "trust": r["trust_classification"],
                "version": r["version_num"],
                "language": r["language"],
                "content": txt,
                "score": round(score, 2),
            }))
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[: max(1, k)]]
