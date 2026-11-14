"""Acceptance tests: runtime discovery (installed / missing / unavailable / unauthenticated) and model routing
(fast / advanced / coding roles, Ollama vs OpenRouter independently, fallback, retired slugs, settings with Ollama offline).
All external services are mocked; nothing touches the network."""
from __future__ import annotations

import asyncio
import json

import pytest

from jarvis import audit
from jarvis.brain import Brain, BrainError
from jarvis.config import BrainSpec, load_settings
from jarvis.core.runtime_registry import EvidenceLevel, RuntimeRegistryService
from jarvis.gateway.gateway import ModelGateway
from jarvis.gateway.interface import GatewayRequest
from jarvis.gateway.providers import CloudGatewayError, is_paid_model, parse_model_key
from jarvis.netsec import SafeHTTPResponse

# ======================= runtime discovery =======================


def _reg(tmp_path, monkeypatch, *, binary, version, auth):
    reg = RuntimeRegistryService(load_settings(), data_dir=tmp_path)
    monkeypatch.setattr(reg, "_find_binary", lambda names: binary)
    monkeypatch.setattr(reg, "_probe_version", lambda p, timeout_s=3.0: version)
    for fn in ("_probe_auth_opencode", "_probe_auth_claude", "_probe_auth_antigravity"):
        monkeypatch.setattr(reg, fn, lambda a=auth: a)
    return reg


def test_runtime_installed_and_authenticated(tmp_path, monkeypatch):
    rts = _reg(tmp_path, monkeypatch, binary="C:/bin/tool.exe", version="1.2.3", auth=True).discover_runtimes(force=True)
    assert rts and all(r.evidence_level == EvidenceLevel.AUTH_VERIFIED and r.authenticated and r.healthy and r.version == "1.2.3" for r in rts.values())


def test_runtime_missing(tmp_path, monkeypatch):
    rts = _reg(tmp_path, monkeypatch, binary=None, version=None, auth=False).discover_runtimes(force=True)
    for r in rts.values():
        assert r.evidence_level == EvidenceLevel.NOT_DISCOVERED and not r.healthy and not r.authenticated and r.executable_path is None


def test_runtime_unavailable_binary_exists_but_does_not_run(tmp_path, monkeypatch):
    rts = _reg(tmp_path, monkeypatch, binary="C:/bin/tool.exe", version=None, auth=True).discover_runtimes(force=True)
    for r in rts.values():
        assert r.evidence_level == EvidenceLevel.INSTALLED and not r.healthy and not r.authenticated  # even if creds exist: it cannot run
        assert "--version" in r.last_error


def test_runtime_installed_but_unauthenticated_is_not_healthy(tmp_path, monkeypatch):
    rts = _reg(tmp_path, monkeypatch, binary="C:/bin/tool.exe", version="9.9", auth=False).discover_runtimes(force=True)
    for r in rts.values():
        assert r.evidence_level == EvidenceLevel.VERSION_VERIFIED and not r.authenticated and not r.healthy
        assert "authentication" in r.last_error.lower()


def test_summary_counts_are_dynamic_not_hardcoded(tmp_path, monkeypatch):
    s_missing = _reg(tmp_path, monkeypatch, binary=None, version=None, auth=False).get_summary()
    s_ok = _reg(tmp_path, monkeypatch, binary="x", version="1", auth=True).get_summary()
    assert json.dumps(s_missing) != json.dumps(s_ok)


def test_auth_is_evidence_based_not_inferred_from_executable(tmp_path, monkeypatch):
    reg = RuntimeRegistryService(load_settings(), data_dir=tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(home))
    for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("jarvis.core.runtime_registry.has_provider_api_key", lambda p: False)
    assert not reg._probe_auth_opencode() and not reg._probe_auth_claude() and not reg._probe_auth_antigravity()
    (home / ".claude").mkdir()
    (home / ".claude" / "cred.json").write_text("{}", encoding="utf-8")
    assert reg._probe_auth_claude()  # evidence: a credentials file
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert reg._probe_auth_opencode()  # evidence: a provider key
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    assert reg._probe_auth_antigravity()


def test_version_probe_failure_modes(tmp_path):
    reg = RuntimeRegistryService(load_settings(), data_dir=tmp_path)
    assert reg._probe_version(str(tmp_path / "nonexistent.exe")) is None


# ======================= model routing =======================


def _settings(**prov):
    s = load_settings()
    for k, v in prov.items():
        setattr(s.providers, k, v)
    return s


class _Brain(Brain):
    async def health(self):
        return {"up": True, "models": ["qwen3:4b-instruct"], "loaded": []}


def _gw(settings, tmp_path=None):
    return ModelGateway(_Brain(settings), None, settings=settings)


