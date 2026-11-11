"""3-2-1 Complete Disaster Recovery Engine: SQLite + PostgreSQL pg_dump/pg_restore, vectors, repos,
AES-256-GCM authenticated encryption, pluggable destinations, retention rotation, and isolated restore drills."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from .. import audit
from ..config import Settings
from ..agents.approvals import ApprovalService
from ..db.engine import DatabaseManager
from ..db.models import (
    AgentRunRow,
    AuditEventRow,
    BackupManifestRow,
    ExperienceLessonRow,
    PersonalMemoryRow,
    ProjectRow,
    ReminderRow,
    VectorEntryRow,
)
from ..db.targets import DatabaseTargetRegistry, redact_db_url
from ..memory.five_way import FiveWayMemoryService
from ..security.secrets import export_encrypted_vault, import_encrypted_vault, load_secrets_map, save_secrets_map

_ARCHIVE_MAGIC = b"JDRVAULT1"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rmtree_readonly_safe(path: Path) -> None:
    def _onexc(func, p, exc_info):
        os.chmod(p, 0o700)
        func(p)

    shutil.rmtree(path, onexc=_onexc)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _derive_key(passphrase: str, salt: bytes, label: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", (passphrase.encode("utf-8") + label), salt, 200_000, dklen=32)


def encrypt_archive_bytes(raw: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt, b"jdr-archive")
    ct = AESGCM(key).encrypt(nonce, raw, _ARCHIVE_MAGIC)
    return _ARCHIVE_MAGIC + salt + nonce + ct


def decrypt_archive_bytes(payload: bytes, passphrase: str) -> bytes:
    if not payload.startswith(_ARCHIVE_MAGIC) or len(payload) < len(_ARCHIVE_MAGIC) + 28:
        raise ValueError("Invalid encrypted backup archive header")
    off = len(_ARCHIVE_MAGIC)
    salt = payload[off : off + 16]
    nonce = payload[off + 16 : off + 28]
    ct = payload[off + 28 :]
    key = _derive_key(passphrase, salt, b"jdr-archive")
    return AESGCM(key).decrypt(nonce, ct, _ARCHIVE_MAGIC)


def _find_pg_tool(tool_name: str) -> str | None:
    found = shutil.which(tool_name) or shutil.which(f"{tool_name}.exe")
    if found:
        return found
    pg_base = Path(r"C:\Program Files\PostgreSQL")
    if pg_base.exists():
        for ver_dir in sorted(pg_base.iterdir(), reverse=True):
            cand = ver_dir / "bin" / (f"{tool_name}.exe" if os.name == "nt" else tool_name)
            if cand.exists():
                return str(cand)
    return None


def _pg_cli_url(sa_url: str) -> str:
    return sa_url.replace("postgresql+psycopg://", "postgresql://", 1).replace("postgresql+asyncpg://", "postgresql://", 1)


def _pg_conn_spec(sa_url: str) -> dict[str, str]:
    u = make_url(_pg_cli_url(sa_url))
    return {
        "host": str(u.host or "localhost"),
        "port": str(u.port or 5432),
        "username": str(u.username or "postgres"),
        "password": str(u.password or ""),
        "database": str(u.database or "postgres"),
    }


def _same_pg_target(url_a: str, url_b: str) -> bool:
    if not url_a or not url_b:
        return False
    try:
        a = _pg_conn_spec(url_a)
        b = _pg_conn_spec(url_b)
        ha = "localhost" if a["host"] in ("127.0.0.1", "::1", "localhost") else a["host"].lower()
        hb = "localhost" if b["host"] in ("127.0.0.1", "::1", "localhost") else b["host"].lower()
        return (ha, a["port"], a["database"]) == (hb, b["port"], b["database"])
    except Exception:
        return _pg_cli_url(url_a).split("@")[-1] == _pg_cli_url(url_b).split("@")[-1]


def _scrub_pg_secrets(msg: str, password: str) -> str:
    if not msg:
        return ""
    out = msg
    if password:
        out = out.replace(password, "***REDACTED***")
    return out


def _escape_pgpass(val: str) -> str:
    return val.replace("\\", "\\\\").replace(":", "\\:")


def _write_temp_pgpass(spec: dict[str, str]) -> Path | None:
    if not spec.get("password"):
        return None
    fd, raw_path = tempfile.mkstemp(prefix="jarvis_pgpass_", suffix=".conf")
    os.close(fd)
    p = Path(raw_path)
    line = f"{_escape_pgpass(spec['host'])}:{_escape_pgpass(spec['port'])}:{_escape_pgpass(spec['database'])}:{_escape_pgpass(spec['username'])}:{_escape_pgpass(spec['password'])}\n"
    p.write_text(line, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


def _pg_cli_flags(spec: dict[str, str]) -> list[str]:
    """Build passwordless CLI flags (`--host`, `--port`, `--username`, `--no-password`); secrets go via PGPASSFILE only."""
    return ["--host", spec["host"], "--port", spec["port"], "--username", spec["username"], "--no-password"]


def _manifest_hmac(manifest_canonical: bytes, salt: bytes, passphrase: str) -> str:
    key = _derive_key(passphrase, salt, b"jdr-hmac")
    return hmac.new(key, manifest_canonical, hashlib.sha256).hexdigest()


class BackupDestination:
    """Optional backup destination storage interface."""

    name: str = "local"

    def upload(self, file_path: Path) -> str:
        raise NotImplementedError


class SecondaryPathDestination(BackupDestination):
    """Independent local/external drive destination."""

    name = "secondary_path"

    def __init__(self, dest_dir: Path):
        self.dest_dir = Path(dest_dir)

    def upload(self, file_path: Path) -> str:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        target = self.dest_dir / file_path.name
        shutil.copy2(file_path, target)
        return str(target)


class S3CompatibleDestination(BackupDestination):
    """Optional S3-compatible remote storage destination (encrypted archive ONLY, gated by Online Mode)."""

    name = "s3"

    def __init__(self, *, bucket: str, prefix: str = "jarvis-backups", endpoint: str = "", region: str = "us-east-1", settings: Settings | None = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint
        self.region = region
        self.settings = settings

    def upload(self, file_path: Path) -> str:
        if self.settings and (self.settings.offline_enforced and not (self.settings.internet.enabled and audit.is_online_enabled())):
            raise PermissionError("Remote S3 backup upload blocked in Local Mode.")
        if not file_path.name.endswith(".jvault"):
            raise ValueError("Refusing remote S3 upload of unencrypted bundle; must encrypt archive (.jvault) before upload.")
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError("S3 destination requires optional 'boto3' package.") from e
        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint or None,
            region_name=self.region,
        )
        key = f"{self.prefix}/{file_path.name}" if self.prefix else file_path.name
        client.upload_file(str(file_path), self.bucket, key)
        return f"s3://{self.bucket}/{key}"


class DisasterRecoveryManager:
    """Manages complete DB/Vector/Memory/Workflow/Repo backups, encryption, retention, and isolated DR drills."""

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings,
        *,
        data_dir: Path,
        secrets_path: Path,
        target_registry: DatabaseTargetRegistry | None = None,
        approvals: ApprovalService | None = None,
    ):
        self.db = db
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.secrets_path = Path(secrets_path)
        self._explicit_registry = target_registry
        self._manual_targets: list[tuple[str, str, str]] = []
        self.approvals = approvals or ApprovalService(db)
        bdir = Path(settings.recovery.backup_dir)
        self.backup_root = bdir if bdir.is_absolute() else (self.data_dir / "backups")
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def get_target_registry(self) -> DatabaseTargetRegistry:
        if self._explicit_registry is not None:
            reg = self._explicit_registry
            reg.set_active_db(self.db.sync_url, configured_role=str(getattr(self.settings.database, "role", "auto") or "auto"))
        else:
            reg = DatabaseTargetRegistry(self.settings, active_sync_url=self.db.sync_url, data_dir=self.data_dir)
        for role, url, label in self._manual_targets:
            reg.register(role, url, source="manual", label=label)
        return reg

    def register_database_target(self, role: str, url: str, *, label: str = "") -> dict:
        t = self.get_target_registry().register(role, url, source="manual", label=label)
        self._manual_targets.append((role, redact_db_url(url), label))
        return t.to_dict()

    def list_database_targets(self) -> list[dict]:
        return self.get_target_registry().list_targets()

    def _record_restore_audit(self, *, actor: str, decision: str, target_fp: str, details: dict) -> None:
        try:
            with self.db.sync_session() as s:
                s.add(
                    AuditEventRow(
                        actor=(actor or "system")[:64],
                        category="database_restore",
                        decision=decision[:24],
                        target=target_fp[:500],
                        details=details,
                    )
                )
                s.commit()
        except Exception:
            pass

    @staticmethod
    def recovery_steps_doc() -> list[str]:
        return [
            "1. Install prerequisites (Python 3.12+, Node 20+, Git, Ollama, optional PostgreSQL 16+/pgvector).",
            "2. Clone the Jarvis repository and create/activate `.venv` via `scripts/bootstrap.ps1`.",
            "3. Verify integrity & decrypt `secrets.vault.enc` / `.jvault` archive using the owner recovery passphrase.",
            "4. Restore `settings.json` configuration cleanly into `data/`.",
            "5. Restore secrets encrypted at rest with Windows DPAPI into `data/secrets.json`.",
            "6. If running PostgreSQL, run `pg_restore` / `psql` into the fresh target DB and verify row counts & schema.",
            "7. If running SQLite, copy verified `jarvis.db` snapshot atomically into `data/jarvis.db`.",
            "8. Verify / restore vector entries and embeddings (`pg_vector_entries` or SQLite `vector_entries`).",
            "9. Verify personal memory, experience lessons, and reminders loaded cleanly via FiveWayMemoryService.",
            "10. Verify project registry rows and restore Git repository bundles / optional uncommitted patches.",
            "11. Rebuild Graphify AST graphs (`sync_graphify`) across restored projects.",
            "12. Rebuild or verify Knowledge Base FTS5 index & learning metadata.",
            "13. Install/verify local Ollama LLM & embedding models (`ollama pull ...`).",
            "14. Verify Piper TTS voices (`models/piper/`) and Whisper STT models (`models/whisper/`).",
            "15. Execute `python preflight.py` and `python drill.py` to confirm loopback & health status.",
            "16. Inspect recoverable LangGraph agent runs / pending approvals before resuming paused workflows.",
        ]

    def backup_postgresql(self, out_dir: Path) -> dict:
        """Create real PostgreSQL dump via `pg_dump` using PGPASSFILE (never CLI passwords) with schema & row counts."""
        pg_dump = _find_pg_tool("pg_dump")
        if not pg_dump:
            raise FileNotFoundError("PostgreSQL backup failed: `pg_dump` CLI tool not found on system.")
        dump_path = out_dir / "db" / "postgres.dump"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        spec = _pg_conn_spec(self.db.sync_url)
        pgpass_path = _write_temp_pgpass(spec)
        env = dict(os.environ)
        if pgpass_path is not None:
            env["PGPASSFILE"] = str(pgpass_path)
        argv = [pg_dump, "--format=custom", "--no-owner", *_pg_cli_flags(spec), "--dbname", spec["database"], "--file", str(dump_path)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, shell=False, env=env)
        finally:
            if pgpass_path is not None:
                pgpass_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise RuntimeError(_scrub_pg_secrets(f"pg_dump failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}", spec["password"]))

        with self.db.sync_engine.connect() as conn:
            ver = conn.execute(text("SHOW server_version")).scalar()
            tables = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            ).scalars().all()
            counts = {}
            for t in tables:
                counts[t] = int(conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0)
            pgvector_ext = bool(conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar())

        meta = {"server_version": str(ver or ""), "tables": counts, "pgvector_installed": pgvector_ext, "dump_file": "db/postgres.dump"}
        (out_dir / "db" / "postgres_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def restore_postgresql(
        self,
        dump_path: Path,
        target_pg_url: str,
        *,
        expected_meta: dict | None = None,
        approval_token: str | None = None,
        owner_actor: str = "",
        verification_scratch_only: bool = False,
    ) -> dict:
        """Restore PostgreSQL dump via `pg_restore --exit-on-error` (PGPASSFILE) enforcing target protection & owner approval."""
        reg = self.get_target_registry()
        cls = reg.classify_target(target_pg_url)
        fp = cls["fingerprint"]
        dump_sha = _sha256_file(dump_path) if Path(dump_path).exists() else ""

        if cls["is_unknown"] or cls["is_ambiguous"]:
            self._record_restore_audit(
                actor=owner_actor or "system",
                decision="blocked_unknown",
                target_fp=fp,
                details={"reason": "unknown_or_ambiguous_target", "roles": cls["roles"], "dump_sha256": dump_sha},
            )
            raise ValueError(
                f"Refusing destructive pg_restore: target {fp} is {cls['primary_role']!r} "
                "(unknown or ambiguous database targets are rejected by default)."
            )

        if cls["is_production"] or cls["is_current_live"]:
            if verification_scratch_only:
                self._record_restore_audit(
                    actor=owner_actor or "system",
                    decision="blocked_verify_prod",
                    target_fp=fp,
                    details={"reason": "cannot_verify_on_production_or_live", "roles": cls["roles"]},
                )
                raise ValueError(f"Refusing backup verification: target {fp} is production/live DB.")
            if not approval_token:
                self._record_restore_audit(
                    actor=owner_actor or "system",
                    decision="blocked_production",
                    target_fp=fp,
                    details={"reason": "missing_owner_approval_token", "roles": cls["roles"]},
                )
                raise ValueError(
                    f"Refusing destructive pg_restore against production/live database {fp} without explicit owner approval token."
                )
            approved_req = self.approvals.consume_approved(approval_token, expected_action_type="restore_production_database")
            args = approved_req.get("args") or {}
            if args.get("target_fingerprint") != fp:
                raise PermissionError(f"Approval token target mismatch ({args.get('target_fingerprint')} != {fp}).")
            if args.get("dump_sha256") and dump_sha and args["dump_sha256"] != dump_sha:
                raise PermissionError("Dump file SHA-256 changed after owner production restore approval.")
            owner_actor = str(approved_req.get("decided_by") or owner_actor or "owner")
        elif verification_scratch_only and not cls["is_scratch"]:
            self._record_restore_audit(
                actor=owner_actor or "system",
                decision="blocked_non_scratch",
                target_fp=fp,
                details={"reason": "verification_requires_scratch_role", "roles": cls["roles"]},
            )
            raise ValueError(f"Refusing verification restore on role={cls['primary_role']!r}; use a dedicated 'scratch_pg' database.")

        pg_restore = _find_pg_tool("pg_restore")
        if not pg_restore:
            raise FileNotFoundError("PostgreSQL restore failed: `pg_restore` CLI tool not found.")
        spec = _pg_conn_spec(target_pg_url)
        pgpass_path = _write_temp_pgpass(spec)
        env = dict(os.environ)
        if pgpass_path is not None:
            env["PGPASSFILE"] = str(pgpass_path)
        argv = [
            pg_restore,
            "--exit-on-error",
            "--clean",
            "--if-exists",
            "--no-owner",
            *_pg_cli_flags(spec),
            "--dbname",
            spec["database"],
            str(dump_path),
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, shell=False, env=env)
        finally:
            if pgpass_path is not None:
                pgpass_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise RuntimeError(
                _scrub_pg_secrets(f"pg_restore failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}", spec["password"])
            )

        target_db = DatabaseManager(target_pg_url)
        pgvector_ok = False
        memory_count = 0
        vector_count = 0
        checkpoints_count = 0
        projects_count = 0
        vector_probe_ok = False
        try:
            with target_db.sync_engine.connect() as conn:
                tables = set(
                    conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    ).scalars().all()
                )
                restored_counts = {}
                for t in tables:
                    restored_counts[t] = int(conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0)
                pgvector_ok = bool(conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar())

            iso_mem = FiveWayMemoryService(target_db, settings=self.settings)
            mems = iso_mem.list_personal_memories()
            memory_count = len(mems)
            vector_count = iso_mem.vectors.count()
            if mems:
                hits = iso_mem.search_personal_memory(mems[0]["content"][:40], k=3)
                vector_probe_ok = len(hits) >= 1
            else:
                vector_probe_ok = True
            with target_db.sync_session() as s:
                checkpoints_count = len(s.scalars(select(AgentRunRow)).all())
                projects_count = len(s.scalars(select(ProjectRow)).all())
        finally:
            target_db.sync_engine.dispose()

        mismatches: dict[str, tuple[int, int]] = {}
        if expected_meta and expected_meta.get("tables"):
            for t, exp_c in expected_meta["tables"].items():
                got_c = restored_counts.get(t, -1)
                if got_c != exp_c:
                    mismatches[t] = (exp_c, got_c)

        expected_pgvec = bool((expected_meta or {}).get("pgvector_installed", True))
        pgvec_valid = (pgvector_ok or not expected_pgvec)
        ok_flag = len(mismatches) == 0 and pgvec_valid and vector_probe_ok
        self._record_restore_audit(
            actor=owner_actor or "system",
            decision="restored_ok" if ok_flag else "restore_mismatch",
            target_fp=fp,
            details={
                "role": cls["primary_role"],
                "approval_token": approval_token,
                "dump_sha256": dump_sha,
                "pgvector_installed": pgvector_ok,
                "mismatches": mismatches,
            },
        )
        return {
            "ok": ok_flag,
            "target_fingerprint": fp,
            "target_role": cls["primary_role"],
            "tables": restored_counts,
            "mismatches": mismatches,
            "pgvector_installed": pgvector_ok,
            "memory_count": memory_count,
            "vector_count": vector_count,
            "vector_probe_ok": vector_probe_ok,
            "checkpoints_count": checkpoints_count,
            "projects_count": projects_count,
            "warnings": [_scrub_pg_secrets(proc.stderr.strip(), spec["password"])] if proc.stderr.strip() else [],
        }

    def request_production_restore(
        self,
        bundle_path: Path,
        *,
        target_pg_url: str,
        passphrase: str,
        requested_by: str = "owner",
        reason: str = "",
    ) -> dict:
        """Verify bundle on scratch/checksums first and create a pinned single-use production restore approval request."""
        bp = Path(bundle_path).resolve()
        if not bp.exists():
            raise FileNotFoundError(f"Backup bundle not found: {bp}")
        reg = self.get_target_registry()
        cls = reg.classify_target(target_pg_url)
        if cls["is_unknown"] or cls["is_ambiguous"]:
            raise ValueError(f"Target {cls['fingerprint']} is {cls['primary_role']!r}; register target before requesting restore.")

        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            zpath = bp
            if bp.name.endswith(".jvault"):
                zpath = scratch / "inner.zip"
                zpath.write_bytes(decrypt_archive_bytes(bp.read_bytes(), passphrase))
            with zipfile.ZipFile(zpath, "r") as zf:
                zf.extractall(scratch)
            dump_fp = scratch / "db" / "postgres.dump"
            if not dump_fp.exists():
                raise ValueError("Bundle does not contain db/postgres.dump for PostgreSQL production restore.")
            dump_sha = _sha256_file(dump_fp)

        req = self.approvals.create_request(
            action_type="restore_production_database",
            target_resource=cls["fingerprint"],
            preview_diff=f"DESTRUCTIVE pg_restore --clean into {cls['fingerprint']} (roles={cls['roles']})\nBundle: {bp.name}\nDump SHA-256: {dump_sha}\nReason: {reason or 'owner requested'}",
            args={
                "bundle_path": str(bp),
                "target_fingerprint": cls["fingerprint"],
                "roles": cls["roles"],
                "dump_sha256": dump_sha,
                "requested_by": requested_by,
            },
        )
        self._record_restore_audit(
            actor=requested_by,
            decision="approval_requested",
            target_fp=cls["fingerprint"],
            details={"approval_token": req["token"], "bundle": str(bp), "dump_sha256": dump_sha},
        )
        return req

    def execute_production_restore(
        self,
        *,
        approval_token: str,
        target_pg_url: str,
        bundle_path: Path,
        passphrase: str,
    ) -> dict:
        """Execute production PostgreSQL restore ONLY with an already-approved single-use ApprovalRequestRow token."""
        bp = Path(bundle_path).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            zpath = bp
            if bp.name.endswith(".jvault"):
                zpath = scratch / "inner.zip"
                zpath.write_bytes(decrypt_archive_bytes(bp.read_bytes(), passphrase))
            with zipfile.ZipFile(zpath, "r") as zf:
                zf.extractall(scratch)
            pg_dump_file = scratch / "db" / "postgres.dump"
            if not pg_dump_file.exists():
                raise ValueError("Backup bundle does not contain db/postgres.dump")
            meta_file = scratch / "db" / "postgres_meta.json"
            pg_meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else None
            return self.restore_postgresql(
                pg_dump_file,
                target_pg_url,
                expected_meta=pg_meta,
                approval_token=approval_token,
            )

    def _export_structured_snapshots(self, stage: Path) -> dict[str, int]:
        """Export DB tables & vectors cleanly to JSONL snapshots for cross-dialect recovery."""
        snap_dir = stage / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}

        with self.db.sync_session() as s:
            pm = s.scalars(select(PersonalMemoryRow)).all()
            (snap_dir / "personal_memories.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "id": r.id,
                            "namespace": r.namespace,
                            "owner_scope": r.owner_scope,
                            "project_id": r.project_id,
                            "category": r.category,
                            "topic": r.topic,
                            "title": r.title,
                            "content": r.content,
                            "confirmed": r.confirmed,
                            "confidence": r.confidence,
                            "privacy_level": r.privacy_level,
                            "archived": r.archived,
                            "expires_at": r.expires_at,
                            "created_at": r.created_at,
                        }
                    )
                    for r in pm
                ),
                encoding="utf-8",
            )
            counts["personal_memories"] = len(pm)

            projs = s.scalars(select(ProjectRow)).all()
            (snap_dir / "projects.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "id": p.id,
                            "name": p.name,
                            "root_path": p.root_path,
                            "default_branch": p.default_branch,
                            "test_cmd": p.test_cmd,
                            "privacy_level": p.privacy_level,
                        }
                    )
                    for p in projs
                ),
                encoding="utf-8",
            )
            counts["projects"] = len(projs)

            runs = s.scalars(select(AgentRunRow)).all()
            (snap_dir / "agent_runs.jsonl").write_text(
                "\n".join(
                    json.dumps({"id": r.id, "workflow_name": r.workflow_name, "state": r.state, "thread_id": r.thread_id, "project_id": r.project_id})
                    for r in runs
                ),
                encoding="utf-8",
            )
            counts["agent_runs"] = len(runs)

            rems = s.scalars(select(ReminderRow)).all()
            (snap_dir / "reminders.jsonl").write_text(
                "\n".join(json.dumps({"id": r.id, "title": r.title, "due_at": r.due_at, "status": r.status}) for r in rems),
                encoding="utf-8",
            )
            counts["reminders"] = len(rems)

            exp = s.scalars(select(ExperienceLessonRow)).all()
            (snap_dir / "experience_lessons.jsonl").write_text(
                "\n".join(json.dumps({"id": e.id, "scope": e.scope, "topic": e.topic, "lesson_after": e.lesson_after}) for e in exp),
                encoding="utf-8",
            )
            counts["experience_lessons"] = len(exp)

            vecs = s.scalars(select(VectorEntryRow)).all()
            (snap_dir / "vectors.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "id": v.id,
                            "namespace": v.namespace,
                            "project_id": v.project_id,
                            "doc_id": v.doc_id,
                            "title": v.title,
                            "content": v.content,
                            "dim": v.dim,
                            "embedding_model": v.embedding_model,
                            "expires_at": v.expires_at,
                            "embedding_b64": base64.b64encode(v.embedding_blob).decode("ascii"),
                        }
                    )
                    for v in vecs
                ),
                encoding="utf-8",
            )
            counts["vectors"] = len(vecs)
        return counts

    @staticmethod
    def _git_run(args: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )

    def _export_repositories(self, stage: Path, include_uncommitted: bool) -> dict:
        backed: list[str] = []
        meta_list: list[dict] = []
        missing: list[str] = []
        repos_dir = stage / "repositories"
        with self.db.sync_session() as s:
            projs = s.scalars(select(ProjectRow)).all()
        for p in projs:
            root = Path(p.root_path)
            if not root.exists() or not (root / ".git").exists():
                missing.append(p.name)
                continue
            repos_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in p.name).strip(".") or p.id
            head_res = self._git_run(["rev-parse", "HEAD"], root, timeout=15)
            head_sha = head_res.stdout.strip() if head_res.returncode == 0 else ""
            br_res = self._git_run(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=15)
            branch = br_res.stdout.strip() if br_res.returncode == 0 else "main"

            bundle_name = f"{safe_name}.bundle"
            bundle_out = repos_dir / bundle_name
            res = self._git_run(["bundle", "create", str(bundle_out), "--all"], root, timeout=60)
            if res.returncode != 0:
                raise RuntimeError(f"git bundle create failed for {p.name}: {res.stderr.strip()}")
            backed.append(p.name)

            patch_name: str | None = None
            untracked_zip_name: str | None = None
            untracked_files: list[str] = []
            if include_uncommitted:
                # Capture all staged + unstaged tracked changes relative to HEAD
                diff = self._git_run(["diff", "HEAD", "--binary"], root, timeout=30)
                if diff.stdout.strip():
                    patch_name = f"{safe_name}.uncommitted.patch"
                    (repos_dir / patch_name).write_text(diff.stdout, encoding="utf-8")
                untracked_res = self._git_run(["ls-files", "--others", "--exclude-standard"], root, timeout=30)
                if untracked_res.returncode == 0:
                    untracked_files = [ln.strip() for ln in untracked_res.stdout.splitlines() if ln.strip()]
                if untracked_files:
                    untracked_zip_name = f"{safe_name}.untracked.zip"
                    with zipfile.ZipFile(repos_dir / untracked_zip_name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                        for rel in untracked_files:
                            fp = (root / rel).resolve()
                            if fp.is_file() and str(fp).startswith(str(root.resolve())):
                                zf.write(fp, rel.replace("\\", "/"))

            meta_list.append(
                {
                    "name": p.name,
                    "safe_name": safe_name,
                    "head_sha": head_sha,
                    "branch": branch,
                    "bundle_file": f"repositories/{bundle_name}",
                    "patch_file": f"repositories/{patch_name}" if patch_name else None,
                    "untracked_zip": f"repositories/{untracked_zip_name}" if untracked_zip_name else None,
                    "untracked_files": untracked_files,
                    "include_uncommitted": bool(include_uncommitted),
                }
            )
        if meta_list:
            repos_dir.mkdir(parents=True, exist_ok=True)
            (repos_dir / "repos_meta.json").write_text(json.dumps(meta_list, indent=2), encoding="utf-8")
        return {"backed": backed, "meta": meta_list, "missing": missing}

    def restore_repositories(
        self,
        extracted_root: Path,
        target_repos_dir: Path,
        *,
        overwrite_existing: bool = False,
        restore_uncommitted: bool = True,
    ) -> dict:
        """Restore exported Git bundles, optional tracked patches & untracked files, verifying HEAD and working tree."""
        repos_stage = Path(extracted_root) / "repositories"
        if not repos_stage.exists():
            return {"ok": True, "restored": [], "count": 0}
        meta_path = repos_stage / "repos_meta.json"
        if meta_path.exists():
            entries = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            entries = [
                {
                    "name": b.stem,
                    "safe_name": b.stem,
                    "head_sha": "",
                    "branch": "",
                    "bundle_file": f"repositories/{b.name}",
                    "patch_file": f"repositories/{b.stem}.uncommitted.patch" if (repos_stage / f"{b.stem}.uncommitted.patch").exists() else None,
                    "untracked_zip": None,
                    "untracked_files": [],
                }
                for b in sorted(repos_stage.glob("*.bundle"))
            ]

        base_target = Path(target_repos_dir).resolve()
        base_target.mkdir(parents=True, exist_ok=True)
        restored: list[dict] = []

        for ent in entries:
            dest_repo = (base_target / ent["safe_name"]).resolve()
            if dest_repo.parent != base_target:
                raise ValueError(f"Unsafe repository target path: {dest_repo}")
            if dest_repo.exists() and any(dest_repo.iterdir()):
                if not overwrite_existing:
                    raise FileExistsError(
                        f"Refusing to overwrite existing target repository '{dest_repo}' without overwrite_existing=True."
                    )
                _rmtree_readonly_safe(dest_repo)
            bundle_file = Path(extracted_root) / ent["bundle_file"]
            cl = subprocess.run(
                ["git", "clone", str(bundle_file), str(dest_repo)],
                capture_output=True,
                text=True,
                timeout=60,
                shell=False,
            )
            if cl.returncode != 0:
                raise RuntimeError(f"git clone from bundle failed for {ent['name']}: {cl.stderr.strip()}")

            if ent.get("branch") and ent["branch"] != "HEAD":
                self._git_run(["checkout", ent["branch"]], dest_repo, timeout=20)
            restored_head = self._git_run(["rev-parse", "HEAD"], dest_repo, timeout=15).stdout.strip()
            if ent.get("head_sha") and restored_head != ent["head_sha"]:
                raise RuntimeError(
                    f"Restored HEAD mismatch for {ent['name']}: expected {ent['head_sha']}, got {restored_head}"
                )

            patch_applied = False
            untracked_restored = 0
            if restore_uncommitted:
                if ent.get("patch_file"):
                    pf = Path(extracted_root) / ent["patch_file"]
                    if pf.exists() and pf.read_text(encoding="utf-8").strip():
                        ap = self._git_run(["apply", str(pf)], dest_repo, timeout=30)
                        if ap.returncode != 0:
                            raise RuntimeError(f"git apply failed for {ent['name']}: {ap.stderr.strip()}")
                        patch_applied = True
                if ent.get("untracked_zip"):
                    uz = Path(extracted_root) / ent["untracked_zip"]
                    if uz.exists():
                        with zipfile.ZipFile(uz, "r") as zf:
                            for info in zf.infolist():
                                p_rel = Path(info.filename)
                                if ".." in p_rel.parts or p_rel.is_absolute():
                                    raise ValueError(f"Unsafe path in untracked archive: {info.filename}")
                            zf.extractall(dest_repo)
                        for rel in ent.get("untracked_files") or []:
                            if not (dest_repo / rel).exists():
                                raise RuntimeError(f"Expected untracked file '{rel}' missing after restore in {ent['name']}")
                            untracked_restored += 1

            st = self._git_run(["status", "--porcelain"], dest_repo, timeout=20)
            restored.append(
                {
                    "name": ent["name"],
                    "path": str(dest_repo),
                    "head_sha": restored_head,
                    "patch_applied": patch_applied,
                    "untracked_restored": untracked_restored,
                    "working_tree_status": [ln for ln in st.stdout.splitlines() if ln.strip()],
                }
            )
        return {"ok": True, "restored": restored, "count": len(restored)}

    def apply_retention(self, tier: str, keep: int | None = None) -> int:
        tier_dir = self.backup_root / tier
        if not tier_dir.exists():
            return 0
        limit = keep or (
            self.settings.recovery.retain_tier1 if tier == "tier1_hot" else
            self.settings.recovery.retain_tier3 if tier == "tier3_cold" else
            self.settings.recovery.retain_tier2
        )
        bundles = sorted(
            [p for p in tier_dir.iterdir() if p.is_file() and (p.suffix in (".zip", ".jvault"))],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for stale in bundles[int(limit):]:
            stale.unlink(missing_ok=True)
            removed += 1
        return removed

    def create_backup(
        self,
        *,
        tier: str = "tier2_warm",
        passphrase: str,
        notes_dir: Path | None = None,
        extra_dest_dir: Path | None = None,
        remote_destination: BackupDestination | None = None,
        include_repositories: bool = False,
        include_uncommitted: bool = False,
        encrypt_archive: bool = False,
    ) -> dict:
        """Create atomic encrypted DR bundle covering SQL/PG, vectors, memory, workflows, docs, config & secrets."""
        if not passphrase or len(passphrase) < 8:
            raise ValueError("Recovery passphrase must be at least 8 characters")
        ts = time.strftime("%Y%m%d-%H%M%S")
        tier_dir = self.backup_root / tier
        tier_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = tier_dir / f"jarvis-{tier}-{ts}.zip"

        files_hashes: dict[str, str] = {}
        snapshot_counts: dict[str, int] = {}
        repos_backed: list[str] = []
        pg_meta: dict | None = None

        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            # 1. Database dump (PostgreSQL native `pg_dump` or SQLite online `backup`)
            if self.db.is_postgres:
                pg_meta = self.backup_postgresql(stage)
            else:
                db_src = self.data_dir / "jarvis.db"
                if db_src.exists():
                    db_snap = stage / "jarvis.db"
                    src_con = sqlite3.connect(str(db_src))
                    dst_con = sqlite3.connect(str(db_snap))
                    try:
                        src_con.backup(dst_con)
                    finally:
                        dst_con.close()
                        src_con.close()

            # 2. Cross-engine structured JSONL snapshots (vectors, 5-way memory, workflow runs, reminders, projects)
            snapshot_counts = self._export_structured_snapshots(stage)

            # 3. Encrypted portable secrets vault (NEVER plaintext secrets.json!)
            secrets = load_secrets_map(self.secrets_path)
            (stage / "secrets.vault.enc").write_bytes(export_encrypted_vault(secrets, passphrase))

            # 4. Configuration, notes, knowledge index, feedback/registry, user-created documents & calendar
            settings_override = self.data_dir / "settings.json"
            if settings_override.exists():
                shutil.copy2(settings_override, stage / "settings.json")
            if notes_dir and Path(notes_dir).exists():
                shutil.copytree(notes_dir, stage / "notes", ignore=shutil.ignore_patterns(".trash", "*.tmp"))

            for sub_rel in ("outputs/documents", "calendar", "outbox", "learning/knowledge", "learning/feedback"):
                src_sub = self.data_dir / sub_rel
                if src_sub.exists():
                    shutil.copytree(src_sub, stage / sub_rel, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp", "__pycache__"))

            learn_train = self.data_dir / "learning" / "training"
            if learn_train.exists():
                for fname in ("registry.json", "audit.jsonl"):
                    fp = learn_train / fname
                    if fp.exists():
                        (stage / "learning" / "training").mkdir(parents=True, exist_ok=True)
                        shutil.copy2(fp, stage / "learning" / "training" / fname)

            repos_res: dict = {"backed": [], "meta": [], "missing": []}
            if include_repositories:
                repos_res = self._export_repositories(stage, include_uncommitted=include_uncommitted)
            repos_backed = repos_res["backed"]
            repositories_included = bool(include_repositories and len(repos_backed) > 0)

            for item in sorted(stage.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(stage).as_posix()
                    files_hashes[rel] = _sha256_file(item)

            hmac_salt = os.urandom(16)
            manifest_core = {
                "format": "jarvis-dr-bundle-v2",
                "tier": tier,
                "created_at": round(time.time(), 3),
                "dialect": "postgresql" if self.db.is_postgres else "sqlite",
                "snapshot_counts": snapshot_counts,
                "pg_meta": pg_meta,
                "repositories": repos_backed,
                "repositories_included": repositories_included,
                "repositories_requested": bool(include_repositories),
                "repositories_missing": repos_res["missing"],
                "include_uncommitted": bool(include_uncommitted),
                "files": files_hashes,
            }
            canonical_bytes = json.dumps(manifest_core, indent=2, sort_keys=True).encode("utf-8")
            manifest = {
                **manifest_core,
                "hmac_salt_b64": base64.b64encode(hmac_salt).decode("ascii"),
                "hmac_sha256": _manifest_hmac(canonical_bytes, hmac_salt, passphrase),
            }
            manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            (stage / "manifest.json").write_bytes(manifest_bytes)
            manifest_sha = _sha256_bytes(manifest_bytes)

            tmp_zip = bundle_path.with_suffix(".tmp")
            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for item in sorted(stage.rglob("*")):
                    if item.is_file():
                        zf.write(item, item.relative_to(stage).as_posix())
            os.replace(tmp_zip, bundle_path)

        encrypted_path: Path | None = None
        if encrypt_archive or remote_destination is not None or self.settings.recovery.encrypt_bundles:
            enc_target = bundle_path.with_suffix(".zip.jvault")
            enc_target.write_bytes(encrypt_archive_bytes(bundle_path.read_bytes(), passphrase))
            encrypted_path = enc_target

        tier3_copy = None
        if extra_dest_dir or self.settings.recovery.tier3_dir:
            dest = SecondaryPathDestination(Path(extra_dest_dir or self.settings.recovery.tier3_dir))
            tier3_copy = dest.upload(encrypted_path or bundle_path)

        remote_uri = None
        if remote_destination is not None:
            if encrypted_path is None:
                enc_target = bundle_path.with_suffix(".zip.jvault")
                enc_target.write_bytes(encrypt_archive_bytes(bundle_path.read_bytes(), passphrase))
                encrypted_path = enc_target
            remote_uri = remote_destination.upload(encrypted_path)

        self.apply_retention(tier)

        rec = {
            "tier": tier,
            "bundle_path": str(bundle_path),
            "encrypted_archive_path": str(encrypted_path) if encrypted_path else None,
            "tier3_copy": tier3_copy,
            "remote_uri": remote_uri,
            "sha256_manifest": manifest_sha,
            "size_bytes": bundle_path.stat().st_size,
            "file_count": len(files_hashes),
            "snapshot_counts": snapshot_counts,
            "repositories": repos_backed,
            "repositories_included": repositories_included,
            "repositories_requested": bool(include_repositories),
            "repositories_missing": repos_res["missing"],
            "include_uncommitted": bool(include_uncommitted),
            "encrypted": True,
            "verified_restore": False,
        }
        with self.db.sync_session() as s:
            s.add(
                BackupManifestRow(
                    tier=tier,
                    bundle_path=str(bundle_path),
                    sha256_manifest=manifest_sha,
                    encrypted=True,
                    size_bytes=rec["size_bytes"],
                    verified_restore=False,
                    details={
                        "files": files_hashes,
                        "tier3_copy": rec["tier3_copy"],
                        "encrypted_archive_path": rec["encrypted_archive_path"],
                        "remote_uri": remote_uri,
                        "snapshot_counts": snapshot_counts,
                        "repositories_included": repositories_included,
                        "repositories": repos_backed,
                    },
                )
            )
            s.commit()
        return rec

    def verify_backup(
        self,
        bundle_path: Path,
        *,
        passphrase: str,
        scratch_pg_url: str | None = None,
    ) -> dict:
        """Isolated restore dry-run NEVER touching live production DB: verifies decryption, HMAC authenticity,
        file checksums, DB integrity, memory retrieval, vector retrieval, checkpoint recovery, project repos & RTO."""
        t0 = time.perf_counter()
        bp = Path(bundle_path).resolve()
        if not bp.exists():
            raise FileNotFoundError(f"Backup bundle not found: {bp}")
        if scratch_pg_url:
            cls = self.get_target_registry().classify_target(scratch_pg_url)
            if not cls["is_scratch"]:
                raise ValueError(
                    f"Refusing backup verification: scratch_pg_url target {cls['fingerprint']} has role={cls['primary_role']!r} "
                    "(must be an isolated registered 'scratch_pg' database, never production, live, unknown, or ambiguous)."
                )

        warnings: list[str] = []
        db_verified = False
        repos_included = False
        repos_verified = False
        verified_repos_count = 0
        repositories_list: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            zip_to_extract = bp
            if bp.name.endswith(".jvault"):
                decrypted_zip = scratch / "decrypted.zip"
                decrypted_zip.write_bytes(decrypt_archive_bytes(bp.read_bytes(), passphrase))
                zip_to_extract = decrypted_zip

            with zipfile.ZipFile(zip_to_extract, "r") as zf:
                for info in zf.infolist():
                    if ".." in Path(info.filename).parts or Path(info.filename).is_absolute():
                        raise ValueError(f"Unsafe path in backup bundle: {info.filename}")
                zf.extractall(scratch)

            manifest_file = scratch / "manifest.json"
            if not manifest_file.exists():
                raise ValueError("manifest.json missing from backup bundle")
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

            # Verify passphrase-keyed HMAC-SHA256 authenticity tag if v2 bundle
            if "hmac_sha256" in manifest and "hmac_salt_b64" in manifest:
                core = {k: v for k, v in manifest.items() if k not in ("hmac_sha256", "hmac_salt_b64")}
                canonical = json.dumps(core, indent=2, sort_keys=True).encode("utf-8")
                salt = base64.b64decode(manifest["hmac_salt_b64"])
                expected_hmac = _manifest_hmac(canonical, salt, passphrase)
                if not hmac.compare_digest(expected_hmac, manifest["hmac_sha256"]):
                    raise ValueError("Backup manifest HMAC authenticity verification failed (wrong passphrase or tampered manifest)")

            files = manifest.get("files") or {}
            verified_files = 0
            for rel, expected_sha in files.items():
                fp = (scratch / rel).resolve()
                if not fp.exists() or _sha256_file(fp) != expected_sha:
                    raise ValueError(f"Checksum verification failed for {rel}")
                verified_files += 1

            secrets = import_encrypted_vault((scratch / "secrets.vault.enc").read_bytes(), passphrase)

            db_tables = 0
            memory_count = 0
            vector_count = 0
            vector_probe_ok = False
            checkpoints_count = 0
            projects_count = 0

            db_file = scratch / "jarvis.db"
            pg_dump_file = scratch / "db" / "postgres.dump"
            if db_file.exists():
                con = sqlite3.connect(str(db_file))
                try:
                    check = con.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0].lower() != "ok":
                        raise ValueError(f"SQLite integrity check failed: {check}")
                    db_tables = int(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
                finally:
                    con.close()

                # Boot an isolated DB manager against scratch DB ONLY to validate real memory/vector/checkpoint retrieval!
                iso_db = DatabaseManager(f"sqlite:///{db_file.as_posix()}")
                try:
                    iso_mem = FiveWayMemoryService(iso_db, settings=self.settings)
                    mems = iso_mem.list_personal_memories()
                    memory_count = len(mems)
                    vector_count = iso_mem.vectors.count()
                    if mems:
                        hits = iso_mem.search_personal_memory(mems[0]["content"][:40], k=3)
                        vector_probe_ok = len(hits) >= 1
                    else:
                        vector_probe_ok = True
                    with iso_db.sync_session() as s:
                        checkpoints_count = len(s.scalars(select(AgentRunRow)).all())
                        projects_count = len(s.scalars(select(ProjectRow)).all())
                finally:
                    iso_db.sync_engine.dispose()
                db_verified = True
            elif pg_dump_file.exists():
                pg_meta_file = scratch / "db" / "postgres_meta.json"
                pg_meta = json.loads(pg_meta_file.read_text(encoding="utf-8")) if pg_meta_file.exists() else None
                if scratch_pg_url:
                    pg_rep = self.restore_postgresql(
                        pg_dump_file,
                        scratch_pg_url,
                        expected_meta=pg_meta,
                        verification_scratch_only=True,
                    )
                    if not pg_rep["ok"]:
                        raise ValueError(
                            f"PostgreSQL isolated restore verification failed: mismatches={pg_rep['mismatches']} "
                            f"pgvector={pg_rep['pgvector_installed']} vector_probe={pg_rep['vector_probe_ok']}"
                        )
                    db_tables = len(pg_rep["tables"])
                    memory_count = pg_rep["memory_count"]
                    vector_count = pg_rep["vector_count"]
                    vector_probe_ok = pg_rep["vector_probe_ok"]
                    checkpoints_count = pg_rep["checkpoints_count"]
                    projects_count = pg_rep["projects_count"]
                    db_verified = True
                else:
                    db_verified = False
                    warnings.append("PostgreSQL dump checksum verified ONLY; live pg_restore verification did NOT run because scratch_pg_url was not supplied.")
            else:
                db_verified = False
                warnings.append("No database file present in bundle.")

            repositories_list = list(manifest.get("repositories") or [])
            repos_included = bool(manifest.get("repositories_included") and repositories_list)
            if repos_included and (scratch / "repositories").exists():
                repo_rep = self.restore_repositories(
                    scratch,
                    scratch / "_repo_verify",
                    overwrite_existing=True,
                    restore_uncommitted=bool(manifest.get("include_uncommitted", True)),
                )
                verified_repos_count = repo_rep["count"]
                repos_verified = verified_repos_count == len(repositories_list)
            elif not repos_included:
                warnings.append("Bundle scope is database_and_app_state_only (repositories_included=False); workspace Git repositories not included.")

        created_at = float(manifest.get("created_at") or time.time())
        rto_s = round(time.perf_counter() - t0, 3)
        rpo_s = max(0.0, round(time.time() - created_at, 3))

        verified_ok = bool(db_verified and (repos_verified or not repos_included))
        full_recovery_verified = bool(db_verified and repos_included and repos_verified)
        database_only_verified = bool(db_verified and not repos_included)
        recovery_scope = (
            "full_recovery_complete" if full_recovery_verified
            else "database_and_app_state_only" if db_verified
            else "incomplete"
        )

        if verified_ok:
            match_paths = [str(bp)]
            if bp.name.endswith(".jvault"):
                match_paths.append(str(bp)[: -len(".jvault")])
            with self.db.sync_session() as s:
                rows = s.query(BackupManifestRow).filter(BackupManifestRow.bundle_path.in_(match_paths)).all()
                for r in rows:
                    r.verified_restore = True
                if rows:
                    s.commit()

        return {
            "ok": verified_ok,
            "verified_restore": verified_ok,
            "db_verified": db_verified,
            "verified_files": verified_files,
            "secrets_count": len(secrets),
            "db_tables": db_tables,
            "memory_count": memory_count,
            "vector_count": vector_count,
            "vector_probe_ok": vector_probe_ok,
            "checkpoints_count": checkpoints_count,
            "projects_count": projects_count,
            "repositories": repositories_list,
            "repositories_included": repos_included,
            "repositories_verified": repos_verified,
            "verified_repos_count": verified_repos_count,
            "database_only_verified": database_only_verified,
            "full_recovery_complete": full_recovery_verified,
            "recovery_scope": recovery_scope,
            "rto_seconds": rto_s,
            "rpo_seconds": rpo_s,
            "warnings": warnings,
            "bundle_path": str(bp),
        }

    def restore_secrets_from_bundle(self, bundle_path: Path, *, passphrase: str, dest_secrets_path: Path | None = None) -> int:
        bp = Path(bundle_path).resolve()
        if bp.name.endswith(".jvault"):
            raw = decrypt_archive_bytes(bp.read_bytes(), passphrase)
            with tempfile.TemporaryDirectory() as tmp:
                zp = Path(tmp) / "inner.zip"
                zp.write_bytes(raw)
                with zipfile.ZipFile(zp, "r") as zf:
                    vault = zf.read("secrets.vault.enc")
        else:
            with zipfile.ZipFile(bp, "r") as zf:
                vault = zf.read("secrets.vault.enc")
        secrets = import_encrypted_vault(vault, passphrase)
        save_secrets_map(dest_secrets_path or self.secrets_path, secrets, encrypt_dpapi=True)
        return len(secrets)

    def restore_to_directory(
        self,
        bundle_path: Path,
        *,
        passphrase: str,
        target_data_dir: Path,
        target_notes_dir: Path | None = None,
        target_pg_url: str | None = None,
        target_repos_dir: Path | None = None,
        overwrite_existing: bool = False,
        restore_uncommitted: bool = True,
    ) -> dict:
        """Execute machine & optional repository recovery onto a target directory after isolated verification."""
        verify_rep = self.verify_backup(bundle_path, passphrase=passphrase, scratch_pg_url=target_pg_url)
        if not verify_rep["ok"]:
            raise RuntimeError(f"Backup verification failed prior to restore: {verify_rep.get('warnings')}")

        dst_data = Path(target_data_dir).resolve()
        dst_data.mkdir(parents=True, exist_ok=True)
        bp = Path(bundle_path).resolve()

        repo_restore_rep: dict = {"ok": True, "restored": [], "count": 0}
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            zpath = bp
            if bp.name.endswith(".jvault"):
                zpath = scratch / "inner.zip"
                zpath.write_bytes(decrypt_archive_bytes(bp.read_bytes(), passphrase))
            with zipfile.ZipFile(zpath, "r") as zf:
                for info in zf.infolist():
                    if ".." in Path(info.filename).parts or Path(info.filename).is_absolute():
                        raise ValueError(f"Unsafe path in backup bundle: {info.filename}")
                zf.extractall(scratch)

            if (scratch / "jarvis.db").exists():
                db_dst = dst_data / "jarvis.db"
                if db_dst.exists() and not overwrite_existing:
                    raise FileExistsError(f"Refusing to overwrite existing restored database '{db_dst}' without overwrite_existing=True.")
                shutil.copy2(scratch / "jarvis.db", db_dst)
            if (scratch / "settings.json").exists():
                shutil.copy2(scratch / "settings.json", dst_data / "settings.json")
            self.restore_secrets_from_bundle(bp, passphrase=passphrase, dest_secrets_path=dst_data / "secrets.json")

            for sub in ("outputs/documents", "calendar", "outbox", "learning"):
                src_sub = scratch / sub
                if src_sub.exists():
                    shutil.copytree(src_sub, dst_data / sub, dirs_exist_ok=True)
            if target_notes_dir and (scratch / "notes").exists():
                Path(target_notes_dir).mkdir(parents=True, exist_ok=True)
                shutil.copytree(scratch / "notes", Path(target_notes_dir), dirs_exist_ok=True)

            if (scratch / "repositories").exists() and target_repos_dir is not None:
                repo_restore_rep = self.restore_repositories(
                    scratch,
                    Path(target_repos_dir),
                    overwrite_existing=overwrite_existing,
                    restore_uncommitted=restore_uncommitted,
                )

        repos_expected = len(verify_rep.get("repositories") or [])
        full_recovery_complete = bool(
            verify_rep.get("repositories_included")
            and target_repos_dir is not None
            and repo_restore_rep.get("count", 0) == repos_expected
        )
        recovery_scope = "full_recovery_complete" if full_recovery_complete else "database_and_app_state_only"
        return {
            "ok": True,
            "restored_data_dir": str(dst_data),
            "verification": verify_rep,
            "repositories_restored": repo_restore_rep["restored"],
            "repositories_included": verify_rep.get("repositories_included", False),
            "full_recovery_complete": full_recovery_complete,
            "recovery_scope": recovery_scope,
        }

    def list_manifests(self) -> list[dict]:
        with self.db.sync_session() as s:
            rows = s.query(BackupManifestRow).order_by(BackupManifestRow.created_at.desc()).limit(30).all()
            return [
                {
                    "id": r.id,
                    "tier": r.tier,
                    "bundle_path": r.bundle_path,
                    "sha256_manifest": r.sha256_manifest,
                    "size_bytes": r.size_bytes,
                    "verified_restore": r.verified_restore,
                    "details": r.details or {},
                    "created_at": r.created_at,
                }
                for r in rows
            ]
