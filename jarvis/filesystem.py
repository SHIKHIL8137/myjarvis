"""Security-hardened local filesystem search, project inspection, and allowlisted document reader/writer."""
from dataclasses import asdict, dataclass
import os
from pathlib import Path

from .config import ROOT, FilesystemSpec

_SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "secrets.json", "credentials.json", "id_rsa", "id_ed25519",
    ".npmrc", ".pypirc", ".netrc", "authorized_keys",
}

_TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".html", ".css", ".csv", ".log", ".ps1", ".sh", ".sql",
}


class FilesystemPermissionError(PermissionError):
    """Raised when an operation targets a path outside allowlisted roots or inside a restricted path."""


@dataclass
class FileMatch:
    name: str
    path: str
    root: str
    ext: str
    size: int
    is_dir: bool = False
    snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SafeFilesystem:
    """Allowlisted local filesystem inspector & bounded reader/writer."""

    def __init__(self, spec: FilesystemSpec | None = None, default_base: Path = ROOT):
        self.spec = spec or FilesystemSpec()
        self.default_base = Path(default_base).resolve()

    def allowed_roots(self) -> list[Path]:
        roots = []
        for raw in self.spec.allowed_roots:
            p = Path(raw)
            if not p.is_absolute():
                p = self.default_base / p
            try:
                rp = p.resolve()
                if rp.exists():
                    roots.append(rp)
            except OSError:
                pass
        return roots

    def _is_excluded_name(self, name: str) -> bool:
        low = name.lower()
        if low in _SENSITIVE_FILENAMES or low.startswith(".env."):
            return True
        return name in set(self.spec.excluded_dirs) or low in {d.lower() for d in self.spec.excluded_dirs}

    def resolve_safe(self, raw_path: str, must_exist: bool = True) -> Path:
        if not raw_path or "\x00" in raw_path:
            raise FilesystemPermissionError("Invalid empty or malformed path")
        roots = self.allowed_roots()
        if not roots:
            raise FilesystemPermissionError("No valid allowlisted roots exist")

        cand = Path(raw_path)
        candidates = [cand] if cand.is_absolute() else [r / cand for r in roots] + [self.default_base / cand]
        for c in candidates:
            try:
                resolved = c.resolve()
            except OSError:
                continue
            for root in roots:
                if resolved == root or resolved.is_relative_to(root):
                    rel = resolved.relative_to(root)
                    if any(self._is_excluded_name(part) for part in rel.parts):
                        raise FilesystemPermissionError(f"Access denied to excluded or sensitive path: {rel}")
                    if must_exist and not resolved.exists():
                        continue
                    return resolved
        raise FilesystemPermissionError(f"Path outside allowlisted directories or missing: {raw_path!r}")

    def search(
        self,
        query: str = "",
        *,
        ext: str | None = None,
        folder: str | None = None,
        content_query: str | None = None,
        folders_only: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Search allowlisted roots by filename, folder name, extension, or file content."""
        max_hits = min(limit or self.spec.max_results, self.spec.max_results)
        q_low = (query or "").strip().lower()
        c_low = (content_query or "").strip().lower()
        f_low = (folder or "").strip().lower()
        ext_norm = ("." + ext.strip().lstrip(".").lower()) if ext else None

        hits: list[FileMatch] = []
        for root in self.allowed_roots():
            for cur, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not self._is_excluded_name(d))
                cur_p = Path(cur)
                if f_low and f_low not in cur_p.name.lower() and not q_low:
                    pass
                if folders_only or (not ext_norm and not c_low):
                    for d in dirs:
                        dp = cur_p / d
                        if (q_low and q_low in d.lower()) or (f_low and f_low in d.lower()):
                            hits.append(FileMatch(d, str(dp), str(root), "", 0, is_dir=True))
                            if len(hits) >= max_hits:
                                return [h.to_dict() for h in hits]
                if folders_only:
                    continue
                for fname in sorted(files):
                    if self._is_excluded_name(fname):
                        continue
                    fp = cur_p / fname
                    f_ext = fp.suffix.lower()
                    if ext_norm and f_ext != ext_norm:
                        continue
                    if f_low and f_low not in str(fp.relative_to(root)).lower():
                        continue
                    try:
                        size = fp.stat().st_size
                    except OSError:
                        continue

                    snippet = ""
                    matched = False
                    if q_low and (q_low in fname.lower() or q_low in str(fp.relative_to(root)).lower()):
                        matched = True
                    if c_low and size <= self.spec.max_file_bytes and f_ext in _TEXT_EXTENSIONS:
                        try:
                            raw = fp.read_bytes()
                            if b"\x00" not in raw[:512]:
                                text = raw.decode("utf-8", errors="replace")
                                idx = text.lower().find(c_low)
                                if idx >= 0:
                                    start = max(0, idx - 60)
                                    snippet = text[start : idx + len(c_low) + 120].replace("\n", " ").strip()
                                    matched = True
                        except OSError:
                            pass
                    if not q_low and not c_low:
                        matched = True

                    if matched:
                        hits.append(FileMatch(fname, str(fp), str(root), f_ext, size, is_dir=False, snippet=snippet))
                        if len(hits) >= max_hits:
                            return [h.to_dict() for h in hits]
        return [h.to_dict() for h in hits]

    def read_document(self, raw_path: str, max_chars: int = 8000) -> dict:
        fp = self.resolve_safe(raw_path, must_exist=True)
        if fp.is_dir():
            raise ValueError(f"{fp} is a directory, not a file")
        size = fp.stat().st_size
        if size > self.spec.max_file_bytes:
            raise ValueError(f"File exceeds configured max_file_bytes ({size} > {self.spec.max_file_bytes})")
        raw = fp.read_bytes()
        if b"\x00" in raw[:512]:
            raise ValueError(f"Binary file format cannot be read as plain text: {fp.suffix}")
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > max_chars
        return {
            "name": fp.name,
            "path": str(fp),
            "ext": fp.suffix.lower(),
            "size": size,
            "truncated": truncated,
            "content": text[:max_chars],
        }

    def inspect_project(self, raw_path: str | None = None) -> dict:
        target = self.resolve_safe(raw_path, must_exist=True) if raw_path else (self.allowed_roots()[0] if self.allowed_roots() else self.default_base)
        if target.is_file():
            target = target.parent
        ext_counts: dict[str, int] = {}
        manifests = []
        top_entries = sorted(p.name for p in target.iterdir() if not self._is_excluded_name(p.name))
        total_files = 0
        for cur, dirs, files in os.walk(target):
            dirs[:] = sorted(d for d in dirs if not self._is_excluded_name(d))
            for fn in files:
                if self._is_excluded_name(fn):
                    continue
                total_files += 1
                ext = Path(fn).suffix.lower() or "(none)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                if fn.lower() in ("pyproject.toml", "package.json", "requirements.txt", "config.json", "readme.md", "dockerfile"):
                    manifests.append(str(Path(cur).relative_to(target) / fn if Path(cur) != target else fn))
        return {
            "root": str(target),
            "total_files": total_files,
            "top_entries": top_entries[:30],
            "manifests": sorted(manifests)[:20],
            "languages": dict(sorted(ext_counts.items(), key=lambda x: -x[1])[:12]),
        }

    def create_or_modify(self, raw_path: str, content: str, *, overwrite: bool = False) -> dict:
        if not self.spec.allow_write:
            raise FilesystemPermissionError("Filesystem writes are disabled in settings")
        fp = self.resolve_safe(raw_path, must_exist=False)
        existed = fp.exists()
        if existed and not overwrite:
            raise FileExistsError(f"File already exists: {fp}")
        if len(content.encode("utf-8")) > self.spec.max_file_bytes:
            raise ValueError("Content exceeds max_file_bytes")
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        import hashlib

        written_sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
        os.replace(tmp, fp)
        return {"path": str(fp), "created": not existed, "bytes": fp.stat().st_size, "sha256": written_sha}

    def move_file(self, src_raw: str, dst_raw: str) -> dict:
        if not self.spec.allow_write:
            raise FilesystemPermissionError("Filesystem writes are disabled in settings")
        src = self.resolve_safe(src_raw, must_exist=True)
        dst = self.resolve_safe(dst_raw, must_exist=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return {"from": str(src), "to": str(dst)}

    def delete_file(self, raw_path: str) -> dict:
        """Move file safely into allowlisted root `.trash/` instead of irreversible hard delete."""
        if not self.spec.allow_write:
            raise FilesystemPermissionError("Filesystem writes are disabled in settings")
        fp = self.resolve_safe(raw_path, must_exist=True)
        if fp.is_dir():
            raise FilesystemPermissionError("Directory deletion is not permitted")
        root = next(r for r in self.allowed_roots() if fp.is_relative_to(r))
        trash = root / ".trash"
        trash.mkdir(exist_ok=True)
        import time
        dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{fp.name}"
        os.replace(fp, dest)
        return {"deleted": str(fp), "moved_to": str(dest)}

