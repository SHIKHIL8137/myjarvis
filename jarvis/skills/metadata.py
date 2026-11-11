"""Canonical provider-independent Skill manifest schema, safe frontmatter parser, & checksum utilities."""
import hashlib
from pathlib import Path
import re
import time
from pydantic import BaseModel, Field

_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def normalize_skill_id(raw_id: str) -> str:
    slug = _SLUG_RE.sub("-", (raw_id or "").strip().lower()).strip("-")
    if len(slug) < 2:
        raise ValueError(f"Invalid skill_id {raw_id!r}: must normalize to at least 2 alphanumeric/hyphen chars.")
    return slug[:120]


def parse_skill_markdown(text: str) -> tuple[dict, str]:
    """Parse YAML-style frontmatter without unsafe code execution (`yaml.unsafe_load` is never used)."""
    raw = text or ""
    lines = raw.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}, raw
    end_idx = -1
    for i in range(1, min(len(lines), 160)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, raw

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).strip()
    meta: dict = {}
    try:
        import yaml  # optional PyYAML safe_load if present

        parsed = yaml.safe_load("\n".join(fm_lines))
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        cur_key = ""
        cur_indent = 0
        for ln in fm_lines:
            if not ln.strip() or ln.strip().startswith("#"):
                continue
            indent = len(ln) - len(ln.lstrip(" "))
            if ":" in ln and indent == 0:
                k, _, v = ln.partition(":")
                cur_key = k.strip()
                cur_indent = indent
                val = v.strip().strip("\"'")
                if val in (">", "|"):
                    meta[cur_key] = ""
                else:
                    meta[cur_key] = val
            elif cur_key and indent > cur_indent:
                piece = ln.strip()
                if isinstance(meta.get(cur_key), str):
                    meta[cur_key] = f"{meta[cur_key]} {piece}".strip()
    return meta, body


def compute_directory_sha256(root: Path) -> str:
    """Compute deterministic SHA-256 over relative paths & file contents inside `root`."""
    base = Path(root).resolve()
    h = hashlib.sha256()
    files = sorted(
        (p for p in base.rglob("*") if p.is_file() and ".git" not in p.parts),
        key=lambda p: p.relative_to(base).as_posix(),
    )
    for fp in files:
        rel = fp.relative_to(base).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with fp.open("rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
        h.update(b"\x00")
    return h.hexdigest()


class SkillManifest(BaseModel):
    skill_id: str
    name: str
    description: str = ""
    category: str = "general"
    version: str = "1.0.0"
    source_repo: str = ""
    source_commit: str = ""
    content_sha256: str = ""
    license: str = "unknown"
    supported_runtimes: list[str] = Field(default_factory=lambda: ["jarvis_native"])
    compatible_roles: list[str] = Field(default_factory=lambda: ["coder_agent", "assistant_coordinator"])
    required_dependencies: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    network_requirements: list[str] = Field(default_factory=list)
    execution_mode: str = Field("instruction_only", pattern="^(instruction_only|script_assisted|sandboxed_tool)$")
    approval_required: bool = True
    installed_at: float = Field(default_factory=lambda: round(time.time(), 3))
    updated_at: float = Field(default_factory=lambda: round(time.time(), 3))
    enabled: bool = True
    verification_status: str = "pending"
    install_path: str = ""
    last_used_at: float | None = None
    keywords: list[str] = Field(default_factory=list)
    security_report: dict = Field(default_factory=dict)
    runtime_compat: dict = Field(default_factory=dict)

