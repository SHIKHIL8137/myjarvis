"""Concrete provider adapters: OpenAI-compatible (OpenRouter/HF/OpenAI), Anthropic, Google Gemini, OpenCode."""
import asyncio
import json
import time
from urllib.parse import urlsplit

from .. import audit
from ..config import InternetSpec, Settings, get_provider_api_key
from ..netsec import safe_request
from .interface import GatewayEvent, GatewayRequest, ProviderCapabilities

OPENAI_COMPAT_PROVIDERS = {
    "openai": ("openai_base_url", "openai_model"),
    "openrouter": ("openrouter_base_url", "openrouter_model"),
    "huggingface": ("huggingface_base_url", "huggingface_model"),
}


class CloudGatewayError(RuntimeError):
    """Raised when cloud adapter fails or violates offline/budget/policy rules."""


def parse_model_key(model_key: str, settings: Settings) -> tuple[str, str]:
    """Return (provider_name, model_id) from qualified key or local brain config."""
    k = (model_key or settings.boot_brain).strip()
    for prov in ("openrouter", "huggingface", "gemini", "openai", "anthropic", "opencode"):
        if k.startswith(prov + ":"):
            return prov, k.split(":", 1)[1]
    if k in settings.brains:
        return "ollama", settings.brains[k].model
    return "ollama", k


def check_cloud_egress_allowed(provider: str, settings: Settings) -> str:
    """Enforce Local Mode gate + allow_cloud + non-empty API key before any external LLM request."""
    if settings.offline_enforced and not (settings.internet.enabled and audit.is_online_enabled()):
        raise CloudGatewayError(f"Cloud provider {provider!r} is blocked in Local Mode.")
    if not settings.providers.allow_cloud:
        raise CloudGatewayError(f"Cloud provider {provider!r} disabled (providers.allow_cloud=false).")
    key = get_provider_api_key(provider)
    if not key:
        raise CloudGatewayError(f"API key for provider {provider!r} is not configured.")
    return key


class OpenAICompatAdapter:
    """Hardened OpenAI-schema adapter for OpenAI, OpenRouter, and Hugging Face Inference Providers."""

    def __init__(self, provider_name: str, base_url: str, allowed_hosts: set[str] | None = None):
        self.name = provider_name
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calling=True, json_mode=True, local=False, max_ctx=32768)

    async def complete(self, req: GatewayRequest, *, api_key: str, model_id: str, max_fetch_bytes: int = 512_000):
        url = self.base_url + "/chat/completions"
        host = (urlsplit(url).hostname or "").lower()
        allow = self.allowed_hosts or ({host} if host else set())
        if host not in allow:
            raise CloudGatewayError(f"Host {host!r} not in providers.allowed_cloud_hosts")

        headers = {"Authorization": f"Bearer {api_key}"}
        if self.name == "openrouter":
            headers["HTTP-Referer"] = "https://localhost.jarvis.local"
            headers["X-Title"] = "Jarvis Personal AI OS"

        body: dict = {"model": model_id, "messages": req.messages, "max_tokens": req.max_tokens, "temperature": req.temperature}
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        if req.tools:
            body["tools"] = req.tools

        net_spec = InternetSpec(enabled=True, timeout_s=30.0, max_fetch_bytes=max_fetch_bytes)
        t0 = time.perf_counter()
        res = await safe_request("POST", url, net_spec, allowed_hosts=allow, json_body=body, headers=headers)
        if res.status_code >= 400:
            raise CloudGatewayError(f"{self.name} HTTP {res.status_code}: {res.body.decode('utf-8', errors='replace')[:240]}")
        data = json.loads(res.body.decode("utf-8"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        txt = msg.get("content") or ""
        usage = data.get("usage") or {}
        lat_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if txt:
            yield GatewayEvent(type="token", text=txt)
        if msg.get("tool_calls"):
            yield GatewayEvent(type="tool_calls", calls=list(msg["tool_calls"]))
        yield GatewayEvent(
            type="done",
            usage={
                "provider": self.name,
                "model": model_id,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or len(txt.split())),
                "latency_ms": lat_ms,
            },
        )


class GeminiAdapter:
    """Hardened Google Gemini API adapter passing key via `x-goog-api-key` header (never query string)."""

    name = "gemini"

    def __init__(self, base_url: str, allowed_hosts: set[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calling=False, json_mode=True, local=False, max_ctx=131072)

    async def complete(self, req: GatewayRequest, *, api_key: str, model_id: str, max_fetch_bytes: int = 512_000):
        url = f"{self.base_url}/models/{model_id}:generateContent"
        host = (urlsplit(url).hostname or "").lower()
        allow = self.allowed_hosts or ({host} if host else set())
        sys_parts = [m.get("content", "") for m in req.messages if m.get("role") == "system"]
        contents = [
            {"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": str(m.get("content", ""))}]}
            for m in req.messages
            if m.get("role") != "system"
        ]
        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": req.max_tokens, "temperature": req.temperature},
        }
        if sys_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}
        if req.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"

        net_spec = InternetSpec(enabled=True, timeout_s=30.0, max_fetch_bytes=max_fetch_bytes)
        t0 = time.perf_counter()
        res = await safe_request(
            "POST",
            url,
            net_spec,
            allowed_hosts=allow,
            json_body=body,
            headers={"x-goog-api-key": api_key},
        )
        if res.status_code >= 400:
            raise CloudGatewayError(f"gemini HTTP {res.status_code}: {res.body.decode('utf-8', errors='replace')[:240]}")
        data = json.loads(res.body.decode("utf-8"))
        cands = data.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
        txt = "".join(str(p.get("text") or "") for p in parts)
        meta = data.get("usageMetadata") or {}
        lat_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if txt:
            yield GatewayEvent(type="token", text=txt)
        yield GatewayEvent(
            type="done",
            usage={
                "provider": "gemini",
                "model": model_id,
                "prompt_tokens": int(meta.get("promptTokenCount") or 0),
                "completion_tokens": int(meta.get("candidatesTokenCount") or len(txt.split())),
                "latency_ms": lat_ms,
            },
        )


