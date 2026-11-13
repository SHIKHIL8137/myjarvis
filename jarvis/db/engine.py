"""Async & sync database engine manager supporting SQLite+WAL locally and PostgreSQL 18 + pgvector."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import DATA
from .models import Base

log = logging.getLogger("jarvis.db")

_P10_COLUMN_DDLS: dict[str, list[tuple[str, str]]] = {
    "personal_memories": [
        ("namespace", "VARCHAR(48) NOT NULL DEFAULT 'personal_memory'"),
        ("owner_scope", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
        ("project_id", "VARCHAR(36)"),
        ("meta", "TEXT NOT NULL DEFAULT '{}'"),
        ("privacy_level", "VARCHAR(24) NOT NULL DEFAULT 'private'"),
        ("embedding_model", "VARCHAR(120) NOT NULL DEFAULT 'hash-v1:256'"),
        ("archived", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("deleted_at", "FLOAT"),
        ("superseded_by", "VARCHAR(32)"),
        ("supersedes_id", "VARCHAR(32)"),
    ],
    "projects": [
        ("privacy_level", "VARCHAR(24) NOT NULL DEFAULT 'private'"),
    ],
    "vector_entries": [
        ("memory_type", "VARCHAR(32) NOT NULL DEFAULT 'fact'"),
        ("source", "VARCHAR(64) NOT NULL DEFAULT 'system'"),
        ("confidence", "FLOAT NOT NULL DEFAULT 1.0"),
        ("privacy_level", "VARCHAR(24) NOT NULL DEFAULT 'private'"),
        ("embedding_model", "VARCHAR(120) NOT NULL DEFAULT 'hash-v1:256'"),
        ("expires_at", "FLOAT"),
        ("created_at", "FLOAT NOT NULL DEFAULT 0.0"),
    ],
    "assistant_tasks": [
        ("worker_kind", "VARCHAR(48) NOT NULL DEFAULT 'assistant'"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
        ("not_before", "FLOAT"),
        ("next_retry_at", "FLOAT"),
        ("deadline_at", "FLOAT"),
        ("step_budget", "INTEGER NOT NULL DEFAULT 20"),
        ("steps_used", "INTEGER NOT NULL DEFAULT 0"),
        ("token_budget", "INTEGER NOT NULL DEFAULT 120000"),
        ("tokens_used", "INTEGER NOT NULL DEFAULT 0"),
        ("tool_budget", "INTEGER NOT NULL DEFAULT 40"),
        ("tools_used_count", "INTEGER NOT NULL DEFAULT 0"),
        ("quota_reason", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("payload", "TEXT NOT NULL DEFAULT '{}'"),
        ("checkpoint_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("steer_notes", "TEXT NOT NULL DEFAULT '[]'"),
        ("completed_step_keys", "TEXT NOT NULL DEFAULT '[]'"),
        ("claimed_by", "VARCHAR(64)"),
        ("claimed_at", "FLOAT"),
        ("scheduled_for", "FLOAT"),
        ("pause_reason", "VARCHAR(240) NOT NULL DEFAULT ''"),
        ("partial_report", "TEXT NOT NULL DEFAULT '{}'"),
        ("correlation_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("parent_task_id", "VARCHAR(36)"),
    ],
    "voice_transcripts": [
        ("source_language", "VARCHAR(24) NOT NULL DEFAULT 'unknown'"),
        ("language_probability", "FLOAT NOT NULL DEFAULT 0.0"),
        ("recognition_quality", "VARCHAR(24) NOT NULL DEFAULT 'medium'"),
        ("meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    ],
}


def resolve_database_urls(configured_url: str = "", data_dir: Path | None = None) -> tuple[str, str]:
    """Return (async_url, sync_url) cleanly for either PostgreSQL or local SQLite."""
    raw = (os.environ.get("JARVIS_DATABASE_URL") or configured_url or "").strip()
    if not raw:
        db_file = (Path(data_dir) if data_dir else DATA) / "jarvis.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        norm = db_file.resolve().as_posix()
        return f"sqlite+aiosqlite:///{norm}", f"sqlite:///{norm}"
    if raw.startswith("sqlite+aiosqlite://"):
        return raw, raw.replace("sqlite+aiosqlite://", "sqlite://", 1)
    if raw.startswith("sqlite://"):
        return raw.replace("sqlite://", "sqlite+aiosqlite://", 1), raw
    if raw.startswith("postgresql://"):
        sync = raw.replace("postgresql://", "postgresql+psycopg://", 1)
        return sync, sync
    return raw, raw


def _attach_sqlite_pragmas(engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cur.close()


def _ensure_alembic_version_sync(conn) -> None:
    """Ensure alembic_version table is stamped with current schema head if missing."""
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if "alembic_version" not in tables:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(text("INSERT OR IGNORE INTO alembic_version (version_num) VALUES ('0011_p5_undo_journal')"))


def _reconcile_columns_sync(conn) -> None:
    """Safely add newly introduced P10 columns to existing databases created prior to Alembic upgrade."""
    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    is_pg = conn.dialect.name == "postgresql"
    for tbl, cols in _P10_COLUMN_DDLS.items():
        if tbl not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(tbl)}
        for cname, ddl in cols:
            if cname in have:
                continue
            pg_ddl = ddl
            if is_pg:
                if cname in ("meta", "payload", "checkpoint_json"):
                    pg_ddl = ddl.replace("TEXT NOT NULL DEFAULT '{}'", "JSONB NOT NULL DEFAULT '{}'::jsonb")
                elif cname in ("steer_notes", "completed_step_keys"):
                    pg_ddl = ddl.replace("TEXT NOT NULL DEFAULT '[]'", "JSONB NOT NULL DEFAULT '[]'::jsonb")
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {cname} {pg_ddl}"))


class DatabaseManager:
    """Lifecycle owner for SQLAlchemy async + sync engines, migrations, and health status."""

    def __init__(self, configured_url: str = "", data_dir: Path | None = None, echo: bool = False):
        self.data_dir = Path(data_dir) if data_dir else DATA
        self.async_url, self.sync_url = resolve_database_urls(configured_url, data_dir=self.data_dir)
        self.is_postgres = self.sync_url.startswith("postgresql")
        self.pgvector_available = False
        self.pgvector_version = ""

        self.sync_engine = create_engine(self.sync_url, echo=echo, future=True)
        if not self.is_postgres:
            _attach_sqlite_pragmas(self.sync_engine)
        self.async_engine: AsyncEngine = create_async_engine(self.async_url, echo=echo, future=True)
        if not self.is_postgres:
            _attach_sqlite_pragmas(self.async_engine.sync_engine)

        self.async_session = async_sessionmaker(self.async_engine, class_=AsyncSession, expire_on_commit=False)
        self.sync_session = sessionmaker(self.sync_engine, class_=Session, expire_on_commit=False)

    def _probe_pgvector_sync(self, conn) -> None:
        if not self.is_postgres:
            return
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            ver = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
            self.pgvector_available = True
            self.pgvector_version = str(ver or "")
        except Exception as e:
            self.pgvector_available = False
            log.warning("PostgreSQL vector extension skipped/unavailable: %s", e)

    def init_schema_sync(self) -> None:
        """Create schema tables and reconcile newly added columns idempotently."""
        with self.sync_engine.begin() as conn:
            self._probe_pgvector_sync(conn)
            Base.metadata.create_all(conn)
            _reconcile_columns_sync(conn)
            _ensure_alembic_version_sync(conn)

    async def init_schema(self) -> None:
        async with self.async_engine.begin() as conn:
            await conn.run_sync(self._probe_pgvector_sync)
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_reconcile_columns_sync)
            await conn.run_sync(_ensure_alembic_version_sync)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.async_session() as s:
            yield s

    async def health(self) -> dict:
        try:
            async with self.async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {
                "up": True,
                "dialect": "postgresql" if self.is_postgres else "sqlite",
                "pgvector": self.pgvector_available,
                "pgvector_version": self.pgvector_version or None,
            }
        except Exception as e:
            return {"up": False, "dialect": "postgresql" if self.is_postgres else "sqlite", "error": str(e)}

    async def close(self) -> None:
        await self.async_engine.dispose()
        self.sync_engine.dispose()
