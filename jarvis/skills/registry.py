"""Persistent DB + isolated filesystem Agent Skills Registry, versioning, assignments & metrics."""
from pathlib import Path
import shutil
import time

from ..agents.approvals import ApprovalService
from ..db.engine import DatabaseManager
from ..db.models import AuditEventRow, SkillAssignmentRow, SkillMetricRow, SkillRow, SkillVersionRow
from .metadata import SkillManifest, compute_directory_sha256, normalize_skill_id
from .recommended_audit import map_verification_to_classification
from .validator import SkillValidationError, validate_skill_package


class SkillRegistryService:
    """Central DB-backed skill lifecycle manager keeping skill content in isolated directories."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        skills_root: Path,
        approvals: ApprovalService | None = None,
        allowed_licenses: set[str] | None = None,
        require_install_approval: bool = True,
        require_update_approval: bool = True,
        allow_untrusted_scripts: bool = False,
    ):
        self.db = db
        self.skills_root = Path(skills_root).resolve()
        self.installed_root = self.skills_root / "installed"
        self.installed_root.mkdir(parents=True, exist_ok=True)
        self.approvals = approvals or ApprovalService(db)
        self.allowed_licenses = allowed_licenses or {"mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "isc", "cc-by-4.0", "cc0-1.0"}
        self.require_install_approval = require_install_approval
        self.require_update_approval = require_update_approval
        self.allow_untrusted_scripts = allow_untrusted_scripts

    def _audit(self, *, actor: str, decision: str, skill_id: str, details: dict | None = None) -> None:
        try:
            with self.db.sync_session() as s:
                s.add(
                    AuditEventRow(
                        actor=(actor or "system")[:64],
                        category="skills",
                        decision=decision[:24],
                        target=skill_id[:500],
                        details=details or {},
                    )
                )
                s.commit()
        except Exception:
            pass

    @staticmethod
    def _row_to_dict(r: SkillRow) -> dict:
        m = dict(r.manifest_json or {})
        return {
            "skill_id": r.skill_id,
            "name": r.name,
            "description": r.description,
            "category": r.category,
            "version": r.version,
            "source_repo": r.source_repo,
            "source_commit": r.source_commit,
            "content_sha256": r.content_sha256,
            "license": r.license,
            "supported_runtimes": list(r.supported_runtimes or []),
            "compatible_roles": list(r.compatible_roles or []),
            "required_dependencies": list(r.required_dependencies or []),
            "required_permissions": list(r.required_permissions or []),
            "network_requirements": list(r.network_requirements or []),
            "execution_mode": r.execution_mode,
            "approval_required": bool(r.approval_required),
            "enabled": bool(r.enabled),
            "verification_status": r.verification_status,
            "audit_classification": map_verification_to_classification(r.verification_status, runtime_compat=m.get("runtime_compat") or {}),
            "install_path": r.install_path,
            "installed_at": r.installed_at,
            "updated_at": r.updated_at,
            "last_used_at": r.last_used_at,
            "runtime_compat": m.get("runtime_compat") or {},
            "security_report": m.get("security_report") or {},
            "keywords": m.get("keywords") or [],
        }

    def validate_candidate(
        self,
        source_dir: Path,
        *,
        skill_id: str | None = None,
        category: str = "general",
        source_repo: str = "",
        source_commit: str = "",
        license_override: str = "",
        compatible_roles: list[str] | None = None,
        expected_sha256: str = "",
    ) -> tuple[SkillManifest, dict]:
        return validate_skill_package(
            Path(source_dir),
            skill_id=skill_id,
            category=category,
            source_repo=source_repo,
            source_commit=source_commit,
            license_override=license_override,
            compatible_roles=compatible_roles,
            expected_sha256=expected_sha256,
            allowed_licenses=self.allowed_licenses,
            allow_untrusted_scripts=self.allow_untrusted_scripts,
        )

    def request_install(
        self,
        source_dir: Path,
        *,
        skill_id: str | None = None,
        category: str = "general",
        source_repo: str = "",
        source_commit: str = "",
        license_override: str = "",
        compatible_roles: list[str] | None = None,
        expected_sha256: str = "",
        requested_by: str = "owner",
    ) -> dict:
        manifest, report = self.validate_candidate(
            source_dir,
            skill_id=skill_id,
            category=category,
            source_repo=source_repo,
            source_commit=source_commit,
            license_override=license_override,
            compatible_roles=compatible_roles,
            expected_sha256=expected_sha256,
        )
        if not report["valid"]:
            raise SkillValidationError(f"Skill {manifest.skill_id!r} failed security validation and cannot be requested.")
        req = self.approvals.create_request(
            action_type="install_skill",
            target_resource=f"{manifest.skill_id}@{manifest.version}",
            preview_diff=f"Source: {source_dir}\nCommit: {source_commit or 'local'}\nLicense: {manifest.license}\nSHA-256: {manifest.content_sha256}\nPermissions: {manifest.required_permissions}",
            args={
                "skill_id": manifest.skill_id,
                "content_sha256": manifest.content_sha256,
                "source_dir": str(Path(source_dir).resolve()),
                "requested_by": requested_by,
            },
        )
        return {"approval": req, "manifest": manifest.model_dump(), "validation_report": report}

    @staticmethod
    def _copy_isolated(src_dir: Path, dest_dir: Path) -> None:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(
            src_dir,
            dest_dir,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", "*.tmp"),
        )

    def install_skill(
        self,
        source_dir: Path,
        *,
        skill_id: str | None = None,
        category: str = "general",
        version: str | None = None,
        source_repo: str = "",
        source_commit: str = "",
        license_override: str = "",
        compatible_roles: list[str] | None = None,
        expected_sha256: str = "",
        approved: bool = False,
        approval_token: str | None = None,
        actor: str = "owner",
    ) -> dict:
        manifest, report = self.validate_candidate(
            source_dir,
            skill_id=skill_id,
            category=category,
            source_repo=source_repo,
            source_commit=source_commit,
            license_override=license_override,
            compatible_roles=compatible_roles,
            expected_sha256=expected_sha256,
        )
        if version:
            manifest.version = version
        if not report["valid"]:
            self._audit(actor=actor, decision="blocked_install", skill_id=manifest.skill_id, details=report)
            raise SkillValidationError(
                f"Blocked installing skill {manifest.skill_id!r}: {report['recommendation']}"
            )

        with self.db.sync_session() as s:
            if s.get(SkillRow, manifest.skill_id) is not None:
                raise ValueError(f"Skill {manifest.skill_id!r} is already installed (duplicate installation rejected; use update_skill).")

        if self.require_install_approval:
            if approval_token:
                req = self.approvals.consume_approved(approval_token, expected_action_type="install_skill")
                args = req.get("args") or {}
                if args.get("skill_id") and args["skill_id"] != manifest.skill_id:
                    raise PermissionError("Approval token belongs to a different skill_id.")
                if args.get("content_sha256") and args["content_sha256"] != manifest.content_sha256:
                    raise PermissionError("Skill checksum changed since approval was granted.")
                actor = str(req.get("decided_by") or actor)
            elif not approved:
                raise PermissionError(f"Installing skill {manifest.skill_id!r} requires explicit owner approval.")

        ver_slug = f"{normalize_skill_id(manifest.version)}-{manifest.content_sha256[:8]}"
        dest_dir = self.installed_root / manifest.skill_id / ver_slug
        self._copy_isolated(Path(source_dir).resolve(), dest_dir)
        # Re-verify installed checksum equals validated checksum!
        installed_sha = compute_directory_sha256(dest_dir)
        if installed_sha != manifest.content_sha256:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise SkillValidationError("Post-copy skill checksum verification failed.")

        manifest.install_path = str(dest_dir)
        m_dict = manifest.model_dump()
        with self.db.sync_session() as s:
            row = SkillRow(
                skill_id=manifest.skill_id,
                name=manifest.name,
                description=manifest.description,
                category=manifest.category,
                version=manifest.version,
                source_repo=manifest.source_repo,
                source_commit=manifest.source_commit,
                content_sha256=manifest.content_sha256,
                license=manifest.license,
                supported_runtimes=manifest.supported_runtimes,
                compatible_roles=manifest.compatible_roles,
                required_dependencies=manifest.required_dependencies,
                required_permissions=manifest.required_permissions,
                network_requirements=manifest.network_requirements,
                execution_mode=manifest.execution_mode,
                approval_required=manifest.approval_required,
                enabled=True,
                verification_status=manifest.verification_status,
                install_path=str(dest_dir),
                installed_at=manifest.installed_at,
                updated_at=manifest.updated_at,
                manifest_json=m_dict,
            )
            s.add(row)
            s.add(
                SkillVersionRow(
                    skill_id=manifest.skill_id,
                    version=manifest.version,
                    source_commit=manifest.source_commit,
                    content_sha256=manifest.content_sha256,
                    snapshot_dir=str(dest_dir),
                    manifest_json=m_dict,
                    installed_by=actor,
                    action="install",
                )
            )
            s.commit()
            out = self._row_to_dict(row)
        self._audit(actor=actor, decision="installed", skill_id=manifest.skill_id, details={"version": manifest.version, "sha256": manifest.content_sha256})
        return {"skill": out, "validation_report": report}

    def update_skill(
        self,
        skill_id: str,
        source_dir: Path,
        *,
        version: str,
        source_commit: str = "",
        expected_sha256: str = "",
        approved: bool = False,
        approval_token: str | None = None,
        actor: str = "owner",
    ) -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            existing = s.get(SkillRow, sid)
            if existing is None:
                raise KeyError(sid)
            prev_cat = existing.category
            prev_repo = existing.source_repo
            prev_roles = list(existing.compatible_roles or [])
            prev_lic = existing.license

        manifest, report = self.validate_candidate(
            source_dir,
            skill_id=sid,
            category=prev_cat,
            source_repo=prev_repo,
            source_commit=source_commit,
            license_override="" if prev_lic == "unknown" else prev_lic,
            compatible_roles=prev_roles,
            expected_sha256=expected_sha256,
        )
        manifest.version = version
        if not report["valid"]:
            raise SkillValidationError(f"Update blocked for {sid!r}: {report['recommendation']}")
        if self.require_update_approval:
            if approval_token:
                req = self.approvals.consume_approved(approval_token, expected_action_type="update_skill")
                actor = str(req.get("decided_by") or actor)
            elif not approved:
                raise PermissionError(f"Updating skill {sid!r} requires explicit owner approval.")

        ver_slug = f"{normalize_skill_id(version)}-{manifest.content_sha256[:8]}"
        dest_dir = self.installed_root / sid / ver_slug
        self._copy_isolated(Path(source_dir).resolve(), dest_dir)
        manifest.install_path = str(dest_dir)
        now = round(time.time(), 3)
        manifest.updated_at = now
        m_dict = manifest.model_dump()

        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            assert row is not None
            row.version = version
            row.source_commit = source_commit or manifest.source_commit
            row.content_sha256 = manifest.content_sha256
            row.supported_runtimes = manifest.supported_runtimes
            row.required_permissions = manifest.required_permissions
            row.verification_status = manifest.verification_status
            row.install_path = str(dest_dir)
            row.updated_at = now
            row.manifest_json = m_dict
            s.add(
                SkillVersionRow(
                    skill_id=sid,
                    version=version,
                    source_commit=row.source_commit,
                    content_sha256=manifest.content_sha256,
                    snapshot_dir=str(dest_dir),
                    manifest_json=m_dict,
                    installed_by=actor,
                    action="update",
                )
            )
            s.commit()
            out = self._row_to_dict(row)
        self._audit(actor=actor, decision="updated", skill_id=sid, details={"version": version, "sha256": manifest.content_sha256})
        return {"skill": out, "validation_report": report}

    def rollback_skill(self, skill_id: str, *, target_version: str | None = None, actor: str = "owner") -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is None:
                raise KeyError(sid)
            vers = (
                s.query(SkillVersionRow)
                .filter(SkillVersionRow.skill_id == sid)
                .order_by(SkillVersionRow.id.desc())
                .all()
            )
            chosen: SkillVersionRow | None = None
            if target_version:
                for v in vers:
                    if v.version == target_version:
                        chosen = v
                        break
            else:
                for v in vers[1:]:
                    if v.content_sha256 != row.content_sha256 or v.version != row.version:
                        chosen = v
                        break
            if chosen is None:
                raise ValueError(f"No prior version available to roll back skill {sid!r}.")
            snap_dir = Path(chosen.snapshot_dir)
            if not snap_dir.exists() or compute_directory_sha256(snap_dir) != chosen.content_sha256:
                raise SkillValidationError(f"Rollback snapshot integrity check failed for {sid}@{chosen.version}.")

            now = round(time.time(), 3)
            m_dict = dict(chosen.manifest_json or {})
            row.version = chosen.version
            row.source_commit = chosen.source_commit
            row.content_sha256 = chosen.content_sha256
            row.install_path = str(snap_dir)
            row.updated_at = now
            row.manifest_json = m_dict
            s.add(
                SkillVersionRow(
                    skill_id=sid,
                    version=chosen.version,
                    source_commit=chosen.source_commit,
                    content_sha256=chosen.content_sha256,
                    snapshot_dir=str(snap_dir),
                    manifest_json=m_dict,
                    installed_by=actor,
                    action="rollback",
                )
            )
            s.commit()
            out = self._row_to_dict(row)
        self._audit(actor=actor, decision="rolled_back", skill_id=sid, details={"to_version": chosen.version})
        return out

    def enable_skill(self, skill_id: str, *, actor: str = "owner") -> dict:
        return self._set_enabled(skill_id, True, actor=actor)

    def disable_skill(self, skill_id: str, *, actor: str = "owner") -> dict:
        return self._set_enabled(skill_id, False, actor=actor)

    def _set_enabled(self, skill_id: str, enabled: bool, *, actor: str) -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is None:
                raise KeyError(sid)
            row.enabled = enabled
            row.updated_at = round(time.time(), 3)
            s.commit()
            out = self._row_to_dict(row)
        self._audit(actor=actor, decision="enabled" if enabled else "disabled", skill_id=sid)
        return out

    def uninstall_skill(self, skill_id: str, *, actor: str = "owner") -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is None:
                raise KeyError(sid)
            s.delete(row)
            s.commit()
        skill_dir = self.installed_root / sid
        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)
        self._audit(actor=actor, decision="uninstalled", skill_id=sid)
        return {"ok": True, "skill_id": sid, "uninstalled": True}

    def list_skills(self, *, category: str | None = None, enabled_only: bool = False) -> list[dict]:
        with self.db.sync_session() as s:
            q = s.query(SkillRow)
            if category:
                q = q.filter(SkillRow.category == category)
            if enabled_only:
                q = q.filter(SkillRow.enabled.is_(True))
            rows = q.order_by(SkillRow.category.asc(), SkillRow.skill_id.asc()).all()
            return [self._row_to_dict(r) for r in rows]

    def search_skills(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        items = self.list_skills()
        if not q:
            return items
        return [
            it
            for it in items
            if q in it["skill_id"].lower()
            or q in it["name"].lower()
            or q in it["description"].lower()
            or q in it["category"].lower()
        ]

    def get_skill(self, skill_id: str) -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is None:
                raise KeyError(sid)
            return self._row_to_dict(row)

    def assign_skill(
        self,
        skill_id: str,
        *,
        agent_role: str,
        project_id: str | None = None,
        priority: int = 10,
        actor: str = "owner",
    ) -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is None:
                raise KeyError(sid)
            if row.verification_status == "blocked":
                raise SkillValidationError(f"Cannot assign blocked skill {sid!r}.")
            existing = (
                s.query(SkillAssignmentRow)
                .filter(
                    SkillAssignmentRow.skill_id == sid,
                    SkillAssignmentRow.agent_role == agent_role,
                    SkillAssignmentRow.project_id == project_id,
                )
                .first()
            )
            if existing is not None:
                existing.enabled = True
                existing.priority = priority
                s.commit()
                rec = existing
            else:
                rec = SkillAssignmentRow(
                    skill_id=sid,
                    agent_role=agent_role,
                    project_id=project_id,
                    priority=priority,
                    enabled=True,
                    assigned_by=actor,
                )
                s.add(rec)
                s.commit()
            out = {
                "id": rec.id,
                "skill_id": rec.skill_id,
                "agent_role": rec.agent_role,
                "project_id": rec.project_id,
                "priority": rec.priority,
                "enabled": rec.enabled,
            }
        self._audit(actor=actor, decision="assigned", skill_id=sid, details={"agent_role": agent_role, "project_id": project_id})
        return out

    def unassign_skill(self, skill_id: str, *, agent_role: str, project_id: str | None = None, actor: str = "owner") -> dict:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            q = s.query(SkillAssignmentRow).filter(
                SkillAssignmentRow.skill_id == sid,
                SkillAssignmentRow.agent_role == agent_role,
            )
            if project_id is not None:
                q = q.filter(SkillAssignmentRow.project_id == project_id)
            rows = q.all()
            removed = len(rows)
            for r in rows:
                s.delete(r)
            s.commit()
        self._audit(actor=actor, decision="unassigned", skill_id=sid, details={"agent_role": agent_role, "removed": removed})
        return {"ok": True, "removed": removed}

    def list_assignments(self, *, agent_role: str | None = None, project_id: str | None = None) -> list[dict]:
        with self.db.sync_session() as s:
            q = s.query(SkillAssignmentRow).filter(SkillAssignmentRow.enabled.is_(True))
            if agent_role:
                q = q.filter(SkillAssignmentRow.agent_role == agent_role)
            if project_id is not None:
                q = q.filter((SkillAssignmentRow.project_id == project_id) | (SkillAssignmentRow.project_id.is_(None)))
            rows = q.order_by(SkillAssignmentRow.priority.desc()).all()
            return [
                {
                    "id": r.id,
                    "skill_id": r.skill_id,
                    "agent_role": r.agent_role,
                    "project_id": r.project_id,
                    "priority": r.priority,
                    "enabled": r.enabled,
                    "assigned_by": r.assigned_by,
                }
                for r in rows
            ]

    def skill_history(self, skill_id: str) -> list[dict]:
        sid = normalize_skill_id(skill_id)
        with self.db.sync_session() as s:
            rows = (
                s.query(SkillVersionRow)
                .filter(SkillVersionRow.skill_id == sid)
                .order_by(SkillVersionRow.id.desc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "skill_id": r.skill_id,
                    "version": r.version,
                    "source_commit": r.source_commit,
                    "content_sha256": r.content_sha256,
                    "action": r.action,
                    "installed_by": r.installed_by,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    def record_invocation_metric(
        self,
        skill_id: str,
        *,
        agent_role: str,
        run_id: str | None = None,
        task_kind: str = "general",
        success: bool = True,
        qa_passed: bool | None = None,
        baseline_qa_passed: bool | None = None,
        latency_ms: float = 0.0,
        token_overhead: int = 0,
        user_corrected: bool = False,
    ) -> dict:
        sid = normalize_skill_id(skill_id)
        now = round(time.time(), 3)
        with self.db.sync_session() as s:
            row = s.get(SkillRow, sid)
            if row is not None:
                row.last_used_at = now
            m = SkillMetricRow(
                skill_id=sid,
                agent_role=agent_role,
                run_id=run_id,
                task_kind=task_kind,
                success=bool(success),
                qa_passed=qa_passed,
                baseline_qa_passed=baseline_qa_passed,
                latency_ms=float(latency_ms),
                token_overhead=int(token_overhead),
                user_corrected=bool(user_corrected),
                created_at=now,
            )
            s.add(m)
            s.commit()
            return {"id": m.id, "skill_id": sid, "success": m.success}

    def skill_metrics_summary(self, skill_id: str | None = None) -> dict[str, dict]:
        with self.db.sync_session() as s:
            q = s.query(SkillMetricRow)
            if skill_id:
                q = q.filter(SkillMetricRow.skill_id == normalize_skill_id(skill_id))
            rows = q.all()

        grouped: dict[str, list[SkillMetricRow]] = {}
        for r in rows:
            grouped.setdefault(r.skill_id, []).append(r)

        summary: dict[str, dict] = {}
        for sid, items in grouped.items():
            n = len(items)
            succ = sum(1 for r in items if r.success)
            qa_items = [r for r in items if r.qa_passed is not None]
            qa_rate = (sum(1 for r in qa_items if r.qa_passed) / len(qa_items)) if qa_items else None
            base_items = [r for r in items if r.baseline_qa_passed is not None]
            base_rate = (sum(1 for r in base_items if r.baseline_qa_passed) / len(base_items)) if base_items else None
            corrs = sum(1 for r in items if r.user_corrected)
            succ_rate = succ / n
            fail_rate = (n - succ) / n
            qa_delta = round(qa_rate - base_rate, 3) if (qa_rate is not None and base_rate is not None) else None

            # Evidence-based assessment: never assume invocation == quality gain
            if fail_rate > 0.4 or (qa_delta is not None and qa_delta < -0.1) or (corrs / n > 0.3):
                verdict = "degrading_recommend_disable"
            elif (qa_delta is not None and qa_delta > 0.05) or (succ_rate >= 0.85 and n >= 3 and corrs == 0):
                verdict = "helpful"
            else:
                verdict = "inconclusive"

            summary[sid] = {
                "invocations": n,
                "success_rate": round(succ_rate, 3),
                "failure_rate": round(fail_rate, 3),
                "qa_pass_rate": round(qa_rate, 3) if qa_rate is not None else None,
                "baseline_qa_pass_rate": round(base_rate, 3) if base_rate is not None else None,
                "qa_improvement_delta": qa_delta,
                "avg_latency_ms": round(sum(r.latency_ms for r in items) / n, 1),
                "avg_token_overhead": round(sum(r.token_overhead for r in items) / n, 1),
                "user_corrections": corrs,
                "quality_assessment": verdict,
            }
        return summary

    def recent_audit(self, limit: int = 40) -> list[dict]:
        with self.db.sync_session() as s:
            rows = (
                s.query(AuditEventRow)
                .filter(AuditEventRow.category == "skills")
                .order_by(AuditEventRow.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "actor": r.actor,
                    "decision": r.decision,
                    "target": r.target,
                    "details": r.details,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

