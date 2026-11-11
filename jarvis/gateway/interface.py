"""Model Gateway protocol interfaces and normalized DTOs."""
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool = True
    tool_calling: bool = False
    vision: bool = False
    json_mode: bool = False
    local: bool = True
    max_ctx: int = 4096


@dataclass
class GatewayRequest:
    model_key: str  # e.g. "local", "openrouter:qwen/qwen3-coder:free", "gemini:gemini-2.5-flash"
    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.6
    tools: list[dict] | None = None
    json_mode: bool = False
    session_id: str | None = None
    run_id: str | None = None
    decision_layer: int = 3
    scrub_secrets: bool = True


@dataclass
class GatewayEvent:
    type: str  # "token" | "tool_calls" | "done" | "error"
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class ModelProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...
    async def complete(self, req: GatewayRequest, *, api_key: str | None = None) -> AsyncIterator[GatewayEvent]: ...

