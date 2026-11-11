"""Agent Capability Router selecting validated, compatible skills per task within context budgets."""
from pathlib import Path
import re

from .compatibility import adapt_skill_for_runtime
from .registry import SkillRegistryService

# Deterministic task -> preferred skill categories & tags
_TASK_CATEGORY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:security|audit|vuln|pen[\s\-]?test|xss|sqli|ssrf|secret|csrf|auth)\b", re.IGNORECASE), "security_audit"),
    (re.compile(r"\b(?:tdd|test[\s\-]?driven|unit test|integration test|api|endpoint|route|code review|review)\b", re.IGNORECASE), "testing_api"),
    (re.compile(r"\b(?:code[\s\-]?review|pull request|diff review)\b", re.IGNORECASE), "code_review"),
    (re.compile(r"\b(?:debug|diagnos|bug|traceback|crash|failing|failure|exception)\b", re.IGNORECASE), "debugging"),
    (re.compile(r"\b(?:architect|domain model|schema|design system|module)\b", re.IGNORECASE), "architecture"),
    (re.compile(r"\b(?:refactor|simplify|clean up|yagni|ponytail)\b", re.IGNORECASE), "refactoring"),
    (re.compile(r"\b(?:react|tsx|frontend|component|css|tailwind|impeccable)\b", re.IGNORECASE), "react_frontend"),
    (re.compile(r"\b(?:ui review|hallmark|visual polish|redesign|layout)\b", re.IGNORECASE), "ui_review"),
    (re.compile(r"\b(?:explain|diagram|anatomy|walkthrough|technical explanation|document)\b", re.IGNORECASE), "technical_explanation"),
]


class AgentCapabilityRouter:
    """Sits cleanly between DecisionEngine & agent workers; loads only relevant compatible skills."""

    def __init__(self, registry: SkillRegistryService, *, default_max_context_chars: int = 3200, default_max_skills: int = 3):
        self.registry = registry
        self.default_max_context_chars = default_max_context_chars
        self.default_max_skills = default_max_skills

    @staticmethod
    def infer_categories(task_prompt: str, intent: str = "") -> list[str]:
        text = f"{intent} {task_prompt}"
        cats: list[str] = []
        for pat, cat in _TASK_CATEGORY_RULES:
            if pat.search(text) and cat not in cats:
                cats.append(cat)
        return cats

    def select_skills_for_task(
        self,
        *,
        task_prompt: str,
        intent: str = "",
        department: str = "assistant",
        complexity: str = "standard",
        agent_role: str = "coder_agent",
        project_id: str | None = None,
        runtime: str = "jarvis_native",
        max_skills: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict:
        """Select enabled, compatible, permission-valid skills relevant to the task within context budget."""
        q_clean = (task_prompt or "").strip()
        if complexity in ("trivial", "fast_command") or department == "system" or (len(q_clean) < 16 and intent in ("chat", "small_talk", "fast_command")):
            return {
                "selected": [],
                "skipped_reason": "simple_request_no_skills_needed",
                "inferred_categories": [],
                "combined_prompt_block": "",
                "total_chars": 0,
            }

        eff_max_skills = max_skills if max_skills is not None else self.default_max_skills
        eff_max_chars = max_context_chars if max_context_chars is not None else self.default_max_context_chars

        skills = self.registry.list_skills(enabled_only=True)
        assignments = self.registry.list_assignments(agent_role=agent_role, project_id=project_id)
        assigned_prio = {a["skill_id"]: int(a["priority"]) for a in assignments}
        inferred = self.infer_categories(task_prompt, intent=intent)
        metrics = self.registry.skill_metrics_summary()

        candidates: list[tuple[int, dict]] = []
        rejected: list[dict] = []
        q_lower = q_clean.lower()

        for sk in skills:
            sid = sk["skill_id"]
            if sk["verification_status"] == "blocked":
                rejected.append({"skill_id": sid, "reason": "security_blocked"})
                continue
            compat = (sk.get("runtime_compat") or {}).get(runtime)
            if compat is not None and not compat.get("compatible"):
                rejected.append({"skill_id": sid, "reason": f"incompatible_runtime:{runtime}"})
                continue

            roles = sk.get("compatible_roles") or []
            if sid not in assigned_prio and agent_role not in roles:
                rejected.append({"skill_id": sid, "reason": f"role_mismatch:{agent_role}"})
                continue

            # Skip auto-selecting skills flagged as degrading unless explicitly assigned at high priority
            sk_metric = metrics.get(sid) or {}
            if sk_metric.get("quality_assessment") == "degrading_recommend_disable" and assigned_prio.get(sid, 0) < 50:
                rejected.append({"skill_id": sid, "reason": "quality_degrading"})
                continue

            score = assigned_prio.get(sid, 0)
            cat = sk.get("category", "general")
            if cat in inferred:
                score += 40
            if sid in q_lower or sk["name"].lower() in q_lower:
                score += 60
            for kw in sk.get("keywords") or []:
                if len(kw) >= 3 and kw in q_lower:
                    score += 12

            # Require either topical match OR explicit agent/project assignment
            if score <= 0:
                rejected.append({"skill_id": sid, "reason": "irrelevant_to_task"})
                continue
            candidates.append((score, sk))

        candidates.sort(key=lambda pair: (-pair[0], pair[1]["skill_id"]))

        selected: list[dict] = []
        blocks: list[str] = []
        used_chars = 0

        for score, sk in candidates:
            if len(selected) >= eff_max_skills:
                break
            install_dir = Path(sk["install_path"])
            try:
                adapted = adapt_skill_for_runtime(sk, install_dir, runtime=runtime, allow_scripts=False)
            except Exception as e:
                rejected.append({"skill_id": sk["skill_id"], "reason": str(e)})
                continue

            blk = adapted["prompt_block"]
            remaining = eff_max_chars - used_chars
            if remaining < 180:
                rejected.append({"skill_id": sk["skill_id"], "reason": "context_budget_exhausted"})
                continue
            if len(blk) > remaining:
                # Truncate cleanly preserving closing </jarvis_skill> tag
                blk = blk[: remaining - 20].rstrip() + "\n...\n</jarvis_skill>"
            blocks.append(blk)
            used_chars += len(blk)
            selected.append(
                {
                    "skill_id": sk["skill_id"],
                    "name": sk["name"],
                    "category": sk["category"],
                    "version": sk["version"],
                    "score": score,
                    "mode": sk["execution_mode"],
                    "runtime": runtime,
                }
            )

        return {
            "selected": selected,
            "rejected": rejected,
            "inferred_categories": inferred,
            "combined_prompt_block": "\n\n".join(blocks),
            "total_chars": used_chars,
        }

    def materialize_for_worktree(
        self,
        selection: dict,
        *,
        worktree_path: Path,
        runtime: str = "opencode",
        allow_scripts: bool = False,
    ) -> list[str]:
        """Materialize ONLY selected skills into `.opencode/skills/` or `.agents/skills/` inside the isolated worktree."""
        materialized: list[str] = []
        for item in selection.get("selected") or []:
            sk = self.registry.get_skill(item["skill_id"])
            res = adapt_skill_for_runtime(
                sk,
                Path(sk["install_path"]),
                runtime=runtime,
                target_workdir=Path(worktree_path),
                allow_scripts=allow_scripts,
            )
            materialized.extend(res.get("materialized_files") or [])
        return materialized

