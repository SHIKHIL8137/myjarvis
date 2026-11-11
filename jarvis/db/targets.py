"""Centralized Database Target Identity & Production Restore Protection Registry (never stores plaintext passwords)."""
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.engine import make_url

VALID_DB_ROLES = {"local_sqlite", "dev_pg", "test_pg", "scratch_pg", "prod_pg"}


def redact_db_url(raw_url: str) -> str:
    """Return connection URL with any embedded password replaced cleanly by ***REDACTED***."""
    txt = (raw_url or "").strip()
    if not txt or txt.startswith("sqlite"):
        return txt
    try:
        parts = urlsplit(txt)
        if parts.password:
            user = parts.username or ""
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            auth = f"{user}:***REDACTED***@" if user else "***REDACTED***@"
            netloc = f"{auth}{host}{port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return txt


@dataclass(frozen=True)
class DatabaseIdentity:
    driver: str  # "sqlite" | "postgresql"
    host: str
    port: int
    database: str
    fingerprint: str

    def to_dict(self) -> dict:
        return {
            "driver": self.driver,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "fingerprint": self.fingerprint,
        }


def normalize_database_identity(raw_url: str, data_dir: Path | None = None) -> DatabaseIdentity:
    """Normalize SQLite or PostgreSQL URLs into a password-free canonical identity."""
    clean = (raw_url or "").strip()
    if not clean:
        db_file = (Path(data_dir) / "jarvis.db").resolve() if data_dir else Path("data/jarvis.db").resolve()
        db_norm = db_file.as_posix().lower()
        return DatabaseIdentity(
            driver="sqlite",
            host="local",
            port=0,
            database=db_norm,
            fingerprint=f"sqlite://local:0/{db_norm}",
        )

    u = make_url(clean)
    drv = (u.drivername or "").split("+", 1)[0].lower()
    if drv == "postgres":
        drv = "postgresql"
    if drv == "sqlite":
        db_raw = u.database or ":memory:"
        if db_raw != ":memory:":
            db_norm = Path(db_raw).resolve().as_posix().lower()
        else:
            db_norm = ":memory:"
        return DatabaseIdentity(
            driver="sqlite",
            host="local",
            port=0,
            database=db_norm,
            fingerprint=f"sqlite://local:0/{db_norm}",
        )

    raw_host = (u.host or "localhost").strip().lower()
    host = "localhost" if raw_host in ("127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "") else raw_host
    port = int(u.port or 5432)
    database = (u.database or "postgres").strip().lower()
    return DatabaseIdentity(
        driver=drv,
        host=host,
        port=port,
        database=database,
        fingerprint=f"{drv}://{host}:{port}/{database}",
    )


@dataclass
class DatabaseTarget:
    target_id: str
    role: str
    identity: DatabaseIdentity
    source: str = "config"
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "role": self.role,
            "source": self.source,
            "label": self.label or f"{self.role}:{self.identity.database}",
            **self.identity.to_dict(),
        }


