"""Model Gateway protocol interfaces, dynamic capability descriptors, and normalized DTOs."""
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str = "ollama"
    model_id: str = ""
    streaming: bool = True
    tool_calling: bool = False
    vision: bool = False
    json_mode: bool = False
    embeddings: bool = False
    local: bool = True
    max_ctx: int = 4096
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    rpm_limit: int = 60
    pricing_tier: str = "local"  # "local" | "free" | "paid"
    cost_per_1k_usd: float = 0.0
    free_tier_available: bool = True
    last_verified_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "streaming": self.streaming,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "json_mode": self.json_mode,
            "embeddings": self.embeddings,
            "local": self.local,
            "max_ctx": self.max_ctx,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "rpm_limit": self.rpm_limit,
            "pricing_tier": self.pricing_tier,
            "cost_per_1k_usd": self.cost_per_1k_usd,
            "free_tier_available": self.free_tier_available,
            "last_verified_at": self.last_verified_at,
        }


@dataclass
class GatewayRequest:
    model_key: str  # e.g. "local", "openrouter:qwen/qwen3-coder:free", "gemini:gemini-2.5-flash"
    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.6
    tools: list[dict] | None = None
    json_mode: bool = False
    require_tools: bool = False
    require_vision: bool = False
    require_streaming: bool = False
    session_id: str | None = None
    run_id: str | None = None
    decision_layer: int = 3
    project_privacy_level: str = "private"  # public | internal | private | confidential | restricted
    scrub_secrets: bool = True
    firewall_override_authorized_by: str | None = None
    firewall_override_reason: str = ""
    timeout_s: float | None = None
    cancel_event: asyncio.Event | None = None


@dataclass
class GatewayEvent:
    type: str  # "token" | "tool_calls" | "done" | "error"
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class ModelProvider(Protocol):
    name: str

    def capabilities(self, model_id: str = "") -> ProviderCapabilities: ...
    async def health_check(self, api_key: str | None = None, transport_factory=None) -> dict: ...
    async def complete(
        self,
        req: GatewayRequest,
        *,
        api_key: str | None = None,
        model_id: str = "",
        max_fetch_bytes: int = 512_000,
        transport_factory=None,
    ) -> AsyncIterator[GatewayEvent]: ...
