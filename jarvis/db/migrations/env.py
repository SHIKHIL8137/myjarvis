"""Alembic migration environment configured for Jarvis SQLAlchemy models."""
from logging.config import fileConfig
import os
from alembic import context
from sqlalchemy import engine_from_config, pool

from jarvis.db.engine import resolve_database_urls
from jarvis.db.models import Base

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def _url() -> str:
    env_url = os.environ.get("JARVIS_DATABASE_URL", "")
    if env_url:
        _, sync_url = resolve_database_urls(env_url)
        return sync_url
    return config.get_main_option("sqlalchemy.url") or resolve_database_urls("")[1]


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