class AnthropicAdapter:
    """Hardened Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(self, base_url: str, allowed_hosts: set[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calling=True, json_mode=False, local=False, max_ctx=200000)

    async def complete(self, req: GatewayRequest, *, api_key: str, model_id: str, max_fetch_bytes: int = 512_000):
        url = self.base_url + "/messages"
        host = (urlsplit(url).hostname or "").lower()
        allow = self.allowed_hosts or ({host} if host else set())
        sys_msg = "\n".join(m.get("content", "") for m in req.messages if m.get("role") == "system")
        chat_msgs = [{"role": m["role"], "content": m["content"]} for m in req.messages if m.get("role") != "system"]
        net_spec = InternetSpec(enabled=True, timeout_s=30.0, max_fetch_bytes=max_fetch_bytes)
        t0 = time.perf_counter()
        res = await safe_request(
            "POST",
            url,
            net_spec,
            allowed_hosts=allow,
            json_body={"model": model_id, "system": sys_msg, "messages": chat_msgs, "max_tokens": req.max_tokens},
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        if res.status_code >= 400:
            raise CloudGatewayError(f"anthropic HTTP {res.status_code}: {res.body.decode('utf-8', errors='replace')[:240]}")
        data = json.loads(res.body.decode("utf-8"))
        txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        lat_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if txt:
            yield GatewayEvent(type="token", text=txt)
        yield GatewayEvent(
            type="done",
            usage={
                "provider": "anthropic",
                "model": model_id,
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or len(txt.split())),
                "latency_ms": lat_ms,
            },
        )


async def opencode_provider_bridge(prompt_text: str, *, workdir: str, model: str | None = None, bin_name: str = "opencode", timeout_s: int = 60) -> str:
    """Delegate a single non-interactive coding turn to `opencode run --format json` when opted in."""
    from ..integrations.opencode_adapter import run_opencode_task
    res = await asyncio.to_thread(run_opencode_task, prompt_text, workdir=workdir, model=model, bin_name=bin_name, timeout_s=timeout_s)
    return str(res.get("final_text") or res.get("stdout") or "")