def test_roles_fast_advanced_coding_resolve_independently():
    s = _settings(fast_model="local", advanced_model="local_thinking", coding_model="local")
    gw = _gw(s)
    assert gw.select_model_for_task("fast")[0] == "local"
    assert gw.select_model_for_task("advanced")[0] == "local_thinking"
    assert gw.select_model_for_task("coding")[0] == "local"
    s2 = _settings(fast_model="openrouter:qwen/qwen3-coder:free", advanced_model="openrouter:qwen/qwen3-coder", coding_model="local")
    g2 = _gw(s2)
    assert g2.select_model_for_task("fast")[0].endswith(":free")
    assert g2.select_model_for_task("advanced")[0] == "openrouter:qwen/qwen3-coder"
    assert g2.select_model_for_task("coding")[0] == "local"
    assert g2.select_model_for_task("unknown_role")[0] == s2.boot_brain  # unknown role -> safe local default


def test_fallback_chain_local_primary_falls_back_to_cloud_only_when_configured_and_cloud_primary_falls_back_to_local():
    s = _settings(fallback_provider="openrouter", openrouter_model="qwen/qwen3-coder:free")
    assert _gw(s).select_model_for_task("fast") == ("local", ["openrouter:qwen/qwen3-coder:free"])  # still gated by Local Mode / allow_cloud at call time
    assert _gw(_settings(fallback_provider="ollama")).select_model_for_task("fast") == ("local", [])  # no cloud unless configured
    s3 = _settings(fast_model="openrouter:qwen/qwen3-coder:free", fallback_provider="ollama")
    assert _gw(s3).select_model_for_task("fast") == ("openrouter:qwen/qwen3-coder:free", ["local"])  # a local model is the last resort


def test_local_ollama_is_never_routed_to_cloud_host():
    s = load_settings()
    s.brains["bad_cloud_as_ollama"] = BrainSpec(provider="ollama", model="x", base_url="https://openrouter.ai/api/v1")
    b = Brain(s)
    assert "openrouter.ai" not in b.ollama_host and b.ollama_host.startswith("http://127.0.0.1")


def test_provider_resolution_from_model_keys():
    s = load_settings()
    assert parse_model_key("openrouter:qwen/qwen3-coder:free", s) == ("openrouter", "qwen/qwen3-coder:free")
    assert parse_model_key("local", s)[0] == "ollama"
    assert is_paid_model("openrouter", "qwen/qwen3-coder", s) is True and is_paid_model("openrouter", "qwen/qwen3-coder:free", s) is False
    assert is_paid_model("ollama", "anything", s) is False


def test_cloud_blocked_in_local_mode_without_flag_or_key(monkeypatch):
    s = load_settings()
    b = Brain(s)
    monkeypatch.setattr("jarvis.brain.get_provider_api_key", lambda p: None)
    with pytest.raises(BrainError, match="Local Mode"):
        b._cloud_allowed("openrouter")  # offline_enforced default
    s.offline_enforced = False
    s.internet.enabled = True
    audit.set_online_enabled(True)
    try:
        with pytest.raises(BrainError, match="allow_cloud"):
            b._cloud_allowed("openrouter")
        s.providers.allow_cloud = True
        with pytest.raises(BrainError, match="API key"):
            b._cloud_allowed("openrouter")
        monkeypatch.setattr("jarvis.brain.get_provider_api_key", lambda p: "sk-test-key")
        assert b._cloud_allowed("openrouter") == "sk-test-key"
    finally:
        audit.set_online_enabled(False)


def _or_brain(monkeypatch, responses):
    s = load_settings()
    s.offline_enforced = False
    s.internet.enabled = True
    s.providers.allow_cloud = True
    audit.set_online_enabled(True)
    monkeypatch.setattr("jarvis.brain.get_provider_api_key", lambda p: "sk-or-v1-secretkeysecretkey1234")
    b = Brain(s)
    b.adhoc["or"] = BrainSpec(provider="openrouter", model="qwen/qwen3-coder:free", base_url=s.providers.openrouter_base_url)
    b.current = "or"
    sent: list[dict] = []

    async def fake_request(method, url, spec, **kw):
        sent.append({"url": url, "json": kw.get("json_body"), "headers": kw.get("headers")})
        return responses.pop(0)

    monkeypatch.setattr("jarvis.brain.safe_request", fake_request)
    return b, sent


def _resp(status, body):
    return SafeHTTPResponse(status_code=status, url="https://openrouter.ai/api/v1/chat/completions", headers={}, body=json.dumps(body).encode())


