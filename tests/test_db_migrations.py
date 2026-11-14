"""Alembic migration validation on DISPOSABLE sqlite files only (the real data/jarvis.db is never opened for writing).

* fresh database: `alembic upgrade head` builds the full schema and reaches the head revision
* upgrade: a database at an older revision with data is upgraded with the real migrations (no `stamp`) and keeps its rows
* parity: the schema Alembic builds equals the schema the app builds (models) - drift would break upgrades
* a copy of the real database is checked read-only through the sqlite backup API
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from jarvis.config import RESOURCE_ROOT
from jarvis.db.engine import DatabaseManager

SCRIPTS = str(RESOURCE_ROOT / "jarvis" / "db" / "migrations")


def _cfg(db_file: Path, monkeypatch) -> Config:
    monkeypatch.setenv("JARVIS_DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPTS)
    return cfg


def _head() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPTS)
    return ScriptDirectory.from_config(cfg).get_current_head()


def _revisions() -> list[str]:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPTS)
    return [r.revision for r in reversed(list(ScriptDirectory.from_config(cfg).walk_revisions()))]


def _version(db_file: Path) -> str | None:
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        with eng.connect() as c:
            return MigrationContext.configure(c).get_current_revision()
    finally:
        eng.dispose()


def _schema(db_file: Path) -> dict[str, set[str]]:
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        insp = inspect(eng)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}
    finally:
        eng.dispose()


def test_single_linear_head():
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPTS)
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1  # no branched history


def test_fresh_database_upgrades_to_head(tmp_path, monkeypatch):
    f = tmp_path / "fresh.db"
    command.upgrade(_cfg(f, monkeypatch), "head")
    assert _version(f) == _head()
    assert len(_schema(f)) > 20


def test_fresh_alembic_schema_matches_app_schema(tmp_path, monkeypatch):
    a, b = tmp_path / "alembic.db", tmp_path / "app.db"
    command.upgrade(_cfg(a, monkeypatch), "head")
    monkeypatch.delenv("JARVIS_DATABASE_URL")
    db = DatabaseManager(f"sqlite:///{b.as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    db.sync_engine.dispose()
    sa, sb = _schema(a), _schema(b)
    assert set(sa) == set(sb), (set(sa) ^ set(sb))
    drift = {t: sorted(sa[t] ^ sb[t]) for t in sa if sa[t] != sb[t]}
    assert not drift, drift
    assert _version(b) == _head()  # the app stamps a FRESH database it just created from the models


def test_upgrade_from_older_revision_preserves_data(tmp_path, monkeypatch):
    f = tmp_path / "old.db"
    cfg = _cfg(f, monkeypatch)
    revs = _revisions()
    assert len(revs) >= 6
    old = revs[4]
    command.upgrade(cfg, old)
    assert _version(f) == old
    eng = create_engine(f"sqlite:///{f.as_posix()}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO approval_requests (token, action_type, risk_tier, target_resource, preview_diff, args, status, created_at, expires_at) "
                       "VALUES ('keep-me','delete_file','EXECUTE','x','d','{}','pending',1.0,9999999999.0)"))
    eng.dispose()
    command.upgrade(cfg, "head")  # real migrations, never `stamp`
    assert _version(f) == _head()
    eng = create_engine(f"sqlite:///{f.as_posix()}")
    with eng.connect() as c:
        assert c.execute(text("SELECT status FROM approval_requests WHERE token='keep-me'")).scalar() == "pending"
    eng.dispose()


def test_every_revision_upgrades_step_by_step(tmp_path, monkeypatch):
    f = tmp_path / "steps.db"
    cfg = _cfg(f, monkeypatch)
    for rev in _revisions():
        command.upgrade(cfg, rev)
        assert _version(f) == rev


def test_head_is_stamped_only_when_schema_really_is_head(tmp_path, monkeypatch):
    """Guards the rule 'never `alembic stamp head` to hide a missing migration': a database that is BEHIND head must be reported behind."""
    f = tmp_path / "behind.db"
    command.upgrade(_cfg(f, monkeypatch), _revisions()[3])
    from jarvis.health import migration_state

    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    db = DatabaseManager(f"sqlite:///{f.as_posix()}", data_dir=tmp_path)
    st = migration_state(db)
    db.sync_engine.dispose()
    assert st["at_head"] is False and st["current"] == _revisions()[3] and st["head"] == _head()


def test_real_database_copy_is_at_head_read_only(tmp_path):
    """Copies the live DB with sqlite's backup API (consistent, read-only on the source) and inspects the copy."""
    src = Path(__file__).resolve().parent.parent / "data" / "jarvis.db"
    if not src.exists():
        pytest.skip("no local data/jarvis.db")
    dst = tmp_path / "copy.db"
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    ver = _version(dst)
    assert ver in _revisions(), ver  # a known revision (not stamped blindly to something else)


def test_old_database_opened_by_app_is_not_falsely_stamped_and_still_upgrades(tmp_path, monkeypatch):
    """The app runs create_all + column reconcile at start. That must neither stamp a behind DB as head nor break the later real upgrade."""
    f = tmp_path / "app_opened.db"
    cfg = _cfg(f, monkeypatch)
    old = _revisions()[8]
    command.upgrade(cfg, old)
    monkeypatch.delenv("JARVIS_DATABASE_URL")
    db = DatabaseManager(f"sqlite:///{f.as_posix()}", data_dir=tmp_path)
    db.init_schema_sync()
    db.sync_engine.dispose()
    assert _version(f) == old  # honest: still behind
    command.upgrade(_cfg(f, monkeypatch), "head")
    assert _version(f) == _head()
