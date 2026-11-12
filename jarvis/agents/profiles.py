"""Markdown agent profile loader and workforce coordinator for P3.

Loads profiles from:
- Built-in profiles: `jarvis/agents/builtin_agents/*.md`
- User global profiles: `data/agents/*.md` (or settings.agents_dir)
- Project-local profiles: `.jarvis/agents/*.md` or `.agents/*.md`

Supports YAML frontmatter parsing, Pydantic validation, diagnostic skipping of malformed
files, deterministic tool filtering, and bounded delegation without privilege escalation.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any
import yaml

from ..core.contracts import AgentProfileContract

log = logging.getLogger("jarvis.agents.profiles")

_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_agent_profile_markdown(
    file_path: Path,
    *,
    default_scope: str = "builtin",
) -> tuple[AgentProfileContract | None, list[str]]:
    """Parse a Markdown file with YAML frontmatter into an AgentProfileContract.

    Returns (contract, diagnostics). If the file is malformed or invalid, contract is None
    or marked invalid and diagnostics contains human-readable reasons so it is skipped safely.
    """
    diagnostics: list[str] = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        diagnostics.append(f"Failed to read file {file_path.name}: {exc}")
        return None, diagnostics

    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        diagnostics.append(f"Missing or malformed YAML frontmatter in {file_path.name}")
        return None, diagnostics

    frontmatter_raw, _body_raw = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(frontmatter_raw)
    except Exception as exc:
        diagnostics.append(f"Invalid YAML in frontmatter of {file_path.name}: {exc}")
        return None, diagnostics

    if not isinstance(data, dict):
        diagnostics.append(f"Frontmatter in {file_path.name} is not a YAML dictionary")
        return None, diagnostics

    name = str(data.get("name") or file_path.stem).strip()
    scope = str(data.get("scope") or default_scope).strip()

    try:
        profile = AgentProfileContract(
            name=name,
            description=str(data.get("description") or "").strip(),
            scope=scope,
            source_path=str(file_path.resolve().as_posix()),
            model_role=str(data.get("model_role") or "fast").strip(),
            permission_tier=str(data.get("permission_tier") or "READ").strip().upper(),
            tools_allowlist=[str(x) for x in (data.get("tools_allowlist") or [])],
            tools_denylist=[str(x) for x in (data.get("tools_denylist") or [])],
            max_steps=int(data.get("max_steps") or 12),
            max_tokens=int(data.get("max_tokens") or 32000),
            can_delegate=bool(data.get("can_delegate", False)),
            max_delegation_depth=int(data.get("max_delegation_depth") or 1),
            valid=True,
            diagnostics=[],
        )
        return profile, []
    except Exception as exc:
        diagnostics.append(f"Validation error in {file_path.name}: {exc}")
        return None, diagnostics


class AgentWorkforceCoordinator:
    """Manages loading, validation, diagnostic reporting, and bounded delegation across agent profiles."""

    def __init__(
        self,
        *,
        builtin_dir: Path | None = None,
        user_dir: Path | None = None,
        project_dirs: list[Path] | None = None,
    ):
        self.builtin_dir = builtin_dir or (Path(__file__).parent / "builtin_agents")
        self.user_dir = user_dir
        self.project_dirs = project_dirs or []
        self._profiles: dict[str, AgentProfileContract] = {}
        self._diagnostics: list[dict[str, Any]] = []
        self.reload_profiles()

    def reload_profiles(self) -> dict[str, AgentProfileContract]:
        """Discover and load agent profiles across hierarchy with project > user > builtin precedence."""
        self._profiles.clear()
        self._diagnostics.clear()

        # 1. Built-in
        if self.builtin_dir and self.builtin_dir.is_dir():
            for p in sorted(self.builtin_dir.glob("*.md")):
                prof, diags = parse_agent_profile_markdown(p, default_scope="builtin")
                if prof:
                    self._profiles[prof.name] = prof
                for d in diags:
                    self._diagnostics.append({"path": str(p), "diagnostic": d, "scope": "builtin"})

        # 2. User
        if self.user_dir and self.user_dir.is_dir():
            for p in sorted(self.user_dir.glob("*.md")):
                prof, diags = parse_agent_profile_markdown(p, default_scope="user")
                if prof:
                    self._profiles[prof.name] = prof
                for d in diags:
                    self._diagnostics.append({"path": str(p), "diagnostic": d, "scope": "user"})

        # 3. Project directories
        for proj in self.project_dirs:
            for sub in (".jarvis/agents", ".agents"):
                cand = proj / sub
                if cand.is_dir():
                    for p in sorted(cand.glob("*.md")):
                        prof, diags = parse_agent_profile_markdown(p, default_scope="project")
                        if prof:
                            self._profiles[prof.name] = prof
                        for d in diags:
                            self._diagnostics.append({"path": str(p), "diagnostic": d, "scope": "project"})

        return self._profiles

    def list_profiles(self, *, scope: str | None = None) -> list[AgentProfileContract]:
        """List active profiles optionally filtered by scope."""
        res = list(self._profiles.values())
        if scope:
            res = [p for p in res if p.scope == scope]
        res.sort(key=lambda x: (x.scope != "project", x.scope != "user", x.name))
        return res

    def get_profile(self, name: str) -> AgentProfileContract | None:
        """Fetch profile by exact name."""
        return self._profiles.get(name)

    def get_diagnostics(self) -> list[dict[str, Any]]:
        """Return diagnostic messages for invalid/skipped profile files."""
        return list(self._diagnostics)

    def filter_tools_for_agent(self, agent_name: str, available_tools: list[str]) -> list[str]:
        """Deterministically apply allowlist and denylist to available tools for the specified agent."""
        prof = self.get_profile(agent_name)
        if not prof:
            return available_tools
        allowed = list(available_tools)
        if prof.tools_allowlist:
            allow_set = set(prof.tools_allowlist)
            allowed = [t for t in allowed if t in allow_set]
        if prof.tools_denylist:
            deny_set = set(prof.tools_denylist)
            allowed = [t for t in allowed if t not in deny_set]
        return allowed

    def can_delegate_to(
        self,
        delegator_name: str,
        target_name: str,
        *,
        current_depth: int = 0,
    ) -> tuple[bool, str]:
        """Enforce bounded delegation depth and prevent privilege escalation."""
        delegator = self.get_profile(delegator_name)
        if not delegator:
            return False, f"Delegator agent '{delegator_name}' profile not found"
        if not delegator.can_delegate:
            return False, f"Agent '{delegator_name}' is not authorized to delegate tasks"
        if current_depth >= delegator.max_delegation_depth:
            return False, f"Delegation depth limit reached ({current_depth}/{delegator.max_delegation_depth})"

        target = self.get_profile(target_name)
        if not target:
            return False, f"Target agent '{target_name}' profile not found"

        # Privilege escalation prevention:
        # Tiers hierarchy: READ < PROMPT < EXECUTE < ADMIN
        tier_order = {"READ": 1, "PROMPT": 2, "EXECUTE": 3, "ADMIN": 4}
        del_level = tier_order.get(delegator.permission_tier, 1)
        tgt_level = tier_order.get(target.permission_tier, 1)
        if tgt_level > del_level:
            return (
                False,
                f"Privilege escalation prevented: '{delegator_name}' ({delegator.permission_tier}) "
                f"cannot delegate to higher tier '{target_name}' ({target.permission_tier})",
            )

        return True, "Delegation authorized"

    def select_profile(self, *, intent: str = "", query: str = "") -> AgentProfileContract | None:
        """Deterministically choose the best matching agent profile for an intent or query."""
        combined = f"{intent} {query}".lower()
        if any(k in combined for k in ("research", "verify", "cite", "citation", "evidence", "investigate")):
            if "research_specialist" in self._profiles:
                return self._profiles["research_specialist"]
        if any(k in combined for k in ("code", "refactor", "dev", "patch", "repo", "git", "bug", "test", "workforce")):
            if "dev_coordinator" in self._profiles:
                return self._profiles["dev_coordinator"]
        return self._profiles.get("assistant_general") or next(iter(self._profiles.values()), None)