def test_openrouter_independent_call_and_retired_free_slug_is_normalised(monkeypatch):
    ok = _resp(200, {"choices": [{"message": {"content": "hello from cloud"}}]})
    retired = _resp(404, {"error": {"message": "model is unavailable for free; use this slug instead: qwen/qwen3-coder"}})
    b, sent = _or_brain(monkeypatch, [retired, ok])

    async def run():
        return "".join([e["text"] async for e in b._cloud_complete("openrouter", [{"role": "user", "content": "hi"}]) if e["type"] == "token"])

    try:
        assert asyncio.run(run()) == "hello from cloud"
    finally:
        audit.set_online_enabled(False)
    assert [c["json"]["model"] for c in sent] == ["qwen/qwen3-coder:free", "qwen/qwen3-coder"]  # retried once with the base slug
    assert sent[0]["url"].startswith("https://openrouter.ai/") and sent[0]["headers"]["Authorization"].startswith("Bearer ")
    assert all("ollama" not in c["url"] and "11434" not in c["url"] for c in sent)  # never touches the local endpoint


def test_openrouter_error_does_not_leak_key(monkeypatch):
    b, _ = _or_brain(monkeypatch, [_resp(401, {"error": {"message": "invalid key"}})])

    async def run():
        return [e async for e in b._cloud_complete("openrouter", [{"role": "user", "content": "key sk-or-v1-secretkeysecretkey1234 inside prompt"}])]

    try:
        with pytest.raises(BrainError) as ei:
            asyncio.run(run())
    finally:
        audit.set_online_enabled(False)
    assert "secretkeysecretkey1234" not in str(ei.value)


def test_gateway_paid_model_blocked_and_breaker_fallback_to_local():
    s = _settings(fast_model="openrouter:qwen/qwen3-coder", fallback_provider="ollama")  # paid slug
    gw = _gw(s)
    req = GatewayRequest(model_key="openrouter:qwen/qwen3-coder", messages=[{"role": "user", "content": "x"}])
    eligible, rej = gw.inspect_candidate_eligibility(req, ["openrouter:qwen/qwen3-coder", s.boot_brain])
    assert eligible == [s.boot_brain] and rej[0]["reason"] == "paid_blocked"  # falls back to local, never silently spends
    s.providers.allow_paid_models = True
    for _ in range(int(s.gateway.circuit_breaker_failures)):
        gw._record_breaker_failure("openrouter", "503")
    eligible, rej = gw.inspect_candidate_eligibility(req, ["openrouter:qwen/qwen3-coder", s.boot_brain])
    assert eligible == [s.boot_brain] and rej[0]["reason"] == "circuit_open"


def test_stream_with_fallback_uses_local_when_cloud_blocked_and_errors_when_nothing_eligible():
    s = _settings()

    class FB(_Brain):
        async def stream_events(self, messages, tools=None, **kw):
            yield {"type": "token", "text": "local answer"}
            self.last_stats = {"tokens": 2, "model": "m"}

    gw = ModelGateway(FB(s), None, settings=s)
    req = GatewayRequest(model_key="openrouter:some/model:free", messages=[{"role": "user", "content": "x"}])

    async def run():
        return "".join([e.text async for e in gw.stream_with_fallback(req, fallback_models=[s.boot_brain]) if e.type == "token"])

    assert asyncio.run(run()) == "local answer"  # cloud refused in Local Mode -> local brain answers

    s.providers.allow_paid_models = False
    only_paid = GatewayRequest(model_key="openrouter:some/paid-model", messages=[{"role": "user", "content": "x"}])

    async def run2():
        return [e async for e in gw.stream_with_fallback(only_paid)]

    with pytest.raises(CloudGatewayError) as ei:
        asyncio.run(run2())
    assert ei.value.category == "paid_blocked"


def test_test_route_reports_roles_and_breakers():
    gw = _gw(_settings())
    r = asyncio.run(gw.test_route(role="coding"))
    assert r["role"] == "coding" and r["selected"] and set(r["breakers"]) >= {"ollama", "openrouter"}


def test_settings_load_and_render_with_ollama_offline(settings, assistant, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.api import create_app

    async def down():
        raise ConnectionError("ollama offline")

    assistant.brain.health = down
    assistant.brain.installed = down
    monkeypatch.setattr("jarvis.api.save_overrides", lambda p, *a: None)
    with TestClient(create_app(settings, assistant)) as c:
        assert c.get("/api/v2/settings").status_code == 200
        assert c.get("/api/v2/gateway/capabilities").status_code == 200  # provider/capability listing also survives Ollama being down


def test_retired_model_identifier_in_saved_settings_does_not_break_loading(tmp_path, monkeypatch):
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"providers": {"openrouter_model": "qwen/qwen3-coder:free", "fast_model": "openrouter:retired/model:free"}}), encoding="utf-8")
    s = load_settings(overrides_path=ov)
    assert s.providers.openrouter_model == "qwen/qwen3-coder:free"
    assert _gw(s).select_model_for_task("fast")[0] == "openrouter:retired/model:free"  # kept verbatim; eligibility gates decide at call time
