"""Async & sync database engine manager supporting SQLite+WAL locally and PostgreSQL 18 + pgvector."""
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import DATA
from .models import Base

log = logging.getLogger("jarvis.db")


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


class DatabaseManager:
    """Lifecycle owner for SQLAlchemy async + sync engines, migrations, and health status."""

    def __init__(self, configured_url: str = "", data_dir: Path | None = None, echo: bool = False):
        self.async_url, self.sync_url = resolve_database_urls(configured_url, data_dir=data_dir)
        self.is_postgres = self.sync_url.startswith("postgresql")
        self.pgvector_available = False

        self.sync_engine = create_engine(self.sync_url, echo=echo, future=True)
        if not self.is_postgres:
            _attach_sqlite_pragmas(self.sync_engine)
        self.async_engine: AsyncEngine = create_async_engine(self.async_url, echo=echo, future=True)
        if not self.is_postgres:
            _attach_sqlite_pragmas(self.async_engine.sync_engine)

        self.async_session = async_sessionmaker(self.async_engine, class_=AsyncSession, expire_on_commit=False)
        self.sync_session = sessionmaker(self.sync_engine, class_=Session, expire_on_commit=False)

    def init_schema_sync(self) -> None:
        """Create schema tables idempotently (works in tests/dev; Alembic runs for versioned upgrades)."""
        if self.is_postgres:
            try:
                with self.sync_engine.begin() as conn:
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    self.pgvector_available = True
            except Exception as e:
                log.warning("PostgreSQL vector extension skipped/unavailable: %s", e)
        Base.metadata.create_all(self.sync_engine)

    async def init_schema(self) -> None:
        async with self.async_engine.begin() as conn:
            if self.is_postgres:
                try:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    self.pgvector_available = True
                except Exception as e:
                    log.warning("PostgreSQL vector extension skipped/unavailable: %s", e)
            await conn.run_sync(Base.metadata.create_all)

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
            }
        except Exception as e:
            return {"up": False, "dialect": "postgresql" if self.is_postgres else "sqlite", "error": str(e)}

    async def close(self) -> None:
        await self.async_engine.dispose()
        self.sync_engine.dispose()

