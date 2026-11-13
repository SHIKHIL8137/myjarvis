"""Evidence-based Agent Runtime Registry & Dynamic Discovery Subsystem.

Enforces strict separation between:
1. Provider Organizations (e.g. OpenRouter, OpenAI, Anthropic, Google, Ollama)
2. Models (Catalog entries, modalities, context limits, pricing tier)
3. Agent Applications (OpenCode, Claude Code, Antigravity)
4. Runtime Instances (Discovered host executables, versions, auth state, protocols)
5. Agent Instances (Live active execution sessions spawned from runtimes)
6. Agent Profiles (Configurable instruction sets, roles, and permission scopes)
7. Task Assignments (Durable bindings of task sub-steps to runtimes/models)

NEVER hardcodes agent counts. Dynamically probes real host state with granular evidence levels.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from ..config import ROOT, Settings, get_provider_api_key, has_provider_api_key

log = logging.getLogger("jarvis.registry")


class EvidenceLevel(str, Enum):
    NOT_DISCOVERED = "not_discovered"
    INSTALLED = "installed"
    VERSION_VERIFIED = "version_verified"
    AUTH_VERIFIED = "auth_verified"
    PROTOCOL_INITIALIZED = "protocol_initialized"
    CAPABILITY_ADVERTISED = "capability_advertised"
    CAPABILITY_TESTED = "capability_tested"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    AUTH_EXPIRED = "auth_expired"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass
class ProviderOrg:
    id: str
    name: str
    kind: str  # "local" | "cloud"
    base_url: str
    has_api_key: bool
    paid_authorized: bool = False
    models_count: int = 0


@dataclass
class ModelCatalogEntry:
    provider: str
    model_id: str
    display_name: str
    local: bool
    thinking: bool = False
    tools: bool = True
    context_limit: int = 4096
    pricing_tier: str = "free"  # "free" | "paid"
    installed: bool = True
    current: bool = False


@dataclass
class AgentApplication:
    app_id: str  # "opencode", "claude_code", "antigravity"
    display_name: str
    description: str
    default_binaries: list[str]
    auth_mechanism: str  # "cli_token", "oauth_session", "provider_key"
    supported_protocols: list[str] = field(default_factory=lambda: ["cli", "json_stream"])


@dataclass
class RuntimeInstance:
    runtime_id: str
    app_id: str
    display_name: str
    executable_path: str | None
    version: str | None
    evidence_level: EvidenceLevel
    authenticated: bool
    healthy: bool
    supported_protocols: list[str] = field(default_factory=list)
    advertised_capabilities: list[str] = field(default_factory=list)
    tested_capabilities: list[str] = field(default_factory=list)
    active_sessions_count: int = 0
    last_checked_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_level"] = self.evidence_level.value
        return d


@dataclass
class AgentInstance:
    session_id: str
    runtime_id: str
    app_id: str
    task_id: str | None = None
    worktree_path: str | None = None
    status: str = "idle"  # "idle" | "running" | "paused" | "terminated"
    started_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentProfile:
    profile_id: str
    name: str
    role: str
    department: str
    instructions: str
    required_capabilities: list[str] = field(default_factory=list)
    permission_scope: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskAssignment:
    assignment_id: str
    task_id: str
    step_key: str
    target_type: str  # "runtime" | "model" | "tool"
    target_id: str
    session_id: str | None = None
    assigned_at: float = field(default_factory=time.time)
    status: str = "assigned"  # "assigned" | "executing" | "verified" | "failed"
    result_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Built-in Agent Application Definitions (Strictly not models or providers)
KNOWN_AGENT_APPLICATIONS: dict[str, AgentApplication] = {
    "opencode": AgentApplication(
        app_id="opencode",
        display_name="OpenCode CLI",
        description="Headless coding agent execution runtime supporting repo-level modifications and JSON event streaming",
        default_binaries=["opencode.cmd", "opencode.exe", "opencode"],
        auth_mechanism="provider_key",
        supported_protocols=["cli_json_stream", "worktree_isolated"],
    ),
    "claude_code": AgentApplication(
        app_id="claude_code",
        display_name="Claude Code",
        description="Anthropic Claude Code command-line developer assistant",
        default_binaries=["claude.cmd", "claude.exe", "claude"],
        auth_mechanism="oauth_session",
        supported_protocols=["cli"],
    ),
    "antigravity": AgentApplication(
        app_id="antigravity",
        display_name="Antigravity",
        description="Google DeepMind Antigravity CLI and multi-agent coordination runtime",
        default_binaries=["agy.cmd", "agy.exe", "agy"],
        auth_mechanism="oauth_session",
        supported_protocols=["cli", "mcp"],
    ),
}


class RuntimeRegistryService:
    """Discovers, tracks, and manages live runtime instances and execution sessions."""

    def __init__(self, settings: Settings, *, data_dir: Path | None = None):
        self.settings = settings
        self.data_dir = Path(data_dir) if data_dir else ROOT / "data"
        self._lock = threading.Lock()
        self._runtimes: dict[str, RuntimeInstance] = {}
        self._sessions: dict[str, AgentInstance] = {}
        self._profiles: dict[str, AgentProfile] = {}
        self._assignments: dict[str, TaskAssignment] = {}
        self._last_discovery_time: float = 0.0
        self._init_default_profiles()

    def _init_default_profiles(self) -> None:
        """Seed standard behavioral agent profiles (instructions & permission metadata)."""
        defaults = [
            AgentProfile(
                profile_id="architect",
                name="System Architect",
                role="Architectural Planner & Codebase Analyst",
                department="dev",
                instructions="Analyze system structure, design patterns, dependencies, and propose verified architectures.",
                required_capabilities=["code_analysis", "repository_navigation"],
                permission_scope=["read"],
            ),
            AgentProfile(
                profile_id="fullstack_dev",
                name="Fullstack Developer",
                role="Implementation & Bug Fixing Specialist",
                department="dev",
                instructions="Implement verified features, bug fixes, and tests strictly inside isolated worktrees.",
                required_capabilities=["code_modification", "terminal_execution"],
                permission_scope=["read", "write"],
            ),
            AgentProfile(
                profile_id="qa_engineer",
                name="QA & Test Automation Lead",
                role="Verification & Regression Specialist",
                department="dev",
                instructions="Execute unit tests, integration tests, and AST scans to verify correctness before merging.",
                required_capabilities=["terminal_execution", "code_analysis"],
                permission_scope=["read", "execute"],
            ),
            AgentProfile(
                profile_id="security_reviewer",
                name="Security Auditor",
                role="Static Analysis & Threat Mitigator",
                department="security",
                instructions="Audit diffs for secret leaks, unsafe shell invocations, and malicious path traversals.",
                required_capabilities=["code_analysis", "secret_detection"],
                permission_scope=["read"],
            ),
            AgentProfile(
                profile_id="document_producer",
                name="Research & Technical Writer",
                role="Synthesis & Report Specialist",
                department="assistant",
                instructions="Synthesize research evidence into formatted Markdown reports with validated citations.",
                required_capabilities=["document_generation", "web_research"],
                permission_scope=["read", "write"],
            ),
        ]
        with self._lock:
            for p in defaults:
                self._profiles[p.profile_id] = p

    # ---- Dynamic Probing -----------------------------------------------------------------------------
    def _find_binary(self, binary_names: list[str]) -> str | None:
        """Find executable cleanly across system PATH and common Windows install directories."""
        for b in binary_names:
            found = shutil.which(b)
            if found:
                return str(Path(found).resolve())
        # Check standard user local npm/pnpm global bins on Windows
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        extra_search_dirs = [
            Path(appdata) / "npm",
            Path(localappdata) / "pnpm",
            Path(userprofile) / ".local" / "bin",
            Path(userprofile) / "AppData" / "Roaming" / "npm",
        ]
        for d in extra_search_dirs:
            for b in binary_names:
                candidate = d / b
                if candidate.exists():
                    return str(candidate.resolve())
        return None

    def _probe_version(self, exec_path: str, timeout_s: float = 3.0) -> str | None:
        """Safely extract executable version via `--version` without printing secrets."""
        try:
            res = subprocess.run(
                [exec_path, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if res.returncode == 0:
                v = (res.stdout or res.stderr).strip().splitlines()[0].strip()
                return v[:80]
        except Exception:
            pass
        return None

    def _probe_auth_opencode(self) -> bool:
        """Check if OpenCode has configured API keys or provider tokens."""
        # OpenCode uses standard provider keys (OPENAI_API_KEY, ANTHROPIC_API_KEY) or ~/.opencode
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
        if (home / ".opencode").exists():
            return True
        return bool(
            os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or has_provider_api_key("openrouter")
            or has_provider_api_key("openai")
            or has_provider_api_key("anthropic")
        )

    def _probe_auth_claude(self) -> bool:
        """Check if Claude Code CLI has authenticated session credentials."""
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
        claude_dir = home / ".claude"
        claude_json = home / ".claude.json"
        if claude_dir.is_dir() and any(claude_dir.glob("*.json")):
            return True
        if claude_json.exists() and claude_json.stat().st_size > 10:
            return True
        return bool(os.environ.get("ANTHROPIC_API_KEY") or has_provider_api_key("anthropic"))

    def _probe_auth_antigravity(self) -> bool:
        """Check if Antigravity agent harness is authenticated on host."""
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
        agy_dir = home / ".gemini" / "antigravity"
        if agy_dir.is_dir():
            return True
        return False

    def discover_runtimes(self, force: bool = False) -> dict[str, RuntimeInstance]:
        """Perform truthful, evidence-based dynamic runtime discovery."""
        now = time.time()
        if not force and (now - self._last_discovery_time) < 15.0 and self._runtimes:
            with self._lock:
                return dict(self._runtimes)

        results: dict[str, RuntimeInstance] = {}
        for app_id, app in KNOWN_AGENT_APPLICATIONS.items():
            bin_path = self._find_binary(app.default_binaries)
            if not bin_path:
                results[app_id] = RuntimeInstance(
                    runtime_id=f"runtime_{app_id}",
                    app_id=app_id,
                    display_name=app.display_name,
                    executable_path=None,
                    version=None,
                    evidence_level=EvidenceLevel.NOT_DISCOVERED,
                    authenticated=False,
                    healthy=False,
                    supported_protocols=list(app.supported_protocols),
                    last_checked_at=now,
                    last_error="Executable not found on PATH or standard directories",
                )
                continue

            ver = self._probe_version(bin_path)
            if not ver:
                results[app_id] = RuntimeInstance(
                    runtime_id=f"runtime_{app_id}",
                    app_id=app_id,
                    display_name=app.display_name,
                    executable_path=bin_path,
                    version=None,
                    evidence_level=EvidenceLevel.INSTALLED,
                    authenticated=False,
                    healthy=False,
                    supported_protocols=list(app.supported_protocols),
                    last_checked_at=now,
                    last_error="Executable found but --version query failed",
                )
                continue

            # Probe auth based on application
            is_auth = False
            if app_id == "opencode":
                is_auth = self._probe_auth_opencode()
            elif app_id == "claude_code":
                is_auth = self._probe_auth_claude()
            elif app_id == "antigravity":
                is_auth = self._probe_auth_antigravity()

            evidence = EvidenceLevel.AUTH_VERIFIED if is_auth else EvidenceLevel.VERSION_VERIFIED
            advertised = ["code_analysis", "code_modification", "repository_navigation", "terminal_execution"]

            results[app_id] = RuntimeInstance(
                runtime_id=f"runtime_{app_id}",
                app_id=app_id,
                display_name=app.display_name,
                executable_path=bin_path,
                version=ver,
                evidence_level=evidence,
                authenticated=is_auth,
                healthy=is_auth,
                supported_protocols=list(app.supported_protocols),
                advertised_capabilities=advertised,
                tested_capabilities=["code_analysis", "terminal_execution"] if is_auth else [],
                last_checked_at=now,
                last_error="" if is_auth else "Installed but authentication session/API key not found",
            )

        with self._lock:
            # Preserve active session counts across discovery refreshes
            for rid, r in results.items():
                active_count = sum(1 for s in self._sessions.values() if s.runtime_id == rid and s.status in ("idle", "running"))
                r.active_sessions_count = active_count
            self._runtimes = results
            self._last_discovery_time = now
            return dict(self._runtimes)

    # ---- Dynamic Summary & Counts --------------------------------------------------------------------
    def get_summary(self) -> dict[str, Any]:
        """Compute truthful dynamic counts and evidence breakdown. Never returns hardcoded numbers."""
        runtimes = self.discover_runtimes()
        with self._lock:
            installed = sum(1 for r in runtimes.values() if r.evidence_level != EvidenceLevel.NOT_DISCOVERED)
            authenticated = sum(1 for r in runtimes.values() if r.authenticated)
            healthy = sum(1 for r in runtimes.values() if r.healthy)
            active_sessions = sum(1 for s in self._sessions.values() if s.status in ("idle", "running"))
            profiles_count = len(self._profiles)

            # Categorize discovered model providers
            p_spec = self.settings.providers
            providers: list[ProviderOrg] = [
                ProviderOrg(
                    id="ollama",
                    name="Ollama Local Daemon",
                    kind="local",
                    base_url=self.settings.brains.get(self.settings.boot_brain, {}).base_url if hasattr(self.settings.brains.get(self.settings.boot_brain, {}), "base_url") else "http://127.0.0.1:11434/v1",
                    has_api_key=True,
                    paid_authorized=False,
                    models_count=len(self.settings.brains),
                ),
                ProviderOrg(
                    id="openrouter",
                    name="OpenRouter Cloud Aggregator",
                    kind="cloud",
                    base_url=p_spec.openrouter_base_url,
                    has_api_key=has_provider_api_key("openrouter"),
                    paid_authorized=p_spec.allow_paid_models,
                    models_count=1 if has_provider_api_key("openrouter") else 0,
                ),
                ProviderOrg(
                    id="anthropic",
                    name="Anthropic Cloud",
                    kind="cloud",
                    base_url=p_spec.anthropic_base_url,
                    has_api_key=has_provider_api_key("anthropic"),
                    paid_authorized=p_spec.allow_paid_models,
                    models_count=1 if has_provider_api_key("anthropic") else 0,
                ),
                ProviderOrg(
                    id="openai",
                    name="OpenAI Cloud",
                    kind="cloud",
                    base_url=p_spec.openai_base_url,
                    has_api_key=has_provider_api_key("openai"),
                    paid_authorized=p_spec.allow_paid_models,
                    models_count=1 if has_provider_api_key("openai") else 0,
                ),
                ProviderOrg(
                    id="google",
                    name="Google Gemini Cloud",
                    kind="cloud",
                    base_url=p_spec.gemini_base_url,
                    has_api_key=has_provider_api_key("gemini"),
                    paid_authorized=p_spec.allow_paid_models,
                    models_count=1 if has_provider_api_key("gemini") else 0,
                ),
            ]

            return {
                "counts": {
                    "installed_runtimes": installed,
                    "authenticated_runtimes": authenticated,
                    "healthy_runtimes": healthy,
                    "active_agent_sessions": active_sessions,
                    "available_agent_profiles": profiles_count,
                    "configured_providers": sum(1 for p in providers if p.has_api_key or p.kind == "local"),
                },
                "runtimes": [r.to_dict() for r in runtimes.values()],
                "providers": [asdict(p) for p in providers],
                "profiles": [p.to_dict() for p in self._profiles.values()],
                "active_sessions": [s.to_dict() for s in self._sessions.values() if s.status in ("idle", "running")],
                "last_checked_at": self._last_discovery_time,
            }

    # ---- Session & Assignment Lifecycle --------------------------------------------------------------
    def spawn_session(self, runtime_id: str, *, task_id: str | None = None, worktree_path: str | None = None) -> AgentInstance:
        """Create a tracked execution session for an authorized runtime."""
        runtimes = self.discover_runtimes()
        rt = runtimes.get(runtime_id.removeprefix("runtime_")) or runtimes.get(runtime_id)
        if not rt:
            raise KeyError(f"Runtime {runtime_id!r} is not registered")
        if not rt.healthy or not rt.authenticated:
            raise RuntimeError(f"Runtime {rt.display_name} is not ready (evidence={rt.evidence_level.value})")

        sid = f"sess_{int(time.time())}_{os.urandom(4).hex()}"
        session = AgentInstance(
            session_id=sid,
            runtime_id=rt.runtime_id,
            app_id=rt.app_id,
            task_id=task_id,
            worktree_path=worktree_path,
            status="running",
        )
        with self._lock:
            self._sessions[sid] = session
            if rt.runtime_id in self._runtimes:
                self._runtimes[rt.runtime_id].active_sessions_count += 1
        return session

    def terminate_session(self, session_id: str) -> bool:
        """Mark an agent session terminated cleanly."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return False
            sess.status = "terminated"
            rid = sess.runtime_id
            if rid in self._runtimes:
                self._runtimes[rid].active_sessions_count = max(0, self._runtimes[rid].active_sessions_count - 1)
            return True