class DatabaseTargetRegistry:
    """Tracks normalized DB target roles and prevents destructive restores onto live/prod/unknown targets."""

    def __init__(
        self,
        settings=None,
        *,
        active_sync_url: str = "",
        data_dir: Path | None = None,
        include_env: bool = True,
    ):
        self.data_dir = data_dir
        self._targets: dict[str, list[DatabaseTarget]] = {}
        self.active_identity: DatabaseIdentity | None = None
        self.refresh(settings=settings, active_sync_url=active_sync_url, include_env=include_env)

    def refresh(self, *, settings=None, active_sync_url: str = "", include_env: bool = True) -> None:
        self._targets.clear()
        db_spec = getattr(settings, "database", None) if settings is not None else None

        if db_spec is not None:
            for item in getattr(db_spec, "production_targets", []) or []:
                if str(item).strip():
                    self.register("prod_pg", str(item), source="config.production_targets")
            if getattr(db_spec, "scratch_target_url", ""):
                self.register("scratch_pg", str(db_spec.scratch_target_url), source="config.scratch_target_url")
            if getattr(db_spec, "test_target_url", ""):
                self.register("test_pg", str(db_spec.test_target_url), source="config.test_target_url")
            for entry in getattr(db_spec, "registered_targets", []) or []:
                if isinstance(entry, dict) and entry.get("url") and entry.get("role"):
                    self.register(
                        str(entry["role"]),
                        str(entry["url"]),
                        source="config.registered_targets",
                        label=str(entry.get("label") or ""),
                    )

        if include_env:
            env_map = {
                "JARVIS_PROD_DATABASE_URL": "prod_pg",
                "JARVIS_PRODUCTION_DB_URL": "prod_pg",
                "JARVIS_DEV_DATABASE_URL": "dev_pg",
                "JARVIS_PG_TEST_URL": "test_pg",
                "JARVIS_TEST_DATABASE_URL": "test_pg",
                "JARVIS_PG_SCRATCH_URL": "scratch_pg",
                "JARVIS_SCRATCH_DATABASE_URL": "scratch_pg",
            }
            for env_key, role in env_map.items():
                val = os.environ.get(env_key, "").strip()
                if val:
                    self.register(role, val, source=f"env:{env_key}", label=env_key)

        eff_active = active_sync_url or (getattr(db_spec, "url", "") if db_spec else "")
        self.set_active_db(eff_active, configured_role=str(getattr(db_spec, "role", "auto") or "auto"))

    def set_active_db(self, sync_url: str, *, configured_role: str = "auto") -> None:
        ident = normalize_database_identity(sync_url, data_dir=self.data_dir)
        self.active_identity = ident
        if configured_role in VALID_DB_ROLES:
            role = configured_role
        elif ident.driver == "sqlite":
            role = "local_sqlite"
        else:
            existing_roles = {t.role for t in self._targets.get(ident.fingerprint, [])}
            role = next(iter(existing_roles)) if len(existing_roles) == 1 else "prod_pg"
        # Ensure active live DB is always registered once with its role
        cur = self._targets.setdefault(ident.fingerprint, [])
        if not any(t.source == "active_db" for t in cur):
            cur.append(DatabaseTarget(target_id=f"active-{ident.driver}", role=role, identity=ident, source="active_db", label="Active Jarvis DB"))

    def register(self, role: str, url: str, *, source: str = "manual", label: str = "") -> DatabaseTarget:
        r = (role or "").strip().lower()
        if r not in VALID_DB_ROLES:
            raise ValueError(f"Unsupported database role {role!r}; must be one of {sorted(VALID_DB_ROLES)}")
        ident = normalize_database_identity(url, data_dir=self.data_dir)
        if ident.driver == "sqlite" and r != "local_sqlite":
            raise ValueError(f"SQLite target {ident.fingerprint} cannot be registered as PostgreSQL role {r!r}")
        if ident.driver != "sqlite" and r == "local_sqlite":
            raise ValueError("Non-SQLite target cannot use role 'local_sqlite'")
        tid = f"{r}:{ident.host}:{ident.port}/{ident.database}"
        entries = self._targets.setdefault(ident.fingerprint, [])
        for existing in entries:
            if existing.role == r and existing.source == source:
                return existing
        rec = DatabaseTarget(target_id=tid, role=r, identity=ident, source=source, label=label)
        entries.append(rec)
        return rec

    def classify_target(self, url: str) -> dict:
        ident = normalize_database_identity(url, data_dir=self.data_dir)
        entries = list(self._targets.get(ident.fingerprint, []))
        roles = sorted({e.role for e in entries})
        active_fp = self.active_identity.fingerprint if self.active_identity else ""
        is_current_live = bool(active_fp and ident.fingerprint == active_fp)
        is_ambiguous = len(roles) > 1
        is_unknown = len(roles) == 0
        is_production = ("prod_pg" in roles) or (is_current_live and ident.driver == "postgresql")
        is_scratch = (roles == ["scratch_pg"]) and (not is_current_live) and (not is_ambiguous)
        if is_ambiguous:
            primary_role = "ambiguous"
        elif roles:
            primary_role = roles[0]
        else:
            primary_role = "unknown"
        return {
            **ident.to_dict(),
            "roles": roles,
            "primary_role": primary_role,
            "is_current_live": is_current_live,
            "is_production": is_production,
            "is_scratch": is_scratch,
            "is_unknown": is_unknown,
            "is_ambiguous": is_ambiguous,
            "sources": sorted({e.source for e in entries}),
        }

    def list_targets(self) -> list[dict]:
        out: list[dict] = []
        active_fp = self.active_identity.fingerprint if self.active_identity else ""
        for fp, entries in sorted(self._targets.items()):
            roles = sorted({e.role for e in entries})
            rep = entries[0]
            out.append(
                {
                    **rep.to_dict(),
                    "roles": roles,
                    "ambiguous": len(roles) > 1,
                    "is_current_live": fp == active_fp,
                }
            )
        return out

