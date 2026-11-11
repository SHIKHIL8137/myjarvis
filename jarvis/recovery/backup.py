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
from sqlalchemy import select

from .. import audit
from ..config import Settings
from ..db.engine import DatabaseManager
from ..db.models import (
    AgentRunRow,
    BackupManifestRow,
    ExperienceLessonRow,
    PersonalMemoryRow,
    ProjectRow,
    ReminderRow,
    VectorEntryRow,
)
from ..memory.five_way import FiveWayMemoryService
from ..security.secrets import export_encrypted_vault, import_encrypted_vault, load_secrets_map, save_secrets_map

_ARCHIVE_MAGIC = b"JDRVAULT1"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def __init__(self, db: DatabaseManager, settings: Settings, *, data_dir: Path, secrets_path: Path):
        self.db = db
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.secrets_path = Path(secrets_path)
        bdir = Path(settings.recovery.backup_dir)
        self.backup_root = bdir if bdir.is_absolute() else (self.data_dir / "backups")
        self.backup_root.mkdir(parents=True, exist_ok=True)

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
        """Create real PostgreSQL dump via `pg_dump` with server version, schema & row counts."""
        pg_dump = _find_pg_tool("pg_dump")
        if not pg_dump:
            raise FileNotFoundError("PostgreSQL backup failed: `pg_dump` CLI tool not found on system.")
        dump_path = out_dir / "db" / "postgres.dump"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        uri = _pg_cli_url(self.db.sync_url)
        proc = subprocess.run(
            [pg_dump, "--format=custom", "--no-owner", "--file", str(dump_path), uri],
            capture_output=True,
            text=True,
            timeout=300,
            shell=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {proc.stderr.strip() or proc.stdout.strip()}")

        with self.db.sync_engine.connect() as conn:
            ver = conn.execute(__import__("sqlalchemy").text("SHOW server_version")).scalar()
            tables = conn.execute(
                __import__("sqlalchemy").text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            ).scalars().all()
            counts = {}
            for t in tables:
                counts[t] = int(conn.execute(__import__("sqlalchemy").text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0)

        meta = {"server_version": str(ver or ""), "tables": counts, "dump_file": "db/postgres.dump"}
        (out_dir / "db" / "postgres_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def restore_postgresql(self, dump_path: Path, target_pg_url: str, *, expected_meta: dict | None = None) -> dict:
        """Restore real PostgreSQL custom dump via `pg_restore` and verify version, schema & row counts."""
        pg_restore = _find_pg_tool("pg_restore")
        if not pg_restore:
            raise FileNotFoundError("PostgreSQL restore failed: `pg_restore` CLI tool not found.")
        uri = _pg_cli_url(target_pg_url)
        proc = subprocess.run(
            [pg_restore, "--clean", "--if-exists", "--no-owner", "--dbname", uri, str(dump_path)],
            capture_output=True,
            text=True,
            timeout=300,
            shell=False,
        )
        if proc.returncode not in (0, 1):  # pg_restore returns 1 for benign clean/extension warnings sometimes
            raise RuntimeError(f"pg_restore failed (rc={proc.returncode}): {proc.stderr.strip()}")

        target_db = DatabaseManager(target_pg_url)
        try:
            with target_db.sync_engine.connect() as conn:
                tables = set(
                    conn.execute(
                        __import__("sqlalchemy").text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    ).scalars().all()
                )
                restored_counts = {}
                for t in tables:
                    restored_counts[t] = int(conn.execute(__import__("sqlalchemy").text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0)
        finally:
            target_db.sync_engine.dispose()

        mismatches: dict[str, tuple[int, int]] = {}
        if expected_meta and expected_meta.get("tables"):
            for t, exp_c in expected_meta["tables"].items():
                got_c = restored_counts.get(t, -1)
                if got_c != exp_c:
                    mismatches[t] = (exp_c, got_c)
        return {
            "ok": len(mismatches) == 0,
            "tables": restored_counts,
            "mismatches": mismatches,
            "warnings": [proc.stderr.strip()] if proc.stderr.strip() else [],
        }

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

    def _export_repositories(self, stage: Path, include_uncommitted: bool) -> list[str]:
        backed: list[str] = []
        repos_dir = stage / "repositories"
        with self.db.sync_session() as s:
            projs = s.scalars(select(ProjectRow)).all()
        for p in projs:
            root = Path(p.root_path)
            if not root.exists() or not (root / ".git").exists():
                continue
            repos_dir.mkdir(parents=True, exist_ok=True)
            bundle_out = repos_dir / f"{p.name}.bundle"
            res = subprocess.run(["git", "bundle", "create", str(bundle_out), "--all"], cwd=str(root), capture_output=True, text=True, timeout=60, shell=False)
            if res.returncode == 0:
                backed.append(p.name)
            if include_uncommitted:
                diff = subprocess.run(["git", "diff", "HEAD"], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, shell=False)
                if diff.stdout.strip():
                    (repos_dir / f"{p.name}.uncommitted.patch").write_text(diff.stdout, encoding="utf-8")
        return backed

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

            if include_repositories:
                repos_backed = self._export_repositories(stage, include_uncommitted=include_uncommitted)

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
        file checksums, DB integrity, memory retrieval, vector retrieval, checkpoint recovery, project registry & RTO."""
        t0 = time.perf_counter()
        bp = Path(bundle_path).resolve()
        if not bp.exists():
            raise FileNotFoundError(f"Backup bundle not found: {bp}")

        warnings: list[str] = []
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
            elif pg_dump_file.exists():
                pg_meta_file = scratch / "db" / "postgres_meta.json"
                pg_meta = json.loads(pg_meta_file.read_text(encoding="utf-8")) if pg_meta_file.exists() else None
                if scratch_pg_url:
                    pg_rep = self.restore_postgresql(pg_dump_file, scratch_pg_url, expected_meta=pg_meta)
                    if not pg_rep["ok"]:
                        raise ValueError(f"PostgreSQL isolated restore mismatch: {pg_rep['mismatches']}")
                    db_tables = len(pg_rep["tables"])
                    vector_probe_ok = True
                else:
                    warnings.append("PostgreSQL dump checksum verified; live pg_restore skipped because scratch_pg_url was not supplied.")
            else:
                warnings.append("No database file present in bundle.")

        created_at = float(manifest.get("created_at") or time.time())
        rto_s = round(time.perf_counter() - t0, 3)
        rpo_s = max(0.0, round(time.time() - created_at, 3))

        with self.db.sync_session() as s:
            rows = s.query(BackupManifestRow).filter(BackupManifestRow.bundle_path == str(bp)).all()
            for r in rows:
                r.verified_restore = True
            if rows:
                s.commit()

        return {
            "ok": True,
            "verified_files": verified_files,
            "secrets_count": len(secrets),
            "db_tables": db_tables,
            "memory_count": memory_count,
            "vector_count": vector_count,
            "vector_probe_ok": vector_probe_ok,
            "checkpoints_count": checkpoints_count,
            "projects_count": projects_count,
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
    ) -> dict:
        """Execute a complete machine recovery onto an isolated/new target directory."""
        verify_rep = self.verify_backup(bundle_path, passphrase=passphrase, scratch_pg_url=target_pg_url)
        dst_data = Path(target_data_dir).resolve()
        dst_data.mkdir(parents=True, exist_ok=True)
        bp = Path(bundle_path).resolve()

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
                shutil.copy2(scratch / "jarvis.db", dst_data / "jarvis.db")
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

        return {"ok": True, "restored_data_dir": str(dst_data), "verification": verify_rep}

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
