"""Provider reliability with mocked HTTP (no network, no key, no billing): auth failure, rate limit, timeout, network failure, SSE parsing,
usage/latency reporting, secret hygiene, offline fallback to the local model, and provider health/gating."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis import audit
from jarvis.brain import Brain
from jarvis.config import load_settings
from jarvis.gateway.gateway import ModelGateway
from jarvis.gateway.interface import GatewayRequest
from jarvis.gateway.providers import CloudGatewayError

KEY = "sk-or-v1-UNITTESTKEYUNITTESTKEY1234567890"
SSE_OK = (
    'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"world"}}],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


class _LocalBrain(Brain):
    async def health(self):
        return {"up": True, "models": ["qwen3:4b-instruct"], "loaded": []}

    async def stream_events(self, messages, tools=None, **kw):
        yield {"type": "token", "text": "local answer"}
        self.last_stats = {"tokens": 2, "model": "qwen3:4b-instruct"}


@pytest.fixture
def cloud(monkeypatch):
    s = load_settings()
    s.offline_enforced = False
    s.internet.enabled = True
    s.providers.allow_cloud = True
    s.providers.allow_paid_models = True
    s.gateway.retry_max_attempts = 1
    s.gateway.retry_base_delay_s = 0.0
    audit.set_online_enabled(True)
    monkeypatch.setattr("jarvis.gateway.providers.get_provider_api_key", lambda p: KEY)
    yield s
    audit.set_online_enabled(False)


def _factory(handler):
    return lambda target: httpx.MockTransport(handler)


def _run(gw, req, fallbacks=None, factory=None):
    async def go():
        evs = []
        async for e in gw.stream_with_fallback(req, fallback_models=fallbacks or [], transport_factory=factory):
            evs.append(e)
        return evs

    return asyncio.run(go())


def _req(model="openrouter:qwen/qwen3-coder"):
    return GatewayRequest(model_key=model, messages=[{"role": "user", "content": "hi"}])


def test_success_streams_tokens_and_reports_usage_and_latency(cloud):
    gw = ModelGateway(_LocalBrain(cloud), None, settings=cloud)
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, text=SSE_OK, headers={"content-type": "text/event-stream"})

    evs = _run(gw, _req(), factory=_factory(handler))
    assert "".join(e.text for e in evs if e.type == "token") == "Hello world"
    done = next(e for e in evs if e.type == "done")
    assert done.usage["prompt_tokens"] == 7 and done.usage["completion_tokens"] == 2 and done.usage["latency_ms"] >= 0 and done.usage["provider"] == "openrouter"
    assert seen["url"].startswith("https://openrouter.ai/") and seen["auth"] == f"Bearer {KEY}"  # key only in the auth header


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (503, True)])
def test_http_errors_are_classified_and_never_leak_the_key(cloud, status, retryable):
    gw = ModelGateway(_LocalBrain(cloud), None, settings=cloud)
    handler = lambda request: httpx.Response(status, json={"error": {"message": f"upstream said {status} for key {KEY}"}})  # noqa: E731
    with pytest.raises(CloudGatewayError) as ei:
        _run(gw, _req(), factory=_factory(handler))
    assert ei.value.retryable is retryable
    assert KEY not in str(ei.value) and "UNITTESTKEY" not in str(ei.value)


def test_timeout_and_connection_failure_are_reported_as_gateway_errors(cloud):
    gw = ModelGateway(_LocalBrain(cloud), None, settings=cloud)

    def timeout(request):
        raise httpx.ReadTimeout("Timeout reading response")

    def refused(request):
        raise httpx.ConnectError("connection refused")

    for h in (timeout, refused):
        with pytest.raises(CloudGatewayError):
            _run(gw, _req(), factory=_factory(h))


def test_cloud_failure_falls_back_to_the_local_model(cloud):
    gw = ModelGateway(_LocalBrain(cloud), None, settings=cloud)
    handler = lambda request: httpx.Response(503, text="unavailable")  # noqa: E731
    evs = _run(gw, _req(), fallbacks=[cloud.boot_brain], factory=_factory(handler))
    assert "".join(e.text for e in evs if e.type == "token") == "local answer"  # the user still gets an answer, offline-capable


def test_offline_mode_blocks_cloud_and_uses_local_without_any_http_call(cloud):
    cloud.offline_enforced = True
    audit.set_online_enabled(False)
    gw = ModelGateway(_LocalBrain(cloud), None, settings=cloud)
    calls = []
    handler = lambda request: calls.append(1) or httpx.Response(200, text=SSE_OK)  # noqa: E731
    evs = _run(gw, _req(), fallbacks=[cloud.boot_brain], factory=_factory(handler))
    assert calls == [] and "".join(e.text for e in evs if e.type == "token") == "local answer"


def test_health_check_gating_and_missing_key(monkeypatch):
    s = load_settings()
    gw = ModelGateway(_LocalBrain(s), None, settings=s)
    monkeypatch.setattr("jarvis.gateway.gateway.get_provider_api_key", lambda p: None)
    r = asyncio.run(gw.health_check_provider("openai"))
    assert r["ok"] is False and r["error"] == "missing API key" and r["key_configured"] is False  # no network attempted
    monkeypatch.setattr("jarvis.gateway.gateway.get_provider_api_key", lambda p: KEY)
    r = asyncio.run(gw.health_check_provider("openai"))
    assert r["ok"] is False and r["category"] == "offline_or_cloud_disabled" and KEY not in json.dumps(r)  # offline default: no outbound check
    local = asyncio.run(gw.health_check_provider("ollama"))
    assert local["ok"] is True and "qwen3:4b-instruct" in local["models"]


def test_provider_status_endpoint_never_returns_keys(settings, assistant, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.api import create_app

    monkeypatch.setattr("jarvis.api.save_overrides", lambda p, *a: None)
    monkeypatch.setattr("jarvis.config.get_provider_api_key", lambda p, *a, **k: KEY)
    with TestClient(create_app(settings, assistant)) as c:
        body = c.get("/api/v2/gateway/capabilities").text + c.get("/api/v2/settings").text
    assert KEY not in body and "UNITTESTKEY" not in body
