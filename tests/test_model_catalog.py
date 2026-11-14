"""Model catalog: discovery, cache, filters, recommendations, validated selection, chat switching, offline/secret behaviour. Provider HTTP is mocked here ONLY."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from jarvis import model_catalog as mc
from jarvis.config import load_settings
from jarvis.model_chat import handle_chat, parse_model_request

SECRET = "sk-TESTSECRET1234567890abcdef"
OR_MODELS = {"data": [
    {"id": "deepseek/deepseek-chat:free", "name": "DeepSeek Chat (free)", "context_length": 64000, "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, "supported_parameters": ["tools"]},
    {"id": "qwen/qwen3-coder", "name": "Qwen3 Coder", "context_length": 262144, "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
     "architecture": {"input_modalities": ["text"]}, "supported_parameters": ["tools", "reasoning"]},
    {"id": "google/gemini-vision", "name": "Vision", "context_length": 1000000, "pricing": {"prompt": "0.000001", "completion": "0.000004"},
     "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": []},
]}
OA_MODELS = {"data": [{"id": "gpt-4.1", "owned_by": "system"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}, {"id": "gpt-4o-mini"}]}


class FakeBrain:
    def __init__(self, s):
        self.settings, self.current, self.switched = s, s.boot_brain, []
        self.spec = SimpleNamespace(model="qwen3:4b-instruct")
        self.provider = "ollama"

    async def installed(self):
        return ["qwen3:4b-instruct"]

    async def switch(self, key):
        self.switched.append(key)
        self.current = key
        prov = key.split(":", 1)[0] if ":" in key else "ollama"
        model = key.split(":", 1)[-1]
        self.provider, self.spec = prov, SimpleNamespace(model=model)
        return {"key": key, "model": model, "provider": prov}


def make(handler, *, offline=False, keys=("openai", "openrouter"), monkeypatch=None, calls=None):
    s = load_settings()
    s.offline_enforced = offline
    s.internet.enabled = not offline
    s.providers.allow_cloud = True
    a = SimpleNamespace(settings=s, brain=FakeBrain(s), bus=SimpleNamespace(published=[], publish=lambda ev: a.bus.published.append(ev)), warm_brain=lambda: None)

    def counting(req):
        if calls is not None:
            calls.append(str(req.url))
        return handler(req)

    a.model_catalog = mc.ModelCatalog(a, transport=httpx.MockTransport(counting))
    monkeypatch.setattr(mc, "has_provider_api_key", lambda p, *_a, **_k: p in keys)
    monkeypatch.setattr(mc, "get_provider_api_key", lambda p, *_a, **_k: SECRET if p in keys else None)
    monkeypatch.setattr(mc.audit, "record_external", lambda *a_, **k_: {})
    monkeypatch.setattr(mc.audit, "is_online_enabled", lambda: not offline)
    monkeypatch.setattr("jarvis.netsec.audit.is_online_enabled", lambda: not offline)
    return a


def ok_handler(req):
    return httpx.Response(200, json=OR_MODELS if "openrouter" in str(req.url) else OA_MODELS)


def run(c):
    return asyncio.run(c)


def test_discovery_per_provider_and_filters(monkeypatch):
    a = make(ok_handler, monkeypatch=monkeypatch)
    res = run(a.model_catalog.listing())
    provs = {m["provider"] for m in res["models"]}
    assert provs == {"ollama", "openai", "openrouter"}
    ids = {m["model"] for m in res["models"]}
    assert "gpt-4.1" in ids and "text-embedding-3-small" not in ids and "whisper-1" not in ids  # only chat-capable OpenAI ids
    assert [m["model"] for m in run(a.model_catalog.listing(provider="openai"))["models"]] == ["gpt-4.1", "gpt-4o-mini"]
    free = run(a.model_catalog.listing(free=True))["models"]
    assert all(m["free"] for m in free) and any(m["model"].endswith(":free") for m in free)
    assert [m["model"] for m in run(a.model_catalog.listing(q="coder"))["models"]] == ["qwen/qwen3-coder"]
    q3 = next(m for m in res["models"] if m["model"] == "qwen/qwen3-coder")
    assert q3["price_in"] == 0.3 and q3["price_out"] == 1.2 and q3["context"] == 262144  # per 1M tokens, from provider metadata
    oai = next(m for m in res["models"] if m["model"] == "gpt-4.1")
    assert oai["context"] is None and oai["price_in"] is None and oai["metadata"] == "id-only"  # not invented


def test_cache_ttl_manual_refresh_and_stale_on_failure(monkeypatch):
    calls: list[str] = []
    state = {"fail": False}

    def h(req):
        return httpx.Response(503) if state["fail"] else ok_handler(req)

    a = make(h, monkeypatch=monkeypatch, calls=calls)
    run(a.model_catalog.listing(provider="openrouter"))
    run(a.model_catalog.listing(provider="openrouter"))
    assert len(calls) == 1  # second call served from cache, not the provider
    run(a.model_catalog.listing(provider="openrouter", refresh=True))
    assert len(calls) == 2
    state["fail"] = True
    st = run(a.model_catalog.refresh("openrouter", force=True))
    assert st["models"] and st["stale"] is True and st["error_category"] == "http"  # old list kept but flagged stale, never "fresh"
    a.model_catalog.cache["openrouter"]["fetched_at"] -= mc.TTL_S + 1
    st = run(a.model_catalog.refresh("openrouter"))
    assert st["stale"] is True


@pytest.mark.parametrize("status,cat", [(401, "auth"), (403, "auth"), (429, "rate_limited")])
def test_auth_and_rate_limit_classification_without_secret(monkeypatch, status, cat):
    a = make(lambda r: httpx.Response(status, text=f"bad key {SECRET}"), monkeypatch=monkeypatch)
    st = run(a.model_catalog.refresh("openai"))
    assert st["error_category"] == cat and st["models"] == []
    assert SECRET not in json.dumps(st)


def test_timeout_and_network_errors(monkeypatch):
    def boom(req):
        raise httpx.ConnectTimeout("t")

    a = make(boom, monkeypatch=monkeypatch)
    assert run(a.model_catalog.refresh("openrouter"))["error_category"] == "timeout"

    def down(req):
        raise httpx.ConnectError(f"refused {SECRET}")

    a = make(down, monkeypatch=monkeypatch)
    st = run(a.model_catalog.refresh("openrouter"))
    assert st["error_category"] == "network" and SECRET not in json.dumps(st)


def test_offline_enforced_makes_zero_network_calls(monkeypatch):
    calls: list[str] = []
    a = make(ok_handler, offline=True, monkeypatch=monkeypatch, calls=calls)
    res = run(a.model_catalog.listing())
    assert calls == []
    assert {s["provider"]: s["status"] for s in res["providers"]}["openrouter"] == "offline_blocked"
    out = run(a.model_catalog.select("openrouter:qwen/qwen3-coder", confirm_paid=True))
    assert out["status"] == "offline_blocked" and a.brain.switched == [] and calls == []


def test_disconnected_provider(monkeypatch):
    a = make(ok_handler, keys=(), monkeypatch=monkeypatch)
    assert run(a.model_catalog.refresh("openai"))["status"] == "not_connected"
    assert run(a.model_catalog.select("openai:gpt-4.1"))["status"] == "not_connected"


def test_select_validation_paid_confirmation_and_state_sync(monkeypatch):
    a = make(ok_handler, monkeypatch=monkeypatch)
    run(a.model_catalog.listing())
    for bad in ("", "x", "openai:../../etc", "https://evil.example/v1", "anthropic:claude"):
        assert run(a.model_catalog.select(bad))["ok"] is False
    assert run(a.model_catalog.select("openrouter:does/not-exist"))["status"] == "unavailable"
    paid = run(a.model_catalog.select("openrouter:qwen/qwen3-coder"))
    assert paid["status"] == "needs_confirmation" and paid["pricing"]["input_per_1m_usd"] == 0.3 and a.brain.switched == []
    free = run(a.model_catalog.select("openrouter:deepseek/deepseek-chat:free"))
    assert free["ok"] and a.brain.current == "openrouter:deepseek/deepseek-chat:free"  # the one authoritative state
    assert a.bus.published and a.bus.published[-1]["type"] == "model"  # frontend indicator is refreshed from this event
    assert run(a.model_catalog.select("openrouter:qwen/qwen3-coder", confirm_paid=True))["ok"]
    local = run(a.model_catalog.select(a.settings.boot_brain))
    assert local["ok"] and a.brain.provider == "ollama"


def test_recommendations_use_only_metadata(monkeypatch):
    a = make(ok_handler, monkeypatch=monkeypatch)
    rows = run(a.model_catalog.listing())["models"]
    vis = a.model_catalog.recommend("vision", rows)["recommendations"]
    assert [r["model"] for r in vis] == ["google/gemini-vision"]  # only model whose metadata lists image input
    assert "provider metadata lists image input" in vis[0]["reasons"]
    assert all(r["needs_paid_confirmation"] is (not r["free"]) for r in a.model_catalog.recommend("coding", rows)["recommendations"])
    long = a.model_catalog.recommend("long_documents", rows)["recommendations"]
    assert long[0]["model"] == "google/gemini-vision" and "1,000,000" in long[0]["reasons"][0]
    assert a.model_catalog.recommend("speech", rows)["recommendations"] == []  # no model advertises speech: nothing claimed
    cheap = a.model_catalog.recommend("low_cost", rows)["recommendations"]
    assert cheap[0]["free"] is True


@pytest.mark.parametrize("text,action", [
    ("Show available models.", "list"), ("What cloud models are connected?", "list"), ("show me the modles", "list"),
    ("Show me free OpenRouter models.", "list"), ("Which model is best for coding?", "recommend"), ("Recommend a translation model", "recommend"),
    ("Use GPT-4.1.", "use"), ("Switch to OpenRouter DeepSeek", "use"), ("Use the fastest available model", "use_best"),
    ("Compare the available models", "compare"), ("Which model am I using?", "current"), ("switch back to local ollama", "use"),
])
def test_chat_understanding(text, action):
    assert parse_model_request(text)["action"] == action


@pytest.mark.parametrize("text", ["what is a language model?", "explain how models are trained", "open my folder", "what is the weather", "remember my model number is 42"])
def test_chat_ignores_unrelated(text):
    assert parse_model_request(text) is None


def test_chat_switch_flow_and_secrets(monkeypatch):
    a = make(ok_handler, monkeypatch=monkeypatch)
    listing = run(handle_chat(a, "Show available models"))
    assert listing["ui"]["model_picker"] is not None and "STALE" not in listing["reply"]
    rec = run(handle_chat(a, "recommend the best model for coding"))
    assert "Recommended for coding" in rec["reply"] and "qwen" in rec["reply"].lower()
    paid = run(handle_chat(a, "use gpt-4.1"))
    assert a.brain.switched == [] or a.brain.provider != "openai"  # paid/unpriced model is never switched silently
    free = run(handle_chat(a, "use openrouter deepseek-chat:free"))
    assert free["ok"] and a.brain.current == "openrouter:deepseek/deepseek-chat:free"
    assert run(handle_chat(a, "which model am I using?"))["reply"].count("deepseek") >= 1
    back = run(handle_chat(a, "switch back to local ollama"))
    assert back["ok"] and a.brain.provider == "ollama"
    blob = json.dumps([listing, rec, paid, free, back])
    assert SECRET not in blob
    assert run(handle_chat(a, "use my notes")) is None  # not model-like: normal routing continues


def test_conversation_preserved_across_switch(monkeypatch):
    a = make(ok_handler, monkeypatch=monkeypatch)
    a.history = SimpleNamespace(items=[{"role": "user", "content": "hello"}])
    run(a.model_catalog.listing())
    run(a.model_catalog.select("openrouter:deepseek/deepseek-chat:free"))
    assert a.history.items == [{"role": "user", "content": "hello"}]


def test_rejected_credential_blocks_switching_from_stale_cache(monkeypatch):
    state = {"code": 200}
    a = make(lambda r: httpx.Response(state["code"], json=OR_MODELS) if state["code"] == 200 else httpx.Response(401, text=SECRET), monkeypatch=monkeypatch)
    run(a.model_catalog.listing(provider="openrouter"))
    state["code"] = 401
    run(a.model_catalog.refresh("openrouter", force=True))
    out = run(a.model_catalog.select("openrouter:deepseek/deepseek-chat:free"))
    assert out["status"] == "auth" and a.brain.switched == [] and SECRET not in json.dumps(out)


def test_variable_pricing_router_is_unknown_not_negative(monkeypatch):
    data = {"data": [{"id": "openrouter/auto", "name": "Auto", "context_length": 2000000, "pricing": {"prompt": "-1", "completion": "-1"}, "architecture": {}, "supported_parameters": ["tools"]}]}
    a = make(lambda r: httpx.Response(200, json=data), monkeypatch=monkeypatch)
    m = run(a.model_catalog.listing(provider="openrouter"))["models"][0]
    assert m["price_in"] is None and m["free"] is False
    rec = a.model_catalog.recommend("coding", [m])["recommendations"][0]
    assert rec["score"] <= 4.5  # no bogus boost from a negative price


@pytest.mark.parametrize("text", ["How much VRAM do I need for a local model?", "Which local model fits in 4GB of VRAM?", "should I use a free model for my essay", "do I need a paid model"])
def test_questions_about_models_are_not_management_requests(text):
    assert parse_model_request(text) is None


@pytest.mark.parametrize("text", ["how many models we have?", "how many models do I have installed", "what models do we have"])
def test_model_count_questions_list_models(text):
    assert parse_model_request(text)["action"] == "list"


@pytest.mark.parametrize("text,prov", [("change the model to router", "openrouter"), ("switch to open router", "openrouter"), ("use openai", "openai")])
def test_provider_alias_opens_selector_for_that_provider(monkeypatch, text, prov):
    a = make(ok_handler, monkeypatch=monkeypatch)
    r = run(handle_chat(a, text))
    assert r["ui"]["model_picker"]["provider"] == prov and a.brain.switched == []
