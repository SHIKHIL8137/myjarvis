"""Atomic model rollback controller (Phase 7): safely restores previous or baseline model version."""
from .model_registry import ModelRegistry
from .training_audit import TrainingAuditLog


def rollback_active_model(
    registry: ModelRegistry,
    *,
    target_version_id: str | None = None,
    actor: str = "user",
    audit_log: TrainingAuditLog | None = None,
) -> dict:
    """Roll back active model pointer atomically without corrupting active runtime."""
    with registry._lock:
        if not registry.spec.rollback_enabled:
            raise PermissionError("Model rollback is disabled in settings.")
        state = registry._state
        current_id = state.get("active_version_id", "")
        candidate_target = (
            target_version_id
            or state.get("previous_version_id")
            or state.get("baseline_version_id")
        )
        if not candidate_target or candidate_target not in state["versions"]:
            candidate_target = state.get("baseline_version_id")
        if not candidate_target or candidate_target not in state["versions"]:
            raise RuntimeError("No valid baseline or previous version found in registry to roll back to.")

        target = state["versions"][candidate_target]
        if current_id and current_id != candidate_target and current_id in state["versions"]:
            cur_rec = state["versions"][current_id]
            if cur_rec.get("status") != "baseline":
                cur_rec["status"] = "rolled_back"
        if target.get("status") != "baseline":
            target["status"] = "promoted"
        state["active_version_id"] = candidate_target
        registry._save()

    log = audit_log or registry.audit
    log.record("model_rolled_back", actor=actor, details={"from_version": current_id, "to_version": candidate_target})
    return {
        "rolled_back_from": current_id,
        "active_version_id": candidate_target,
        "active": target,
    }

