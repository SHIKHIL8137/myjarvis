"""Typed DTOs for the 3-Layer Fast Decision Engine."""
from dataclasses import asdict, dataclass, field


@dataclass
class DecisionEnvelope:
    layer: int  # 1, 2, or 3
    department: str = "chat"  # "assistant" | "dev" | "system" | "chat"
    intent: str = "general_chat"
    complexity: str = "standard"  # "trivial" | "standard" | "complex_reasoning" | "heavy_coding"
    needs_live_web: bool = False
    needs_project_context: bool = False
    project_hint: str | None = None
    risk_tier: str = "READ"  # "READ" | "WRITE" | "EXECUTE"
    confidence: float = 0.85
    fast_reply: str | None = None
    clarifying_question: str | None = None
    command_payload: dict | None = None
    latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoutingPlan:
    decision: DecisionEnvelope
    workflow: str  # "fast_command" | "direct_chat" | "assistant_graph" | "workforce_graph" | "clarify"
    primary_model: str
    fallback_models: list[str] = field(default_factory=list)
    agent_role: str = "assistant_coordinator"
    allowed_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "workflow": self.workflow,
            "primary_model": self.primary_model,
            "fallback_models": list(self.fallback_models),
            "agent_role": self.agent_role,
            "allowed_tools": list(self.allowed_tools),
        }

