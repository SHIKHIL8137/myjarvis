"""Opt-in Proactive HUD Suggestion Engine (Phase 10 / Stage E).

Produces actionable yet strictly NON-EXECUTING suggestions from real observed
system alerts, durable task failures, and repeated command patterns.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .config import ProactiveSpec


class ProactiveSuggestionEngine:
    """Computes dismissible HUD suggestion cards without ever running tools automatically."""

    def __init__(self, spec: ProactiveSpec | None = None, db: Any = None) -> None:
        self.spec = spec or ProactiveSpec()
        self.db = db
        self._dismissed_until: dict[str, float] = {}
        self._lock = threading.Lock()
        if self.db is not None:
            self._load_dismissals_sync()

    def _load_dismissals_sync(self) -> None:
        if self.db is None:
            return
        now = time.time()
        try:
            from .db.models import ProactiveDismissalRow
            with self.db.sync_session() as s:
                rows = s.query(ProactiveDismissalRow).filter(ProactiveDismissalRow.dismissed_until > now).all()
                with self._lock:
                    for r in rows:
                        self._dismissed_until[r.id] = float(r.dismissed_until)
        except Exception:
            pass

    def update_spec(self, spec: ProactiveSpec) -> None:
        self.spec = spec

    def dismiss(self, suggestion_id: str) -> dict[str, Any]:
        until = time.time() + float(self.spec.cooldown_s)
        with self._lock:
            self._dismissed_until[suggestion_id] = until
        if self.db is not None:
            try:
                from .db.models import ProactiveDismissalRow
                with self.db.sync_session() as s:
                    row = s.get(ProactiveDismissalRow, suggestion_id)
                    if row is None:
                        row = ProactiveDismissalRow(id=suggestion_id, dismissed_until=until)
                        s.add(row)
                    else:
                        row.dismissed_until = until
                    s.commit()
            except Exception:
                pass
        return {"dismissed": suggestion_id, "cooldown_until": round(until, 2)}

    def clear_dismissals(self) -> None:
        with self._lock:
            self._dismissed_until.clear()
        if self.db is not None:
            try:
                from .db.models import ProactiveDismissalRow
                with self.db.sync_session() as s:
                    s.query(ProactiveDismissalRow).delete()
                    s.commit()
            except Exception:
                pass

    def _is_suppressed(self, sid: str, now: float) -> bool:
        with self._lock:
            exp = self._dismissed_until.get(sid, 0.0)
            if exp > now:
                return True
            self._dismissed_until.pop(sid, None)
            return False

    def collect(
        self,
        *,
        sys_snapshot: dict[str, Any] | None = None,
        recent_tasks: list[dict[str, Any]] | None = None,
        recent_commands: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.spec.enabled:
            return []

        now = time.time()
        suggestions: list[dict[str, Any]] = []

        # 1. System resource alert suggestions (never auto-executes)
        if self.spec.suggest_on_system_alerts and sys_snapshot:
            for al in sys_snapshot.get("alerts") or []:
                sid = f"sys_{al.get('id', 'alert')}"
                if self._is_suppressed(sid, now):
                    continue
                metric = al.get("metric", "")
                prompt = (
                    "Show high-memory processes and suggest cleanup"
                    if metric in ("ram_used_pct", "gpu_vram_pct")
                    else "Show system status and disk usage"
                )
                suggestions.append({
                    "id": sid,
                    "kind": "system_alert",
                    "severity": al.get("severity", "warning"),
                    "title": f"System Alert: {metric or 'resource'}",
                    "reason": str(al.get("message") or ""),
                    "suggested_action_label": "Inspect processes & system health",
                    "suggested_prompt": prompt,
                    "auto_execute": False,
                    "requires_user_action": True,
                    "created_at": now,
                })

        # 2. Failed task recovery suggestion
        if self.spec.suggest_on_failed_tasks and recent_tasks:
            failed = [t for t in recent_tasks if t.get("status") in ("failed", "paused", "awaiting_permission")]
            if failed:
                top = failed[0]
                sid = f"task_{top['task_id']}"
                if not self._is_suppressed(sid, now):
                    suggestions.append({
                        "id": sid,
                        "kind": "task_attention",
                        "severity": "info" if top.get("status") == "awaiting_permission" else "warning",
                        "title": f"Task requires attention ({top.get('status')})",
                        "reason": f"'{str(top.get('user_request') or '')[:90]}' stopped at: {top.get('current_step') or top.get('last_error') or 'checkpoint'}",
                        "suggested_action_label": "Show failed tasks",
                        "suggested_prompt": "show failed tasks",
                        "auto_execute": False,
                        "requires_user_action": True,
                        "created_at": now,
                    })

        # 3. Repeated workflow suggestion
        if self.spec.suggest_on_repeated_commands and recent_commands:
            counts: dict[str, int] = {}
            samples: dict[str, str] = {}
            for cmd in recent_commands:
                norm = str(cmd.get("normalized_command") or "").strip()
                if norm and cmd.get("replayable", True):
                    counts[norm] = counts.get(norm, 0) + 1
                    samples.setdefault(norm, str(cmd.get("raw_text") or norm))
            for norm, cnt in counts.items():
                if cnt >= 3:
                    sid = f"repeat_{abs(hash(norm)) % 100000}"
                    if not self._is_suppressed(sid, now):
                        suggestions.append({
                            "id": sid,
                            "kind": "workflow_shortcut",
                            "severity": "info",
                            "title": "Frequent command detected",
                            "reason": f"You ran `{samples[norm][:70]}` {cnt} times recently.",
                            "suggested_action_label": "Replay last command",
                            "suggested_prompt": samples[norm],
                            "auto_execute": False,
                            "requires_user_action": True,
                            "created_at": now,
                        })
                    break

        return suggestions[:5]

