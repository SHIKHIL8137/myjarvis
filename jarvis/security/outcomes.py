"""Verified Tool Outcome Contract & Post-Condition Verification (P10.22 P0.1).

Prevents Jarvis from ever claiming a side effect succeeded merely because a
process started, returned 0 without observable artifacts, or skipped checks.
Every tool outcome must expose an explicit status (`done`, `unverified`,
`failed`, `started`, `awaiting_approval`, `cancelled`) and verifiable evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable


class ToolStatus(str, Enum):
    DONE = "done"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    STARTED = "started"
    AWAITING_APPROVAL = "awaiting_approval"
    CANCELLED = "cancelled"


HONEST_OUTCOME_RULE: str = (
    "TOOL HONESTY RULE: Never tell the user an action completed unless its ToolOutcome has status='done' "
    "and verified=True. If status='unverified', state clearly that verification could not be confirmed; "
    "if status='started', state that it was only started; if status='failed', report the failure details."
)


@dataclass
class VerificationCheck:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolOutcome:
    """Explicit structured contract returned by side-effectful tools and workflows."""

    tool_name: str
    status: str
    summary: str
    verified: bool = False
    target: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        valid = {s.value for s in ToolStatus}
        if self.status not in valid:
            self.status = ToolStatus.UNVERIFIED.value
        # Never allow status='done' + verified=True unless at least one check ran and passed.
        if self.status == ToolStatus.DONE.value:
            has_passing = bool(self.checks) and all(bool(c.get("passed")) for c in self.checks)
            if not has_passing:
                self.status = ToolStatus.UNVERIFIED.value
                self.verified = False
                self.checks.append(
                    VerificationCheck(
                        name="post_condition_required",
                        passed=False,
                        detail="Downgraded from 'done' to 'unverified' because no passing post-condition checks were attached.",
                    ).to_dict()
                )
            else:
                self.verified = True
        else:
            self.verified = False

    @property
    def ok(self) -> bool:
        """Legacy boolean compatibility — True ONLY when strictly verified done."""
        return self.status == ToolStatus.DONE.value and self.verified

    def format_for_assistant(self) -> str:
        """Format status honestly for user/LLM consumption."""
        if self.status == ToolStatus.DONE.value and self.verified:
            return f"[VERIFIED: {self.tool_name}] {self.summary}"
        if self.status == ToolStatus.UNVERIFIED.value:
            return f"[UNVERIFIED: {self.tool_name}] {self.summary}"
        if self.status == ToolStatus.STARTED.value:
            return f"[STARTED: {self.tool_name}] {self.summary}"
        if self.status == ToolStatus.AWAITING_APPROVAL.value:
            return f"[AWAITING_APPROVAL: {self.tool_name}] {self.summary}"
        if self.status == ToolStatus.CANCELLED.value:
            return f"[CANCELLED: {self.tool_name}] {self.summary}"
        detail = f" ({self.error})" if self.error else ""
        return f"[FAILED: {self.tool_name}] {self.summary}{detail}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_file_created(
    path: str | Path,
    *,
    tool_name: str = "file_create",
    min_bytes: int = 0,
    expected_sha256: str | None = None,
    root: str | Path | None = None,
) -> ToolOutcome:
    """Verify that a file exists on disk, is readable, meets min_bytes, and matches optional hash/root."""
    p = Path(path)
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"path": str(p)}

    if root is not None:
        try:
            p.resolve().relative_to(Path(root).resolve())
            checks.append(VerificationCheck("within_root", True, str(root)).to_dict())
        except Exception:
            checks.append(VerificationCheck("within_root", False, f"{p} is outside {root}").to_dict())
            return ToolOutcome(
                tool_name=tool_name,
                status=ToolStatus.FAILED.value,
                summary=f"Created file escaped allowed root {root}",
                target=str(p),
                evidence=evidence,
                checks=checks,
                error="path outside allowed root",
            )

    exists = p.exists() and p.is_file()
    checks.append(VerificationCheck("file_exists", exists, str(p)).to_dict())
    if not exists:
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Expected file was not found: {p}",
            target=str(p),
            evidence=evidence,
            checks=checks,
            error="file does not exist",
        )

    size = p.stat().st_size
    evidence["size_bytes"] = size
    size_ok = size >= min_bytes
    checks.append(VerificationCheck("min_size", size_ok, f"{size} >= {min_bytes}").to_dict())

    try:
        digest = sha256_file(p)
        evidence["sha256"] = digest
        checks.append(VerificationCheck("readable", True, "streamed sha256").to_dict())
    except OSError as e:
        checks.append(VerificationCheck("readable", False, str(e)).to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Created file could not be read: {p}",
            target=str(p),
            evidence=evidence,
            checks=checks,
            error=str(e),
        )

    if expected_sha256 is not None:
        hash_ok = digest == expected_sha256
        checks.append(VerificationCheck("sha256_match", hash_ok, f"{digest[:12]} == {expected_sha256[:12]}").to_dict())

    all_passed = all(c["passed"] for c in checks)
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value if all_passed else ToolStatus.FAILED.value,
        summary=(f"Verified file {p.name} ({size} bytes)" if all_passed else f"File verification failed for {p.name}"),
        target=str(p),
        evidence=evidence,
        checks=checks,
        error="" if all_passed else "post-condition check failed",
    )


def verify_file_modified(
    path: str | Path,
    *,
    tool_name: str = "file_modify",
    previous_sha256: str | None = None,
    expected_substring: str | None = None,
) -> ToolOutcome:
    """Verify an existing file was modified rather than left untouched."""
    p = Path(path)
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"path": str(p)}

    if not (p.exists() and p.is_file()):
        checks.append(VerificationCheck("file_exists", False, str(p)).to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Target file missing after modification: {p}",
            target=str(p),
            evidence=evidence,
            checks=checks,
            error="missing target file",
        )

    checks.append(VerificationCheck("file_exists", True, str(p)).to_dict())
    digest = sha256_file(p)
    evidence["sha256"] = digest
    evidence["size_bytes"] = p.stat().st_size

    if expected_substring is not None:
        text = p.read_text(encoding="utf-8", errors="replace")
        has_sub = expected_substring in text
        checks.append(VerificationCheck("expected_substring", has_sub, f"len={len(expected_substring)}").to_dict())
        if not has_sub:
            return ToolOutcome(
                tool_name=tool_name,
                status=ToolStatus.FAILED.value,
                summary=f"Expected updated text not present in {p.name}",
                target=str(p),
                evidence=evidence,
                checks=checks,
                error="expected content missing",
            )

    if previous_sha256 is not None:
        changed = digest != previous_sha256
        checks.append(VerificationCheck("hash_changed", changed, f"{previous_sha256[:10]}->{digest[:10]}").to_dict())
        if not changed and expected_substring is None:
            return ToolOutcome(
                tool_name=tool_name,
                status=ToolStatus.FAILED.value,
                summary=f"File content hash did not change for {p.name}",
                target=str(p),
                evidence=evidence,
                checks=checks,
                error="no content change detected",
            )
    elif expected_substring is None:
        checks.append(VerificationCheck("modification_delta_known", False, "no prior hash or expected content supplied").to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary=f"File {p.name} exists ({evidence['size_bytes']} bytes), but prior state was not captured to verify diff",
            target=str(p),
            evidence=evidence,
            checks=checks,
        )

    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value,
        summary=f"Verified modification to {p.name} (sha256 {digest[:12]})",
        target=str(p),
        evidence=evidence,
        checks=checks,
    )


def verify_file_deleted(path: str | Path, *, tool_name: str = "file_delete") -> ToolOutcome:
    """Verify that a deleted file or directory no longer exists on disk."""
    p = Path(path)
    gone = not p.exists()
    checks = [VerificationCheck("path_absent", gone, str(p)).to_dict()]
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value if gone else ToolStatus.FAILED.value,
        summary=f"Verified removal of {p}" if gone else f"Path still exists after delete: {p}",
        target=str(p),
        evidence={"path": str(p), "exists": not gone},
        checks=checks,
        error="" if gone else "path still exists",
    )


def verify_document_generated(
    path: str | Path,
    *,
    fmt: str = "",
    tool_name: str = "document_generate",
    expected_min_bytes: int = 1,
    parser: Callable[[Path], str] | None = None,
) -> ToolOutcome:
    """Verify generated documents (PDF/DOCX/PPTX/XLSX/MD/TXT) exist and parse non-empty."""
    base = verify_file_created(path, tool_name=tool_name, min_bytes=expected_min_bytes)
    if base.status != ToolStatus.DONE.value:
        return base

    p = Path(path)
    checks = list(base.checks)
    evidence = dict(base.evidence)
    kind = fmt.lower().lstrip(".") if fmt else p.suffix.lower().lstrip(".")

    if kind and p.suffix.lower().lstrip(".") != kind:
        checks.append(VerificationCheck("extension_match", False, f"expected .{kind}, got {p.suffix}").to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Generated document extension {p.suffix} does not match .{kind}",
            target=str(p),
            evidence=evidence,
            checks=checks,
            error="extension mismatch",
        )
    checks.append(VerificationCheck("extension_match", True, p.suffix).to_dict())

    if parser is not None:
        try:
            extracted = parser(p)
            evidence["parsed_chars"] = len(extracted)
            readable_text = bool(extracted.strip())
            checks.append(VerificationCheck("document_parseable", readable_text, f"{len(extracted)} chars extracted").to_dict())
            if not readable_text:
                return ToolOutcome(
                    tool_name=tool_name,
                    status=ToolStatus.UNVERIFIED.value,
                    summary=f"Document {p.name} exists ({evidence.get('size_bytes', 0)} bytes) but extracted text was empty",
                    target=str(p),
                    evidence=evidence,
                    checks=checks,
                )
        except Exception as e:
            checks.append(VerificationCheck("document_parseable", False, str(e)).to_dict())
            return ToolOutcome(
                tool_name=tool_name,
                status=ToolStatus.FAILED.value,
                summary=f"Generated document {p.name} failed readability check: {e}",
                target=str(p),
                evidence=evidence,
                checks=checks,
                error=str(e),
            )

    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value,
        summary=f"Verified generated {kind or 'document'} {p.name} ({evidence.get('size_bytes', 0)} bytes)",
        target=str(p),
        evidence=evidence,
        checks=checks,
    )


def verify_git_state(
    workdir: str | Path,
    *,
    tool_name: str = "git_operation",
    expect_branch: str | None = None,
    expect_head_changed_from: str | None = None,
    expect_clean: bool | None = None,
) -> ToolOutcome:
    """Verify git repository state via read-only git inspection."""
    wp = Path(workdir)
    checks: list[VerificationCheck] = []
    evidence: dict[str, Any] = {"workdir": str(wp)}

    if not wp.exists():
        checks.append(VerificationCheck("workdir_exists", False, str(wp)))
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Git workdir missing: {wp}",
            target=str(wp),
            evidence=evidence,
            checks=[c.to_dict() for c in checks],
            error="missing workdir",
        )

    git_bin = shutil.which("git")
    if not git_bin:
        checks.append(VerificationCheck("git_available", False, "git binary not found"))
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary="Cannot verify git state because git binary is unavailable",
            target=str(wp),
            evidence=evidence,
            checks=[c.to_dict() for c in checks],
        )

    try:
        head_proc = subprocess.run(
            [git_bin, "-C", str(wp), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch_proc = subprocess.run(
            [git_bin, "-C", str(wp), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_proc = subprocess.run(
            [git_bin, "-C", str(wp), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:
        checks.append(VerificationCheck("git_inspect", False, str(e)))
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary=f"Git verification errored: {e}",
            target=str(wp),
            evidence=evidence,
            checks=[c.to_dict() for c in checks],
        )

    if head_proc.returncode != 0:
        checks.append(VerificationCheck("git_repo_valid", False, head_proc.stderr.strip()))
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=f"Directory is not a valid git repository: {wp}",
            target=str(wp),
            evidence=evidence,
            checks=[c.to_dict() for c in checks],
            error=head_proc.stderr.strip(),
        )

    head_sha = head_proc.stdout.strip()
    branch = branch_proc.stdout.strip()
    dirty = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]
    evidence.update({"head": head_sha, "branch": branch, "dirty_count": len(dirty)})
    checks.append(VerificationCheck("git_repo_valid", True, f"{branch}@{head_sha[:10]}"))

    if expect_branch is not None:
        ok = branch == expect_branch
        checks.append(VerificationCheck("expected_branch", ok, f"{branch} == {expect_branch}"))
    if expect_head_changed_from is not None:
        ok = head_sha != expect_head_changed_from
        checks.append(VerificationCheck("head_advanced", ok, f"{expect_head_changed_from[:10]}->{head_sha[:10]}"))
    if expect_clean is not None:
        ok = (len(dirty) == 0) == expect_clean
        checks.append(VerificationCheck("clean_tree", ok, f"dirty_count={len(dirty)}"))

    all_passed = all(c.passed for c in checks)
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value if all_passed else ToolStatus.FAILED.value,
        summary=f"Git verified at {branch}@{head_sha[:10]}" if all_passed else f"Git verification failed for {wp}",
        target=str(wp),
        evidence=evidence,
        checks=[c.to_dict() for c in checks],
        error="" if all_passed else "git post-condition failed",
    )


def verify_docker_state(
    target: str,
    *,
    expect_running: bool | None = None,
    expect_image_exists: bool | None = None,
    tool_name: str = "docker_operation",
    runner: Callable[..., Any] | None = None,
) -> ToolOutcome:
    """Verify real Docker container/image state; returns UNVERIFIED if Docker daemon/CLI is missing."""
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"target": target}
    docker_bin = shutil.which("docker")
    if not docker_bin and runner is None:
        checks.append(VerificationCheck("docker_cli_available", False, "docker CLI not found").to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary=f"Docker CLI unavailable; cannot verify state of {target}",
            target=target,
            evidence=evidence,
            checks=checks,
        )

    exec_fn = runner or (
        lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    )
    try:
        if expect_image_exists is not None:
            proc = exec_fn([docker_bin or "docker", "image", "inspect", "--format", "{{.Id}}", target])
            exists = proc.returncode == 0 and bool((proc.stdout or "").strip())
            evidence["image_id"] = (proc.stdout or "").strip()
            ok = exists == expect_image_exists
            checks.append(VerificationCheck("image_exists", ok, f"exists={exists}").to_dict())
        else:
            proc = exec_fn([docker_bin or "docker", "inspect", "--format", "{{.State.Running}}", target])
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                if "cannot connect to the docker daemon" in stderr.lower() or "daemon" in stderr.lower():
                    checks.append(VerificationCheck("docker_daemon_reachable", False, stderr).to_dict())
                    return ToolOutcome(
                        tool_name=tool_name,
                        status=ToolStatus.UNVERIFIED.value,
                        summary=f"Docker daemon unreachable; cannot verify {target}",
                        target=target,
                        evidence=evidence,
                        checks=checks,
                    )
                checks.append(VerificationCheck("container_inspect", False, stderr or "not found").to_dict())
                return ToolOutcome(
                    tool_name=tool_name,
                    status=ToolStatus.FAILED.value,
                    summary=f"Docker target {target} not found",
                    target=target,
                    evidence=evidence,
                    checks=checks,
                    error=stderr or "inspect failed",
                )
            running = (proc.stdout or "").strip().lower() == "true"
            evidence["running"] = running
            if expect_running is not None:
                ok = running == expect_running
                checks.append(VerificationCheck("container_running", ok, f"running={running}").to_dict())
            else:
                checks.append(VerificationCheck("container_inspect", True, f"running={running}").to_dict())
    except Exception as e:
        checks.append(VerificationCheck("docker_verify_exec", False, str(e)).to_dict())
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary=f"Docker verification could not run for {target}: {e}",
            target=target,
            evidence=evidence,
            checks=checks,
        )

    all_passed = all(c["passed"] for c in checks)
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value if all_passed else ToolStatus.FAILED.value,
        summary=f"Verified Docker target {target}" if all_passed else f"Docker post-condition failed for {target}",
        target=target,
        evidence=evidence,
        checks=checks,
        error="" if all_passed else "docker post-condition failed",
    )


def verify_process_state(
    *,
    tool_name: str = "process_operation",
    pid: int | None = None,
    exit_code: int | None = None,
    target: str = "",
    started_only: bool = False,
) -> ToolOutcome:
    """Report process launch / exit honestly without falsely claiming 'done' when only started."""
    if started_only:
        checks = [VerificationCheck("process_launched", pid is not None and pid > 0, f"pid={pid}").to_dict()]
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.STARTED.value if (pid and pid > 0) else ToolStatus.UNVERIFIED.value,
            summary=f"Process started (pid={pid}); completion not verified" if (pid and pid > 0) else "Process launch status unverified",
            target=target or str(pid or ""),
            evidence={"pid": pid},
            checks=checks,
        )
    if exit_code is None:
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.UNVERIFIED.value,
            summary=f"Process {target or tool_name} state could not be confirmed",
            target=target,
            evidence={"pid": pid},
            checks=[VerificationCheck("exit_code_observed", False, "no exit code").to_dict()],
        )
    ok = exit_code == 0
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.DONE.value if ok else ToolStatus.FAILED.value,
        summary=f"Process {target or tool_name} exited with code {exit_code}",
        target=target,
        evidence={"pid": pid, "exit_code": exit_code},
        checks=[VerificationCheck("exit_code_zero", ok, f"exit_code={exit_code}").to_dict()],
        error="" if ok else f"exit_code={exit_code}",
    )


def normalize_tool_result(
    tool_name: str,
    raw: dict[str, Any],
    *,
    target: str = "",
    checks: list[dict[str, Any]] | None = None,
) -> ToolOutcome:
    """Wrap any tool dictionary safely into a ToolOutcome (downgrading unverified ok=True)."""
    if isinstance(raw.get("outcome"), dict) and raw["outcome"].get("status"):
        out = raw["outcome"]
        return ToolOutcome(
            tool_name=str(out.get("tool_name") or tool_name),
            status=str(out["status"]),
            summary=str(out.get("summary") or raw.get("message") or tool_name),
            verified=bool(out.get("verified", False)),
            target=str(out.get("target") or target),
            evidence=dict(out.get("evidence") or {}),
            checks=list(out.get("checks") or checks or []),
            error=str(out.get("error") or ""),
        )
    if raw.get("status") in ("awaiting_permission", "awaiting_approval"):
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.AWAITING_APPROVAL.value,
            summary=str(raw.get("message") or raw.get("reason") or "Awaiting explicit approval"),
            target=target,
            evidence={"raw_status": raw.get("status")},
            checks=[],
        )
    if raw.get("status") == "started":
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.STARTED.value,
            summary=str(raw.get("message") or f"{tool_name} started"),
            target=target,
            evidence={k: v for k, v in raw.items() if k != "content"},
            checks=[],
        )
    if raw.get("ok") is False or raw.get("error"):
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.FAILED.value,
            summary=str(raw.get("error") or raw.get("message") or f"{tool_name} failed"),
            target=target,
            evidence={},
            checks=list(checks or []),
            error=str(raw.get("error") or "failed"),
        )
    if checks:
        all_ok = all(bool(c.get("passed")) for c in checks)
        return ToolOutcome(
            tool_name=tool_name,
            status=ToolStatus.DONE.value if all_ok else ToolStatus.FAILED.value,
            summary=str(raw.get("message") or f"{tool_name} verified"),
            target=target,
            evidence={},
            checks=list(checks),
            error="" if all_ok else "verification failed",
        )
    return ToolOutcome(
        tool_name=tool_name,
        status=ToolStatus.UNVERIFIED.value,
        summary=str(raw.get("message") or f"{tool_name} ran without post-condition verification"),
        target=target,
        evidence={},
        checks=[],
    )

