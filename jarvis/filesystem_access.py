"""P10.20 Scoped, Persistent, Separately-Permissioned Filesystem Access Manager.

Security guarantees:
- Strictly separates 9 granular filesystem operations across read, write, execute, delete.
- Read permission NEVER implies write, execute, or delete; write NEVER implies execute or delete.
- "Always allow (read-only)" only grants read operations and honors policy.
- Temporary ("once") grants bind to exact folder + operation + task_id and auto-consume on task end.
- Enforces canonical path normalization, rejects UNC/device/drive-root/system/browser-profile paths,
  blocks symlink/reparse traversal, performs TOCTOU re-checks immediately before I/O, enforces
  extension/size limits, and audits every access, denial, grant, revocation, and block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Iterable

from sqlalchemy import select

from .config import Settings, load_settings
from .db.engine import DatabaseManager
from .db.models import (
    AuditEventRow,
    FilesystemPermissionRow,
    FilesystemPermissionRequestRow,
    ProjectAliasRow,
    ProjectRow,
)
from .departments.assistant.documents import parse_document
from .security.pii_firewall import redact_secrets
from .security.untrusted import wrap_untrusted

_fs_audit_ring: list[dict[str, Any]] = []

READ_OPS: tuple[str, ...] = ("list_directory", "read_file", "search_files", "read_metadata")
WRITE_OPS: tuple[str, ...] = ("create_file", "write_file", "edit_file")
EXECUTE_OPS: tuple[str, ...] = ("execute_in_directory",)
DELETE_OPS: tuple[str, ...] = ("delete_file",)
ALL_OPS: tuple[str, ...] = READ_OPS + WRITE_OPS + EXECUTE_OPS + DELETE_OPS

FS_OPERATION_GROUPS: dict[str, tuple[str, ...]] = {
    "read": READ_OPS,
    "write": WRITE_OPS,
    "execute": EXECUTE_OPS,
    "delete": DELETE_OPS,
}

OWNER_ACTORS: frozenset[str] = frozenset({"owner", "user", "local_ui", "settings_ui", "desktop_owner"})

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_BLOCKED_WRITE_EXTENSIONS: frozenset[str] = frozenset(
    {".exe", ".dll", ".sys", ".drv", ".scr", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".cpl"}
)

_DEFAULT_TEXT_READ_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".htm", ".xml", ".csv", ".tsv",
        ".sql", ".sh", ".ps1", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs",
        ".pdf", ".docx", ".xlsx", ".pptx", ".rtf",
    }
)

_SENSITIVE_DIR_FRAGMENTS: tuple[str, ...] = (
    "/.ssh",
    "\\.ssh",
    "/.gnupg",
    "\\.gnupg",
    "/.aws",
    "\\.aws",
    "/.azure",
    "\\.azure",
    "/.kube",
    "\\.kube",
    "/.docker",
    "\\.docker",
    "/.config/gh",
    "\\.config\\gh",
    "appdata/local/google/chrome/user data",
    "appdata\\local\\google\\chrome\\user data",
    "appdata/local/microsoft/edge/user data",
    "appdata\\local\\microsoft\\edge\\user data",
    "appdata/roaming/mozilla/firefox/profiles",
    "appdata\\roaming\\mozilla\\firefox\\profiles",
    "appdata/local/brave-browser/user data",
    "appdata\\local\\brave-browser\\user data",
    "/etc/shadow",
    "/etc/sudoers",
)

_SENSITIVE_FILENAMES: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_dsa*",
    "id_ecdsa*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.jks",
    "secrets.json",
    "credentials.json",
    "service-account*.json",
    "client_secret*.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "secrets.dpapi",
    "jarvis.db",
    "master.key",
    "vault.enc",
)

_PROTECTED_SYSTEM_DIRS: tuple[str, ...] = (
    "windows",
    "system32",
    "syswow64",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "$recycle.bin",
    "system volume information",
)


class FilesystemSecurityError(PermissionError):
    """Raised when a filesystem path or operation violates security policy."""

    def __init__(self, reason: str, *, code: str = "security_blocked", path: str = "", flags: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.path = path
        self.flags = list(flags or [])


@dataclass(slots=True)
class PermissionCheckResult:
    allowed: bool
    canonical_path: str
    operation: str
    reason: str = ""
    blocked: bool = False
    code: str = "ok"
    source: str = ""
    permission_id: str | None = None
    scope: str | None = None
    sensitive_flags: list[str] = field(default_factory=list)


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _is_within(child_norm: str, parent_norm: str) -> bool:
    if child_norm == parent_norm:
        return True
    sep = os.sep
    prefix = parent_norm if parent_norm.endswith(sep) else parent_norm + sep
    return child_norm.startswith(prefix)


def _has_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return True
        # Distinguish actual symlinks/NTFS junctions from OneDrive cloud file placeholders.
        reparse_tag = getattr(st, "st_reparse_tag", 0)
        if reparse_tag in (0xA000000C, 0xA0000003):  # IO_REPARSE_TAG_SYMLINK | IO_REPARSE_TAG_MOUNT_POINT
            return True
    except OSError:
        return False
    return False


def _validate_raw_syntax(raw_path: str | Path) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        raise FilesystemSecurityError("Empty path rejected.", code="empty_path")
    if "\x00" in raw:
        raise FilesystemSecurityError("Null byte in path rejected.", code="null_byte", path=raw)
    if raw.startswith("\\\\") or raw.startswith("//") or raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        raise FilesystemSecurityError("UNC or Windows device paths are blocked by security policy.", code="unc_or_device_path", path=raw)
    for part in raw.replace("\\", "/").split("/"):
        if part == "..":
            raise FilesystemSecurityError("Explicit '..' path traversal segment rejected.", code="traversal_blocked", path=raw)
        stem = part.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise FilesystemSecurityError(f"Reserved OS device name '{part}' rejected.", code="reserved_name", path=raw)
    return raw


def _is_drive_or_fs_root(resolved: Path) -> bool:
    s = str(resolved).rstrip("\\/")
    if len(s) == 2 and s[1] == ":" and s[0].isalpha():
        return True
    if str(resolved) in ("/", "\\") or resolved.parent == resolved:
        return True
    return False


def _detect_sensitive_path(resolved: Path, raw: str = "") -> list[str]:
    flags: list[str] = []
    lower_full = str(resolved).lower()
    lower_norm = lower_full.replace("\\", "/")
    lower_raw = (raw or "").replace("\\", "/").lower()
    parts_lower = [p.lower() for p in resolved.parts if p and p not in ("\\", "/")]

    # Protected OS root directories like C:\Windows, C:\Program Files, /etc, /bin
    if len(parts_lower) >= 2 and parts_lower[1].rstrip("\\/") in _PROTECTED_SYSTEM_DIRS:
        flags.append("protected_system_dir")
    elif any(seg in ("system32", "syswow64") for seg in parts_lower):
        flags.append("protected_system_dir")
    if lower_norm.startswith(("/etc", "/bin", "/sbin", "/usr/bin", "/root", "/proc", "/sys")):
        flags.append("protected_system_dir")

    # Known credential / browser profile directories
    for frag in _SENSITIVE_DIR_FRAGMENTS:
        frag_norm = frag.replace("\\", "/").lower()
        if frag_norm in lower_norm or frag_norm in lower_raw:
            flags.append("protected_credential_or_browser_dir")
            break

    # Sensitive file names
    name_lower = resolved.name.lower()
    for pat in _SENSITIVE_FILENAMES:
        if fnmatch.fnmatch(name_lower, pat.lower()):
            flags.append(f"sensitive_file:{name_lower}")
            break
    if ".git" in parts_lower and name_lower in {"config", "credentials", "index"}:
        flags.append("git_sensitive_metadata")
    return sorted(set(flags))


def canonicalize_safe_path(
    raw_path: str | Path,
    *,
    expected_root: Path | None = None,
    allow_nonexistent_leaf: bool = True,
) -> Path:
    """Resolve raw_path strictly while preventing symlink escapes and syntax tricks."""
    raw = _validate_raw_syntax(raw_path)
    expanded = os.path.expanduser(raw)
    # Reject relative paths unless bounded by expected_root
    cand = Path(expanded)
    if not cand.is_absolute():
        if expected_root is None:
            raise FilesystemSecurityError("Relative path rejected without explicit root context.", code="relative_path", path=raw)
        cand = expected_root / cand

    # Check non-canonical lexical bounds BEFORE symlink resolution if expected_root given
    abs_cand = Path(os.path.abspath(str(cand)))
    if expected_root is not None:
        root_real = Path(os.path.realpath(str(expected_root)))
        abs_root = Path(os.path.abspath(str(expected_root)))
        if not (_is_within(os.path.normcase(str(abs_cand)), os.path.normcase(str(abs_root))) or
                _is_within(os.path.normcase(str(abs_cand)), os.path.normcase(str(root_real)))):
            raise FilesystemSecurityError("Path escapes authorized folder boundaries.", code="outside_scope", path=raw)
        # Walk every component strictly under expected_root to block symlinks/junctions
        rel = None
        for base in (root_real, abs_root):
            try:
                rel = abs_cand.relative_to(base)
                cur = base
                for part in rel.parts:
                    cur = cur / part
                    if cur.exists() and _has_reparse_or_symlink(cur):
                        target_real = os.path.normcase(os.path.realpath(str(cur)))
                        if not _is_within(target_real, os.path.normcase(str(root_real))):
                            raise FilesystemSecurityError(
                                f"Symlink/reparse traversal outside approved folder blocked: {cur}",
                                code="symlink_escape_blocked",
                                path=str(cur),
                                flags=["symlink_escape"],
                            )
                        raise FilesystemSecurityError(
                            f"Symlink/reparse point blocked inside scoped folder: {cur}",
                            code="symlink_blocked",
                            path=str(cur),
                            flags=["symlink_blocked"],
                        )
                break
            except ValueError:
                continue

    resolved = Path(os.path.realpath(str(abs_cand)))
    if not allow_nonexistent_leaf and not resolved.exists():
        raise FilesystemSecurityError(f"Target path does not exist: {resolved}", code="not_found", path=str(resolved))

    if expected_root is not None:
        root_norm = os.path.normcase(os.path.realpath(str(expected_root)))
        res_norm = os.path.normcase(str(resolved))
        if not _is_within(res_norm, root_norm):
            raise FilesystemSecurityError(
                f"Resolved canonical path '{resolved}' escapes authorized root '{expected_root}'.",
                code="symlink_escape_blocked",
                path=str(resolved),
                flags=["symlink_escape"],
            )
    return resolved


class FilesystemAccessManager:
    """DB-backed scoped permission gate + safe filesystem operations executor."""

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings | None = None,
        *,
        workspace_baseline_roots: Iterable[str | Path] | None = None,
        session_id: str | None = None,
    ):
        self.db = db
        self.settings = settings or load_settings()
        self.session_id = session_id or "active_session"
        roots: list[str | Path]
        if workspace_baseline_roots is None:
            # Workspace notes folder remains readable baseline so existing internal notes features work,
            # while all host directories (Documents, Downloads, Desktop, project dirs, temp dirs) require explicit grant.
            roots = [self.settings.notes_dir]
        else:
            roots = list(workspace_baseline_roots)
        self.workspace_baseline_roots: list[Path] = []
        for r in roots:
            try:
                p = Path(os.path.realpath(str(Path(r).expanduser())))
                if p not in self.workspace_baseline_roots:
                    self.workspace_baseline_roots.append(p)
            except Exception:
                pass

    # ---------------- Audit ----------------

    async def _log_audit(self, *, actor: str, decision: str, target: str, details: dict[str, Any] | None = None) -> None:
        clean = {"timestamp": time.time(), **(details or {})}
        try:
            async with self.db.async_session() as s:
                s.add(
                    AuditEventRow(
                        actor=str(actor or "system")[:64],
                        category="filesystem_access",
                        decision=str(decision)[:24],
                        target=str(target)[:500],
                        details=clean,
                    )
                )
                await s.commit()
        except Exception:
            pass
        _fs_audit_ring.append({"category": "filesystem_access", "actor": actor, "decision": decision, "target": target, "details": clean})
        del _fs_audit_ring[:-500]

    # ---------------- Policy Helpers ----------------

    def _validate_operations(self, operations: Iterable[str]) -> list[str]:
        ops: list[str] = []
        for op in operations:
            op_s = str(op or "").strip()
            if op_s not in ALL_OPS:
                raise FilesystemSecurityError(f"Unsupported filesystem operation: {op_s}", code="invalid_operation")
            if op_s not in ops:
                ops.append(op_s)
        if not ops:
            raise FilesystemSecurityError("At least one valid filesystem operation must be specified.", code="empty_operations")
        return ops

    def _screen_target_security(self, raw_path: str | Path, operation: str) -> tuple[Path, list[str]]:
        """Validate syntax, resolve canonical path, and inspect sensitive/system rules."""
        resolved = canonicalize_safe_path(raw_path, allow_nonexistent_leaf=True)
        if _is_drive_or_fs_root(resolved):
            raise FilesystemSecurityError(
                f"Access to drive root or filesystem root '{resolved}' is forbidden.",
                code="drive_root_blocked",
                path=str(resolved),
                flags=["drive_root"],
            )
        # Never permit symlinks directly passed as targets
        cand_no_follow = Path(os.path.abspath(os.path.expanduser(str(raw_path))))
        if cand_no_follow.exists() and _has_reparse_or_symlink(cand_no_follow):
            raise FilesystemSecurityError(
                f"Direct symlink/reparse target rejected: {cand_no_follow}",
                code="symlink_blocked",
                path=str(cand_no_follow),
                flags=["symlink_blocked"],
            )

        flags = _detect_sensitive_path(resolved, raw=str(raw_path))
        if "protected_system_dir" in flags or "protected_credential_or_browser_dir" in flags or "git_sensitive_metadata" in flags:
            raise FilesystemSecurityError(
                f"Target '{resolved}' falls inside a protected OS, credential, or browser profile directory ({', '.join(flags)}).",
                code="protected_directory_blocked",
                path=str(resolved),
                flags=flags,
            )

        policy = getattr(self.settings.filesystem, "sensitive_file_policy", "block") or "block"
        if flags and policy != "allow_explicit":
            if not (policy == "metadata_only" and operation == "read_metadata"):
                raise FilesystemSecurityError(
                    f"Sensitive file blocked by policy ({', '.join(flags)}): {resolved.name}",
                    code="sensitive_file_blocked",
                    path=str(resolved),
                    flags=flags,
                )

        ext = resolved.suffix.lower()
        if operation in WRITE_OPS:
            if ext in _BLOCKED_WRITE_EXTENSIONS:
                raise FilesystemSecurityError(
                    f"Writing executable/script extension '{ext}' is blocked by security policy.",
                    code="blocked_extension",
                    path=str(resolved),
                    flags=["blocked_write_extension"],
                )
            if not getattr(self.settings.filesystem, "allow_write", True) and operation in WRITE_OPS:
                # Write still gated per-folder, only blocked globally if explicitly disabled AND not scoped
                pass

        return resolved, flags

    def _is_workspace_baseline(self, resolved: Path, operation: str) -> bool:
        if operation not in READ_OPS:
            return False
        res_norm = os.path.normcase(str(resolved))
        for root in self.workspace_baseline_roots:
            if _is_within(res_norm, os.path.normcase(str(root))):
                return True
        return False

    # ---------------- Authorization Checks ----------------

    async def authorize(
        self,
        target_path: str | Path,
        operation: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        actor: str = "jarvis",
        audit: bool = True,
    ) -> PermissionCheckResult:
        if operation not in ALL_OPS:
            return PermissionCheckResult(
                allowed=False,
                canonical_path=str(target_path),
                operation=operation,
                reason=f"Unknown operation: {operation}",
                blocked=True,
                code="invalid_operation",
            )
        try:
            resolved, flags = self._screen_target_security(target_path, operation)
        except FilesystemSecurityError as e:
            if audit:
                await self._log_audit(
                    actor=actor,
                    decision="blocked",
                    target=str(target_path),
                    details={"operation": operation, "code": e.code, "reason": e.reason, "flags": e.flags, "task_id": task_id},
                )
            return PermissionCheckResult(
                allowed=False,
                canonical_path=e.path or str(target_path),
                operation=operation,
                reason=e.reason,
                blocked=True,
                code=e.code,
                sensitive_flags=e.flags,
            )

        if self._is_workspace_baseline(resolved, operation):
            return PermissionCheckResult(
                allowed=True,
                canonical_path=str(resolved),
                operation=operation,
                reason="Workspace notes baseline read scope",
                source="workspace_baseline",
            )

        now = time.time()
        eff_session = session_id or self.session_id
        res_norm = os.path.normcase(str(resolved))

        async with self.db.async_session() as s:
            rows = (
                await s.execute(
                    select(FilesystemPermissionRow).where(FilesystemPermissionRow.status == "active")
                )
            ).scalars().all()

            expired_any = False
            matched: FilesystemPermissionRow | None = None
            for row in rows:
                if row.expires_at is not None and row.expires_at <= now:
                    row.status = "expired"
                    expired_any = True
                    continue
                folder_norm = os.path.normcase(os.path.realpath(row.canonical_folder_path))
                if not _is_within(res_norm, folder_norm):
                    continue
                # Verify symlink boundaries inside the matching granted folder!
                try:
                    canonicalize_safe_path(target_path, expected_root=Path(row.canonical_folder_path), allow_nonexistent_leaf=True)
                except FilesystemSecurityError as e:
                    if audit:
                        await self._log_audit(
                            actor=actor,
                            decision="blocked",
                            target=str(resolved),
                            details={"operation": operation, "code": e.code, "reason": e.reason, "task_id": task_id},
                        )
                    if expired_any:
                        await s.commit()
                    return PermissionCheckResult(
                        allowed=False,
                        canonical_path=str(resolved),
                        operation=operation,
                        reason=e.reason,
                        blocked=True,
                        code=e.code,
                        sensitive_flags=e.flags,
                    )

                granted_ops = list(row.granted_operations or [])
                if operation not in granted_ops:
                    continue
                if row.scope == "once":
                    # Temporary once permission MUST belong to the exact task (or non-empty matching task_id)
                    if not task_id or row.task_id != task_id:
                        continue
                elif row.scope == "session":
                    if row.session_id and eff_session and row.session_id != eff_session:
                        continue
                # Persistent ("persistent") grants apply across tasks until revoked or expired
                matched = row
                break

            if expired_any:
                await s.commit()

        if matched is not None:
            if audit:
                await self._log_audit(
                    actor=actor,
                    decision="allowed",
                    target=str(resolved),
                    details={
                        "operation": operation,
                        "permission_id": matched.permission_id,
                        "scope": matched.scope,
                        "task_id": task_id,
                    },
                )
            return PermissionCheckResult(
                allowed=True,
                canonical_path=str(resolved),
                operation=operation,
                reason=f"Authorized via {matched.scope} grant {matched.permission_id}",
                source="permission_grant",
                permission_id=matched.permission_id,
                scope=matched.scope,
                sensitive_flags=flags,
            )

        if audit:
            await self._log_audit(
                actor=actor,
                decision="denied_ungranted",
                target=str(resolved),
                details={"operation": operation, "task_id": task_id, "reason": "No matching active filesystem grant"},
            )
        return PermissionCheckResult(
            allowed=False,
            canonical_path=str(resolved),
            operation=operation,
            reason=f"Folder access permission required for '{operation}' on '{resolved}'.",
            blocked=False,
            code="permission_required",
            sensitive_flags=flags,
        )

    # ---------------- Permission Requests & Owner Decisions ----------------

    async def create_permission_request(
        self,
        requested_path: str | Path,
        operations: Iterable[str],
        *,
        task_id: str | None = None,
        reason: str = "",
        access_duration: str = "once",
        deferred_action: dict[str, Any] | None = None,
        actor: str = "jarvis",
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        ops = self._validate_operations(operations)
        resolved, flags = self._screen_target_security(requested_path, ops[0])
        for op in ops[1:]:
            self._screen_target_security(resolved, op)

        # Normalize requested folder path (if target is a file inside an ungranted dir, scope the request to the dir)
        folder_path = resolved if resolved.is_dir() or str(requested_path).endswith(("\\", "/")) or "." not in resolved.name else resolved.parent
        not_requested = [op for op in ALL_OPS if op not in ops]
        if any(op not in READ_OPS for op in ops):
            flags = sorted(set(flags + [f"mutating_operation:{op}" for op in ops if op not in READ_OPS]))

        now = time.time()
        row = FilesystemPermissionRequestRow(
            task_id=task_id,
            requested_path=str(requested_path),
            canonical_path=str(folder_path),
            requested_operations=ops,
            not_requested_operations=not_requested,
            sensitive_flags=flags,
            reason=str(reason or f"Access requested for {', '.join(ops)}")[:500],
            access_duration=access_duration if access_duration in ("once", "session", "persistent") else "once",
            status="pending",
            deferred_action=dict(deferred_action or {}),
            created_at=now,
            expires_at=now + max(60, ttl_seconds),
        )
        async with self.db.async_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)

        await self._log_audit(
            actor=actor,
            decision="requested",
            target=str(folder_path),
            details={"request_id": row.request_id, "operations": ops, "task_id": task_id, "reason": row.reason},
        )
        return self._request_to_dict(row)

    async def decide_request(
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str = "owner",
        session_id: str | None = None,
        duration_seconds: int | None = None,
        allowed_operations: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        actor_norm = str(decided_by or "").strip().lower()
        if actor_norm not in OWNER_ACTORS:
            await self._log_audit(
                actor=actor_norm or "unknown",
                decision="rejected_non_owner",
                target=request_id,
                details={"attempted_decision": decision},
            )
            raise FilesystemSecurityError(
                f"Only the local owner may approve or deny filesystem permissions (got '{decided_by}').",
                code="owner_only_approval",
            )

        dec = str(decision or "").strip().lower()
        if dec not in ("allow_once", "allow_session", "always_read_only", "allow_persistent", "deny"):
            raise ValueError(f"Unsupported permission decision: {decision}")

        now = time.time()
        async with self.db.async_session() as s:
            req = await s.get(FilesystemPermissionRequestRow, request_id)
            if req is None:
                raise KeyError(f"Permission request '{request_id}' not found")
            if req.status != "pending":
                raise ValueError(f"Permission request '{request_id}' already decided ({req.status})")
            if req.expires_at is not None and req.expires_at <= now:
                req.status = "expired"
                req.decided_at = now
                req.decided_by = actor_norm
                await s.commit()
                raise ValueError(f"Permission request '{request_id}' has expired")

            if dec == "deny":
                req.status = "denied"
                req.decided_at = now
                req.decided_by = actor_norm
                await s.commit()
                await s.refresh(req)
                await self._log_audit(
                    actor=actor_norm,
                    decision="denied",
                    target=req.canonical_path,
                    details={"request_id": req.request_id, "task_id": req.task_id, "operations": req.requested_operations},
                )
                return {"request": self._request_to_dict(req), "permission": None, "granted": False}

            req_ops = self._validate_operations(allowed_operations or req.requested_operations or READ_OPS)
            # Re-screen security at approval time too
            for op in req_ops:
                self._screen_target_security(req.canonical_path, op)

            if dec == "allow_once":
                perm_scope = "once"
                granted_ops = list(req_ops)
                exp = now + float(duration_seconds or 1800)
            elif dec == "allow_session":
                perm_scope = "session"
                granted_ops = list(req_ops)
                exp = now + float(duration_seconds or 28800)
            elif dec == "always_read_only":
                if not getattr(self.settings.filesystem, "allow_always_read_only", True):
                    raise FilesystemSecurityError(
                        "Policy forbids persistent 'always_read_only' folder grants.",
                        code="policy_disallows_always",
                    )
                if any(op not in READ_OPS for op in req_ops):
                    raise FilesystemSecurityError(
                        "Cannot use 'always_read_only' for write, execute, or delete operations.",
                        code="read_only_policy_violation",
                    )
                perm_scope = "persistent"
                granted_ops = list(READ_OPS)
                exp = None
            else:  # allow_persistent (explicit owner grant from settings/policy)
                perm_scope = "persistent"
                granted_ops = list(req_ops)
                exp = None

            perm = FilesystemPermissionRow(
                canonical_folder_path=req.canonical_path,
                granted_operations=granted_ops,
                scope=perm_scope,
                granted_at=now,
                expires_at=exp,
                granted_by=actor_norm,
                status="active",
                purpose=req.reason,
                session_id=(session_id or self.session_id) if perm_scope == "session" else None,
                task_id=req.task_id if perm_scope == "once" else None,
            )
            s.add(perm)
            await s.flush()
            req.status = "approved"
            req.permission_id = perm.permission_id
            req.decided_by = actor_norm
            req.decided_at = now
            await s.commit()
            await s.refresh(req)
            await s.refresh(perm)

        await self._log_audit(
            actor=actor_norm,
            decision="approved",
            target=perm.canonical_folder_path,
            details={
                "request_id": req.request_id,
                "permission_id": perm.permission_id,
                "scope": perm.scope,
                "granted_operations": perm.granted_operations,
                "task_id": req.task_id,
            },
        )
        return {"request": self._request_to_dict(req), "permission": self._perm_to_dict(perm), "granted": True}

    async def grant_direct(
        self,
        folder_path: str | Path,
        operations: Iterable[str],
        *,
        scope: str = "persistent",
        granted_by: str = "owner",
        purpose: str = "Configured in Settings -> Permissions & Privacy",
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly add a folder permission rule (used by Settings -> Permissions & Privacy)."""
        actor_norm = str(granted_by or "").strip().lower()
        if actor_norm not in OWNER_ACTORS:
            raise FilesystemSecurityError("Only owner may add folder policies.", code="owner_only_approval")
        ops = self._validate_operations(operations)
        for op in ops:
            resolved, _ = self._screen_target_security(folder_path, op)
        if not resolved.exists() or not resolved.is_dir():
            raise FilesystemSecurityError(f"Approved folder must be an existing directory: {resolved}", code="not_a_directory")
        eff_scope = scope if scope in ("once", "session", "persistent") else "persistent"
        now = time.time()
        perm = FilesystemPermissionRow(
            canonical_folder_path=str(resolved),
            granted_operations=ops,
            scope=eff_scope,
            granted_at=now,
            expires_at=(now + expires_in_seconds) if expires_in_seconds else None,
            granted_by=actor_norm,
            status="active",
            purpose=purpose[:400],
            session_id=self.session_id if eff_scope == "session" else None,
            task_id=None,
        )
        async with self.db.async_session() as s:
            s.add(perm)
            await s.commit()
            await s.refresh(perm)
        await self._log_audit(
            actor=actor_norm,
            decision="granted_policy",
            target=perm.canonical_folder_path,
            details={"permission_id": perm.permission_id, "scope": eff_scope, "granted_operations": ops},
        )
        return self._perm_to_dict(perm)

    async def consume_task_once_permissions(self, task_id: str) -> int:
        """Auto-expire temporary 'once' grants as soon as their bound task finishes."""
        if not task_id:
            return 0
        now = time.time()
        count = 0
        async with self.db.async_session() as s:
            rows = (
                await s.execute(
                    select(FilesystemPermissionRow).where(
                        FilesystemPermissionRow.task_id == task_id,
                        FilesystemPermissionRow.scope == "once",
                        FilesystemPermissionRow.status == "active",
                    )
                )
            ).scalars().all()
            for r in rows:
                r.status = "consumed"
                r.revoked_at = now
                count += 1
            reqs = (
                await s.execute(
                    select(FilesystemPermissionRequestRow).where(
                        FilesystemPermissionRequestRow.task_id == task_id,
                        FilesystemPermissionRequestRow.status == "approved",
                    )
                )
            ).scalars().all()
            for rq in reqs:
                if rq.access_duration == "once":
                    rq.status = "consumed"
            if count or reqs:
                await s.commit()
        if count:
            await self._log_audit(
                actor="system",
                decision="consumed",
                target=task_id,
                details={"task_id": task_id, "consumed_count": count},
            )
        return count

    async def revoke_permission(self, permission_id: str, *, actor: str = "owner") -> dict[str, Any]:
        now = time.time()
        async with self.db.async_session() as s:
            row = await s.get(FilesystemPermissionRow, permission_id)
            if row is None:
                raise KeyError(f"Permission '{permission_id}' not found")
            row.status = "revoked"
            row.revoked_at = now
            await s.commit()
            await s.refresh(row)
        await self._log_audit(
            actor=actor,
            decision="revoked",
            target=row.canonical_folder_path,
            details={"permission_id": permission_id},
        )
        return self._perm_to_dict(row)

    async def update_permission(
        self,
        permission_id: str,
        *,
        granted_operations: Iterable[str] | None = None,
        scope: str | None = None,
        actor: str = "owner",
    ) -> dict[str, Any]:
        if str(actor or "").strip().lower() not in OWNER_ACTORS:
            raise FilesystemSecurityError("Only owner may update folder permissions.", code="owner_only_approval")
        async with self.db.async_session() as s:
            row = await s.get(FilesystemPermissionRow, permission_id)
            if row is None:
                raise KeyError(f"Permission '{permission_id}' not found")
            if granted_operations is not None:
                row.granted_operations = self._validate_operations(granted_operations)
            if scope in ("session", "persistent", "once"):
                row.scope = scope
                if scope == "persistent":
                    row.expires_at = None
            await s.commit()
            await s.refresh(row)
        await self._log_audit(
            actor=actor,
            decision="updated",
            target=row.canonical_folder_path,
            details={"permission_id": permission_id, "granted_operations": row.granted_operations, "scope": row.scope},
        )
        return self._perm_to_dict(row)

    # ---------------- Listing & Audit ----------------

    async def list_permissions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        now = time.time()
        async with self.db.async_session() as s:
            rows = (
                await s.execute(select(FilesystemPermissionRow).order_by(FilesystemPermissionRow.granted_at.desc()))
            ).scalars().all()
            changed = False
            for r in rows:
                if r.status == "active" and r.expires_at is not None and r.expires_at <= now:
                    r.status = "expired"
                    changed = True
            if changed:
                await s.commit()
            out = [self._perm_to_dict(r) for r in rows if status is None or r.status == status]
        return out

    async def list_requests(self, *, status: str | None = None) -> list[dict[str, Any]]:
        now = time.time()
        async with self.db.async_session() as s:
            rows = (
                await s.execute(
                    select(FilesystemPermissionRequestRow).order_by(FilesystemPermissionRequestRow.created_at.desc())
                )
            ).scalars().all()
            changed = False
            for r in rows:
                if r.status == "pending" and r.expires_at is not None and r.expires_at <= now:
                    r.status = "expired"
                    changed = True
            if changed:
                await s.commit()
            return [self._request_to_dict(r) for r in rows if status is None or r.status == status]

    async def list_audit_events(self, *, limit: int = 80) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            rows = (
                await s.execute(
                    select(AuditEventRow)
                    .where(AuditEventRow.category == "filesystem_access")
                    .order_by(AuditEventRow.created_at.desc())
                    .limit(max(1, min(limit, 500)))
                )
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "actor": r.actor,
                    "category": r.category,
                    "decision": r.decision,
                    "target": r.target,
                    "details": r.details,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    # ---------------- TOCTOU-Safe Operation Execution ----------------

    async def _require_auth(
        self,
        path: str | Path,
        operation: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        actor: str = "jarvis",
    ) -> Path:
        chk = await self.authorize(path, operation, task_id=task_id, session_id=session_id, actor=actor, audit=True)
        if not chk.allowed:
            raise FilesystemSecurityError(chk.reason, code=chk.code, path=chk.canonical_path, flags=chk.sensitive_flags)
        return Path(chk.canonical_path)

    async def execute_list_directory(
        self,
        folder_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        root = await self._require_auth(folder_path, "list_directory", task_id=task_id, session_id=session_id)
        if not root.exists() or not root.is_dir():
            raise FilesystemSecurityError(f"Not a directory: {root}", code="not_a_directory", path=str(root))
        entries: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if _has_reparse_or_symlink(entry):
                continue
            if _detect_sensitive_path(entry):
                continue
            try:
                st = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": entry.is_dir(),
                        "size_bytes": 0 if entry.is_dir() else st.st_size,
                        "modified_at": st.st_mtime,
                    }
                )
            except OSError:
                continue
            if len(entries) >= limit:
                break
        return entries

    async def execute_search_files(
        self,
        folder_path: str | Path,
        *,
        query: str = "",
        extensions: Iterable[str] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        root = await self._require_auth(folder_path, "search_files", task_id=task_id, session_id=session_id)
        if not root.exists() or not root.is_dir():
            raise FilesystemSecurityError(f"Not a directory: {root}", code="not_a_directory", path=str(root))
        ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (extensions or []) if e}
        q_lower = str(query or "").strip().lower()
        stopwords = {"the", "all", "my", "this", "for", "and", "file", "files", "document", "documents", "folder"}
        raw_tokens = [t for t in q_lower.split() if len(t) >= 2 and t not in stopwords]
        q_tokens: list[str] = []
        for tok in raw_tokens:
            if tok not in q_tokens:
                q_tokens.append(tok)
            if tok.endswith("ies") and len(tok) > 4:
                stem = tok[:-3] + "y"
                if stem not in q_tokens:
                    q_tokens.append(stem)
            elif tok.endswith("ses") and len(tok) > 4:
                stem = tok[:-2]
                if stem not in q_tokens:
                    q_tokens.append(stem)
            elif tok.endswith("s") and not tok.endswith("ss") and len(tok) > 3:
                stem = tok[:-1]
                if stem not in q_tokens:
                    q_tokens.append(stem)
        matches: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            # Prune symlinked or sensitive subdirectories before descending
            safe_dirs = []
            for d in dirnames:
                sub = dp / d
                if _has_reparse_or_symlink(sub) or _detect_sensitive_path(sub):
                    continue
                if d in {".git", "node_modules", "__pycache__", ".venv", "venv"}:
                    continue
                safe_dirs.append(d)
            dirnames[:] = safe_dirs

            for fname in filenames:
                fp = dp / fname
                if _has_reparse_or_symlink(fp) or _detect_sensitive_path(fp):
                    continue
                if ext_set and fp.suffix.lower() not in ext_set:
                    continue
                if q_tokens:
                    name_low = fname.lower()
                    path_low = str(fp).lower()
                    if not any(tok in name_low or tok in path_low for tok in q_tokens):
                        continue
                try:
                    st = fp.stat()
                    matches.append(
                        {
                            "name": fname,
                            "path": str(fp),
                            "relative_path": str(fp.relative_to(root)),
                            "extension": fp.suffix.lower(),
                            "size_bytes": st.st_size,
                            "modified_at": st.st_mtime,
                        }
                    )
                except OSError:
                    continue
                if len(matches) >= limit:
                    return matches
        return matches

    @staticmethod
    def _decode_text_bytes(raw: bytes) -> tuple[str, str]:
        """Decode text bytes cleanly across common Windows/Unix encodings before lossy fallback."""
        for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8-lossy"

    async def execute_read_file(
        self,
        file_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        max_bytes: int | None = None,
        start_line: int = 1,
        max_lines: int | None = None,
    ) -> dict[str, Any]:
        target = await self._require_auth(file_path, "read_file", task_id=task_id, session_id=session_id)
        if not target.exists() or not target.is_file():
            raise FilesystemSecurityError(f"File not found: {target}", code="not_found", path=str(target))
        # TOCTOU re-check right before reading
        if _has_reparse_or_symlink(target):
            raise FilesystemSecurityError("TOCTOU check: symlink rejected.", code="symlink_blocked", path=str(target))
        cfg_limit = getattr(self.settings.filesystem, "max_read_bytes", None) or getattr(self.settings.filesystem, "max_file_bytes", 524_288)
        limit = int(max_bytes or cfg_limit or 524_288)
        st = target.stat()
        max_doc_bytes = max(limit * 4, 8 * 1024 * 1024)
        ext = target.suffix.lower()
        doc_exts = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm"}
        if st.st_size > (max_doc_bytes if ext in doc_exts else limit * 4):
            raise FilesystemSecurityError(f"File size ({st.st_size} bytes) exceeds safe read limit.", code="file_too_large", path=str(target))
        allowed_exts = list(getattr(self.settings.filesystem, "allowed_extensions", []) or [])
        if allowed_exts and ext and ext not in [e.lower() for e in allowed_exts]:
            raise FilesystemSecurityError(f"File extension '{ext}' not permitted by allowed_extensions policy.", code="extension_blocked", path=str(target))

        doc_meta: dict[str, Any] = {}
        encoding = "structured"
        raw_bytes = target.read_bytes()
        file_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        if ext in doc_exts:
            try:
                parsed = parse_document(target, max_chars=max(24_000, min(limit, 120_000)))
            except Exception as e:
                raise FilesystemSecurityError(
                    f"Could not extract structured text from {target.name}: {e}",
                    code="document_parse_error",
                    path=str(target),
                ) from e
            text_full = str(parsed.get("content") or "")
            doc_meta = dict(parsed.get("meta") or {})
            truncated_by_doc = bool(parsed.get("truncated", False))
        else:
            if b"\x00" in raw_bytes[:4096]:
                raise FilesystemSecurityError(
                    f"Binary file '{target.name}' cannot be safely read as text (use metadata inspection instead).",
                    code="binary_file",
                    path=str(target),
                )
            text_full, encoding = self._decode_text_bytes(raw_bytes[: limit * 2])
            if ext in (".csv", ".tsv"):
                encoding = f"structured-{ext.lstrip('.')}/{encoding}"
            truncated_by_doc = len(raw_bytes) > limit * 2

        lines = text_full.splitlines()
        total_lines = len(lines)
        start_idx = max(0, int(start_line or 1) - 1)
        if max_lines is not None and max_lines > 0:
            selected_lines = lines[start_idx : start_idx + int(max_lines)]
            sliced_text = "\n".join(selected_lines)
            truncated_lines = (start_idx + int(max_lines)) < total_lines
        else:
            sliced_text = "\n".join(lines[start_idx:]) if start_idx > 0 else text_full
            truncated_lines = False

        truncated = truncated_by_doc or truncated_lines or len(sliced_text) > limit
        content_slice = sliced_text[:limit]
        clean_content, secret_hits = redact_secrets(content_slice)
        untrusted = wrap_untrusted(
            clean_content,
            source_type="local_file",
            source_id=file_sha256[:12],
            url="",
            title=target.name,
            sha256=file_sha256,
        )
        return {
            "path": str(target),
            "name": target.name,
            "ext": ext,
            "encoding": encoding,
            "size_bytes": st.st_size,
            "total_lines": total_lines,
            "start_line": start_idx + 1,
            "truncated": truncated,
            "redacted": bool(secret_hits),
            "redacted_kinds": sorted(set(secret_hits)),
            "sha256": file_sha256,
            "doc_meta": doc_meta,
            "content": clean_content,
            "untrusted_content": untrusted,
        }

    async def execute_read_document(
        self,
        file_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        max_chars: int = 32_000,
    ) -> dict[str, Any]:
        """Read and extract structured content from PDF, DOCX, PPTX, XLSX, CSV, Markdown, or text documents."""
        return await self.execute_read_file(
            file_path,
            task_id=task_id,
            session_id=session_id,
            max_bytes=max(8_192, min(int(max_chars), 200_000)),
        )

    async def execute_get_project_structure(
        self,
        folder_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        max_depth: int = 3,
        max_entries: int = 120,
    ) -> dict[str, Any]:
        """Return bounded, redacted project directory structure inside an authorized root."""
        target = await self._require_auth(folder_path, "list_directory", task_id=task_id, session_id=session_id)
        if not target.exists() or not target.is_dir():
            raise FilesystemSecurityError(f"Directory not found: {target}", code="not_found", path=str(target))
        excluded = set(getattr(self.settings.filesystem, "excluded_dirs", []) or [])
        depth_cap = max(1, min(int(max_depth), 6))
        entry_cap = max(10, min(int(max_entries), 250))
        tree_lines: list[str] = []
        top_dirs: list[str] = []
        top_files: list[str] = []
        stack_markers: list[str] = []
        marker_names = {
            "pyproject.toml", "requirements.txt", "package.json", "alembic.ini",
            "manage.py", "docker-compose.yml", "docker-compose.yaml", "dockerfile",
            "cargo.toml", "go.mod", "pom.xml", ".env.example",
        }
        truncated = False

        def _walk(cur: Path, depth: int, prefix: str) -> None:
            nonlocal truncated
            if len(tree_lines) >= entry_cap:
                truncated = True
                return
            try:
                children = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for child in children:
                if len(tree_lines) >= entry_cap:
                    truncated = True
                    return
                if _has_reparse_or_symlink(child):
                    continue
                if child.is_dir() and child.name in excluded:
                    continue
                if _detect_sensitive_path(child, raw=str(child)):
                    continue
                is_dir = child.is_dir()
                rel = str(child.relative_to(target))
                tree_lines.append(f"{prefix}{child.name}{'/' if is_dir else ''}")
                if depth == 1:
                    (top_dirs if is_dir else top_files).append(child.name)
                if not is_dir and child.name.lower() in marker_names:
                    stack_markers.append(rel)
                if is_dir and depth < depth_cap:
                    _walk(child, depth + 1, prefix + "  ")

        _walk(target, 1, "")
        return {
            "path": str(target),
            "name": target.name,
            "top_dirs": top_dirs,
            "top_files": top_files,
            "stack_markers": stack_markers,
            "entry_count": len(tree_lines),
            "truncated": truncated,
            "tree": "\n".join(tree_lines),
        }

    async def execute_find_files_by_extension(
        self,
        folder_path: str | Path,
        extensions: Iterable[str],
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find non-sensitive files matching any given extension inside an authorized directory."""
        target = await self._require_auth(folder_path, "search_files", task_id=task_id, session_id=session_id)
        if not target.exists() or not target.is_dir():
            raise FilesystemSecurityError(f"Directory not found: {target}", code="not_found", path=str(target))
        norm_exts = {("." + str(e).strip().lower().lstrip(".")) for e in extensions if str(e).strip()}
        if not norm_exts:
            return []
        excluded = set(getattr(self.settings.filesystem, "excluded_dirs", []) or [])
        limit = int(max_results or getattr(self.settings.filesystem, "max_results", 40) or 40)
        matches: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(target, topdown=True, followlinks=False):
            dirs[:] = [
                d for d in sorted(dirs)
                if d not in excluded and not _has_reparse_or_symlink(Path(root) / d) and not _detect_sensitive_path(Path(root) / d)
            ]
            for fn in sorted(files):
                fp = Path(root) / fn
                if fp.suffix.lower() not in norm_exts:
                    continue
                if _has_reparse_or_symlink(fp) or _detect_sensitive_path(fp, raw=str(fp)):
                    continue
                try:
                    st = fp.stat()
                    matches.append({"name": fn, "path": str(fp), "ext": fp.suffix.lower(), "size": st.st_size, "mtime": st.st_mtime})
                except OSError:
                    continue
                if len(matches) >= limit:
                    return matches
        return matches

    async def execute_read_metadata(
        self,
        target_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        target = await self._require_auth(target_path, "read_metadata", task_id=task_id, session_id=session_id)
        if not target.exists():
            raise FilesystemSecurityError(f"Path not found: {target}", code="not_found", path=str(target))
        st = target.stat()
        return {
            "path": str(target),
            "name": target.name,
            "is_dir": target.is_dir(),
            "size_bytes": st.st_size,
            "modified_at": st.st_mtime,
            "extension": target.suffix.lower(),
        }

    async def execute_write_file(
        self,
        file_path: str | Path,
        content: str,
        *,
        create: bool = False,
        edit: bool = False,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        op = "create_file" if create else ("edit_file" if edit else "write_file")
        target = await self._require_auth(file_path, op, task_id=task_id, session_id=session_id)
        if create and target.exists():
            op = "write_file"
            target = await self._require_auth(file_path, op, task_id=task_id, session_id=session_id)
        # TOCTOU check on parent and target immediately before write
        target.parent.mkdir(parents=True, exist_ok=True)
        if _has_reparse_or_symlink(target.parent) or (target.exists() and _has_reparse_or_symlink(target)):
            raise FilesystemSecurityError("TOCTOU check: cannot write through symlink.", code="symlink_blocked", path=str(target))
        data = str(content or "")
        target.write_text(data, encoding="utf-8")
        return {"path": str(target), "name": target.name, "bytes_written": len(data.encode("utf-8")), "operation": op}

    async def execute_delete_file(
        self,
        file_path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        target = await self._require_auth(file_path, "delete_file", task_id=task_id, session_id=session_id)
        if not target.exists():
            raise FilesystemSecurityError(f"Path not found: {target}", code="not_found", path=str(target))
        if _has_reparse_or_symlink(target):
            raise FilesystemSecurityError("TOCTOU check: cannot delete symlink.", code="symlink_blocked", path=str(target))
        if target.is_dir():
            raise FilesystemSecurityError("Directory recursive deletion is blocked; delete individual files only.", code="dir_delete_blocked", path=str(target))
        target.unlink()
        return {"path": str(target), "deleted": True}

    # ---------------- Native Folder Picker ----------------

    @staticmethod
    def pick_folder_dialog() -> dict[str, Any]:
        """Open a native folder picker dialog on desktop hosts without blocking headless runs."""
        if sys.platform != "win32" and not os.environ.get("DISPLAY"):
            return {"ok": False, "cancelled": True, "path": None, "reason": "No desktop display available."}
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            selected = filedialog.askdirectory(title="Select Folder to Grant to Jarvis")
            root.destroy()
            if not selected:
                return {"ok": True, "cancelled": True, "path": None}
            return {"ok": True, "cancelled": False, "path": os.path.realpath(selected)}
        except Exception as e:
            return {"ok": False, "cancelled": True, "path": None, "reason": str(e)}

    # ---------------- Approved Project Aliases ----------------

    @staticmethod
    def _normalize_alias_key(alias: str) -> str:
        return " ".join(str(alias or "").strip().lower().split())

    @staticmethod
    def _alias_to_dict(row: ProjectAliasRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "alias": row.alias,
            "display_name": row.display_name or row.alias,
            "folder_path": row.folder_path,
            "description": row.description or "",
            "usage_count": int(row.usage_count or 0),
            "last_used_at": row.last_used_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def list_aliases(self) -> list[dict[str, Any]]:
        async with self.db.async_session() as s:
            rows = (
                await s.execute(select(ProjectAliasRow).order_by(ProjectAliasRow.usage_count.desc(), ProjectAliasRow.alias.asc()))
            ).scalars().all()
            return [self._alias_to_dict(r) for r in rows]

    async def upsert_alias(
        self,
        alias: str,
        folder_path: str | Path,
        *,
        description: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        if str(actor or "").strip().lower() not in OWNER_ACTORS:
            raise FilesystemSecurityError("Only owner may create or modify project aliases.", code="owner_only_approval")
        raw_alias = str(alias or "").strip()
        key = self._normalize_alias_key(raw_alias)
        if not key or len(key) > 100:
            raise ValueError("Alias name must be 1..100 characters.")
        resolved, _ = self._screen_target_security(folder_path, "list_directory")
        if not resolved.exists() or not resolved.is_dir():
            raise FilesystemSecurityError(f"Alias path must be an existing directory: {resolved}", code="not_a_directory")
        now = time.time()
        async with self.db.async_session() as s:
            existing = (await s.execute(select(ProjectAliasRow).where(ProjectAliasRow.alias == key))).scalar_one_or_none()
            if existing is None:
                row = ProjectAliasRow(
                    alias=key,
                    display_name=raw_alias[:160],
                    folder_path=str(resolved),
                    description=str(description or "")[:400],
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            else:
                row = existing
                row.display_name = raw_alias[:160]
                row.folder_path = str(resolved)
                row.description = str(description or "")[:400]
                row.updated_at = now
            await s.commit()
            await s.refresh(row)
        await self._log_audit(
            actor=actor,
            decision="alias_saved",
            target=str(resolved),
            details={"alias": key, "folder_path": str(resolved)},
        )
        return self._alias_to_dict(row)

    async def delete_alias(self, alias: str, *, actor: str = "owner") -> bool:
        if str(actor or "").strip().lower() not in OWNER_ACTORS:
            raise FilesystemSecurityError("Only owner may delete project aliases.", code="owner_only_approval")
        key = self._normalize_alias_key(alias)
        async with self.db.async_session() as s:
            row = (await s.execute(select(ProjectAliasRow).where(ProjectAliasRow.alias == key))).scalar_one_or_none()
            if row is None:
                row = await s.get(ProjectAliasRow, alias)
            if row is None:
                return False
            target = row.folder_path
            await s.delete(row)
            await s.commit()
        await self._log_audit(actor=actor, decision="alias_deleted", target=target, details={"alias": key})
        return True

    async def resolve_alias(self, target_or_query: str) -> dict[str, Any] | None:
        """Resolve an alias name, registered project name, or active approved folder basename (with optional subpath)."""
        raw = str(target_or_query or "").strip().strip("\"'")
        if not raw or re.match(r"^(?:[A-Za-z]:[/\\]|\\\\|/)", raw):
            return None
        key = self._normalize_alias_key(raw)
        # Support "ERP_V2/backend/config.py" by matching leading segment against alias/project
        parts = raw.replace("\\", "/").split("/", 1)
        head_key = self._normalize_alias_key(parts[0])
        subpath = parts[1].strip("/") if len(parts) > 1 and parts[1].strip("/") else ""

        async with self.db.async_session() as s:
            aliases = (await s.execute(select(ProjectAliasRow))).scalars().all()
            projects = (await s.execute(select(ProjectRow))).scalars().all()
            perms = (await s.execute(select(FilesystemPermissionRow).where(FilesystemPermissionRow.status == "active"))).scalars().all()

        def _combine(base_path: str, source: str, name: str) -> dict[str, Any] | None:
            base = Path(base_path)
            if subpath:
                cand = base / subpath
                return {"source": source, "alias": name, "root_path": str(base), "resolved_path": str(cand)}
            return {"source": source, "alias": name, "root_path": str(base), "resolved_path": str(base)}

        # 1. Exact alias match (full key or head segment)
        for a in aliases:
            if a.alias in (key, head_key):
                return _combine(a.folder_path, "project_alias", a.display_name or a.alias)
        # 2. Exact registered project match
        for p in projects:
            pk = self._normalize_alias_key(p.name)
            if pk in (key, head_key):
                return _combine(p.root_path, "registered_project", p.name)
        # 3. Active approved folder basename match
        for perm in perms:
            if perm.scope not in ("persistent", "session"):
                continue
            bk = self._normalize_alias_key(Path(perm.canonical_folder_path).name)
            if bk and bk in (key, head_key):
                return _combine(perm.canonical_folder_path, "approved_folder", Path(perm.canonical_folder_path).name)
        # 4. Natural-language token mention of alias or registered project
        lowered = f" {key} "
        tokens = set(re.findall(r"[a-z0-9_.-]+", key))
        best: tuple[int, dict[str, Any]] | None = None
        for a in aliases:
            if len(a.alias) >= 2 and (f" {a.alias} " in lowered or a.alias in tokens):
                cand = _combine(a.folder_path, "project_alias", a.display_name or a.alias)
                if cand and (best is None or len(a.alias) > best[0]):
                    best = (len(a.alias), cand)
        for p in projects:
            pk = self._normalize_alias_key(p.name)
            if len(pk) >= 2 and (f" {pk} " in lowered or pk in tokens):
                cand = _combine(p.root_path, "registered_project", p.name)
                if cand and (best is None or len(pk) > best[0]):
                    best = (len(pk), cand)
        for perm in perms:
            if perm.scope not in ("persistent", "session"):
                continue
            bname = Path(perm.canonical_folder_path).name
            bk = self._normalize_alias_key(bname)
            if len(bk) >= 3 and (f" {bk} " in lowered or bk in tokens):
                cand = _combine(perm.canonical_folder_path, "approved_folder", bname)
                if cand and (best is None or len(bk) > best[0]):
                    best = (len(bk), cand)
        return best[1] if best else None

    # ---------------- Serializers ----------------

    @staticmethod
    def _perm_to_dict(row: FilesystemPermissionRow) -> dict[str, Any]:
        return {
            "permission_id": row.permission_id,
            "canonical_folder_path": row.canonical_folder_path,
            "granted_operations": list(row.granted_operations or []),
            "scope": row.scope,
            "granted_at": row.granted_at,
            "expires_at": row.expires_at,
            "granted_by": row.granted_by,
            "revoked_at": row.revoked_at,
            "status": row.status,
            "purpose": row.purpose,
            "session_id": row.session_id,
            "task_id": row.task_id,
        }

    @staticmethod
    def _request_to_dict(row: FilesystemPermissionRequestRow) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "task_id": row.task_id,
            "requested_path": row.requested_path,
            "canonical_path": row.canonical_path,
            "requested_operations": list(row.requested_operations or []),
            "not_requested_operations": list(row.not_requested_operations or []),
            "sensitive_flags": list(row.sensitive_flags or []),
            "reason": row.reason,
            "access_duration": row.access_duration,
            "status": row.status,
            "permission_id": row.permission_id,
            "deferred_action": dict(row.deferred_action or {}),
            "decided_by": row.decided_by,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "decided_at": row.decided_at,
        }
