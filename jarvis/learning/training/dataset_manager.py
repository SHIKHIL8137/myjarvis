"""Dataset import, gated download, immutable version archive, checksum & safe zip extraction (Phase 5)."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import threading
import time
from urllib.parse import urlsplit
import zipfile

from ... import audit
from ...config import InternetSpec, LearningSpec, ROOT
from ...netsec import safe_request
from ...search import SearchOfflineError
from ..feedback import FeedbackStore
from .dataset_validator import normalize_license
from .training_audit import TrainingAuditLog

_UNSAFE_EXT = {".pkl", ".pickle", ".pt", ".pth", ".bin", ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".vbs"}
_DATASET_EXT = {".jsonl", ".json"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class DatasetManager:
    """Manages immutable raw dataset snapshots with SHA-256 verification and safe archive extraction."""

    def __init__(
        self,
        spec: LearningSpec | None = None,
        *,
        training_dir: Path | None = None,
        is_online_fn=lambda: False,
        audit_log: TrainingAuditLog | None = None,
    ):
        from . import TRAINING_DIR
        self.spec = spec or LearningSpec()
        self.root = Path(training_dir) if training_dir else TRAINING_DIR
        self.raw_dir = self.root / "raw"
        self.manifest_path = self.root / "datasets.json"
        self.is_online_fn = is_online_fn
        self.audit = audit_log or TrainingAuditLog(self.root / "audit.jsonl")
        self._lock = threading.RLock()
        self._datasets: dict[str, dict] = {}
        self._load()

    def _load(self):
        with self._lock:
            if not self.manifest_path.exists():
                self._datasets = {}
                return
            try:
                self._datasets = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except Exception:
                self._datasets = {}

    def _save(self):
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._datasets, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.manifest_path)

    def _next_version(self, dataset_id: str) -> int:
        cur = self._datasets.get(dataset_id)
        if not cur:
            return 1
        return int(cur.get("latest_version") or 0) + 1

    def _safe_extract_zip(self, zip_path: Path, dest_dir: Path) -> Path:
        """Safely extract ONLY JSON/JSONL files from a zip archive, rejecting path slip and executables."""
        extracted_candidates: list[Path] = []
        total_uncompressed = 0
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = PurePosixPath(info.filename.replace("\\", "/"))
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"Unsafe archive path traversal blocked: {info.filename}")
                ext = Path(rel.name).suffix.lower()
                if ext in _UNSAFE_EXT:
                    raise ValueError(f"Unsafe or executable file inside archive rejected: {info.filename}")
                if ext not in _DATASET_EXT:
                    continue
                total_uncompressed += info.file_size
                if total_uncompressed > self.spec.max_download_bytes:
                    raise ValueError("Extracted archive exceeds max_download_bytes limit")
                out_path = (dest_dir / rel.name).resolve()
                if dest_dir.resolve() not in out_path.parents:
                    raise ValueError(f"Archive path escape blocked: {info.filename}")
                with zf.open(info, "r") as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted_candidates.append(out_path)
        if not extracted_candidates:
            raise ValueError("Archive contained no .json or .jsonl dataset files")
        return extracted_candidates[0]

    def _register_snapshot(
        self,
        *,
        dataset_id: str,
        name: str,
        version: int,
        data_file: Path,
        sha: str,
        license_name: str,
        source_kind: str,
        source_location: str,
    ) -> dict:
        entry = {
            "version": version,
            "file": str(data_file),
            "sha256": sha,
            "bytes": data_file.stat().st_size,
            "license": normalize_license(license_name),
            "source_kind": source_kind,
            "source_location": source_location,
            "imported_at": round(time.time(), 3),
            "validation": None,
            "cleaning": None,
        }
        with self._lock:
            rec = self._datasets.setdefault(dataset_id, {
                "id": dataset_id,
                "name": name or dataset_id,
                "latest_version": 0,
                "versions": {},
            })
            rec["name"] = name or rec.get("name") or dataset_id
            rec["latest_version"] = version
            rec["versions"][str(version)] = entry
            self._save()
        self.audit.record("dataset_imported", details={"dataset_id": dataset_id, "version": version, "sha256": sha, "source_kind": source_kind})
        return entry

    def import_local_file(
        self,
        src_path: str | Path,
        *,
        dataset_id: str,
        license_name: str = "mit",
        name: str = "",
        expected_sha256: str = "",
    ) -> dict:
        p = Path(src_path)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        else:
            p = p.resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Dataset file not found: {p}")
        ext = p.suffix.lower()
        if ext in _UNSAFE_EXT:
            raise ValueError(f"Unsafe binary/pickle dataset format rejected: '{ext}'")
        if ext not in _DATASET_EXT and ext != ".zip":
            raise ValueError(f"Unsupported dataset extension '{ext}'. Allowed: .jsonl, .json, .zip")
        if p.stat().st_size > self.spec.max_download_bytes:
            raise ValueError(f"Dataset file exceeds max_download_bytes ({self.spec.max_download_bytes})")

        actual_sha = sha256_file(p)
        if expected_sha256 and actual_sha.lower() != expected_sha256.strip().lower():
            raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}")

        ver = self._next_version(dataset_id)
        vdir = self.raw_dir / dataset_id / f"v{ver}"
        vdir.mkdir(parents=True, exist_ok=True)
        if ext == ".zip":
            tmp_zip = vdir / p.name
            shutil.copy2(p, tmp_zip)
            data_file = self._safe_extract_zip(tmp_zip, vdir)
            final_sha = sha256_file(data_file)
        else:
            data_file = vdir / f"dataset{ext}"
            shutil.copy2(p, data_file)
            final_sha = actual_sha
        return self._register_snapshot(
            dataset_id=dataset_id, name=name or p.stem, version=ver,
            data_file=data_file, sha=final_sha, license_name=license_name,
            source_kind="local_file", source_location=str(p),
        )

    async def download_remote(
        self,
        url: str,
        *,
        dataset_id: str,
        license_name: str,
        name: str = "",
        expected_sha256: str = "",
    ) -> dict:
        if not (self.is_online_fn() and audit.is_online_enabled()):
            raise SearchOfflineError("Local Mode active: remote dataset download blocked.")
        host = (urlsplit(url).hostname or "").lower()
        if host not in {h.lower() for h in self.spec.allowed_knowledge_hosts}:
            raise ValueError(f"Dataset host '{host}' is not in allowed_knowledge_hosts.")

        net_spec = InternetSpec(enabled=True, max_fetch_bytes=self.spec.max_download_bytes, timeout_s=15.0)
        res = await safe_request("GET", url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=self.spec.max_download_bytes)
        body = res.body
        actual_sha = hashlib.sha256(body).hexdigest()
        if expected_sha256 and actual_sha.lower() != expected_sha256.strip().lower():
            raise ValueError(f"Downloaded dataset SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}")

        filename = Path(urlsplit(url).path).name or "dataset.jsonl"
        ext = Path(filename).suffix.lower() or ".jsonl"
        if ext in _UNSAFE_EXT or (ext not in _DATASET_EXT and ext != ".zip"):
            raise ValueError(f"Unsupported or unsafe remote dataset extension '{ext}'")

        ver = self._next_version(dataset_id)
        vdir = self.raw_dir / dataset_id / f"v{ver}"
        vdir.mkdir(parents=True, exist_ok=True)
        raw_path = vdir / f"dataset{ext}"
        raw_path.write_bytes(body)
        if ext == ".zip":
            data_file = self._safe_extract_zip(raw_path, vdir)
            final_sha = sha256_file(data_file)
        else:
            data_file = raw_path
            final_sha = actual_sha
        return self._register_snapshot(
            dataset_id=dataset_id, name=name or dataset_id, version=ver,
            data_file=data_file, sha=final_sha, license_name=license_name,
            source_kind="remote_url", source_location=url,
        )

    def import_approved_feedback(self, feedback: FeedbackStore, *, dataset_id: str = "approved-feedback") -> dict:
        examples = feedback.export_training_examples()
        if not examples:
            raise ValueError("No approved feedback examples are ready to export into a dataset.")
        ver = self._next_version(dataset_id)
        vdir = self.raw_dir / dataset_id / f"v{ver}"
        vdir.mkdir(parents=True, exist_ok=True)
        out_path = vdir / "dataset.jsonl"
        payload = "\n".join(json.dumps(ex, ensure_ascii=False) for ex in examples) + "\n"
        out_path.write_text(payload, encoding="utf-8")
        sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self._register_snapshot(
            dataset_id=dataset_id, name="Approved Feedback Examples", version=ver,
            data_file=out_path, sha=sha, license_name="mit",
            source_kind="approved_feedback", source_location=str(feedback.path),
        )

    def update_stage_report(self, dataset_id: str, version: int, stage: str, report: dict):
        with self._lock:
            rec = self._datasets.get(dataset_id)
            if not rec:
                return
            ver = rec["versions"].get(str(version))
            if ver is not None:
                ver[stage] = report
                self._save()

    def list_datasets(self) -> list[dict]:
        with self._lock:
            return list(self._datasets.values())

    def get_version(self, dataset_id: str, version: int | None = None) -> dict | None:
        with self._lock:
            rec = self._datasets.get(dataset_id)
            if not rec:
                return None
            v = version or rec.get("latest_version")
            return rec.get("versions", {}).get(str(v))

