"""Controlled internet access, web search providers, SSRF/DNS-rebinding/redirect protection, API key security,
Ollama tool calling + deterministic fallback, and source attribution tests."""
import asyncio
import json
import socket

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import FakeBrain
from jarvis import audit
from jarvis.api import create_app
from jarvis.brain import Brain, ToolsUnsupportedError
from jarvis.config import InternetSpec, delete_provider_api_key, get_provider_api_key, set_provider_api_key
from jarvis.netsec import NetworkSecurityError, resolve_and_validate, safe_request, sanitize_url
from jarvis.search import (
    SearchConfigError,
    SearchOfflineError,
    SearchRateLimitError,
    WebSearchService,
)

H = {"X-Jarvis": "1"}


def _mock_transport(handler):
    return lambda target: httpx.MockTransport(handler)


def test_internet_disabled_by_default_and_blocks_search(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    assert settings.internet.enabled is False
    with TestClient(create_app(settings, assistant)) as c:
        st = c.get("/api/internet").json()
        assert st["enabled"] is False and st["mode"] == "local"
        r = c.post("/api/internet/search", json={"query": "python 3.14"}, headers=H)
        assert r.status_code == 403 and "Local Mode" in r.json()["error"]
        # Explicit web search chat command returns honest offline receipt
        chat = c.post("/api/chat", json={"message": "search the web for python 3.14"}, headers=H).json()
        assert chat["command"] == "web_offline" and chat["ok"] is False and "Local Mode" in chat["answer"]


def test_online_mode_still_blocks_unscoped_outbound_sockets(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    with TestClient(create_app(settings, assistant)) as c:
        assert c.post("/api/internet/mode", json={"enabled": True}, headers=H).json()["enabled"] is True
        # Arbitrary external sockets outside netsec remain blocked even in Online Assisted Mode
        with pytest.raises(ConnectionError, match="blocked"):
            socket.getaddrinfo("evil.example", 80)
        with pytest.raises(ConnectionError, match="blocked"):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        assert c.post("/api/internet/mode", json={"enabled": False}, headers=H).json()["enabled"] is False


@pytest.mark.parametrize("bad_url", [
    "",
    "http://example.com/search",
    "ftp://example.com/file",
    "javascript:alert(1)",
    "https://user:pass@example.com/",
    "https://localhost/api",
    "https://127.0.0.1/api",
    "https://[::1]/api",
    "https://10.0.0.5/admin",
    "https://192.168.1.1/",
    "https://172.16.0.10/",
    "https://169.254.169.254/latest/meta-data/",
    "https://100.64.0.1/",  # CGNAT
    "https://[::ffff:127.0.0.1]/",
    "https://internal.local/secret",
    "https://example.com:8443/path",
])
def test_invalid_urls_and_private_ips_rejected(bad_url, monkeypatch):
    audit.set_online_enabled(True)
    spec = InternetSpec(enabled=True)
    with pytest.raises(NetworkSecurityError):
        asyncio.run(safe_request("GET", bad_url, spec, transport_factory=_mock_transport(lambda req: httpx.Response(200))))


def test_dns_rebinding_and_ssrf_resolution_blocked(monkeypatch):
    audit.set_online_enabled(True)
    # Simulate DNS returning a private IP for an external domain
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
    )
    with pytest.raises(NetworkSecurityError, match="non-public IP"):
        resolve_and_validate("https://rebind.example/test")


def test_redirect_to_private_ip_or_http_blocked(monkeypatch):
    audit.set_online_enabled(True)
    spec = InternetSpec(enabled=True, max_redirects=3)

    def redirect_private(req):
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    with pytest.raises(NetworkSecurityError, match="non-public IP"):
        asyncio.run(safe_request("GET", "https://example.com/start", spec, transport_factory=_mock_transport(redirect_private)))

    def redirect_http(req):
        return httpx.Response(301, headers={"location": "http://example.com/insecure"})

    with pytest.raises(NetworkSecurityError, match="https://"):
        asyncio.run(safe_request("GET", "https://example.com/start", spec, transport_factory=_mock_transport(redirect_http)))

    def redirect_loop(req):
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    with pytest.raises(NetworkSecurityError, match="maximum redirects"):
        asyncio.run(safe_request("GET", "https://example.com/loop", spec, transport_factory=_mock_transport(redirect_loop)))


def test_pinned_transport_constructs_valid_httpcore_request(monkeypatch):
    from jarvis.netsec import ValidatedTarget, _PinnedTransport

    seen = {}

    class FakePool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def handle_async_request(self, req):
            seen["url"] = req.url

            async def _gen():
                yield b"ok"

            return type("R", (), {"status": 200, "headers": [], "stream": _gen(), "extensions": {}})()

        async def aclose(self):
            pass

    target = ValidatedTarget("https://example.com/search?q=hi", "https://example.com/search?q=hi", "example.com", 443, ("93.184.216.34",), "93.184.216.34")
    tr = _PinnedTransport(target)
    tr._pool = FakePool()

    async def go():
        async with httpx.AsyncClient(transport=tr) as c:
            r = await c.get("https://example.com/search?q=hi")
            return r.status_code

    assert asyncio.run(go()) == 200
    assert seen["url"].target == b"/search?q=hi" and seen["url"].host == b"example.com"


def test_download_size_limit_enforced():
    audit.set_online_enabled(True)
    spec = InternetSpec(enabled=True, max_fetch_bytes=10_000)
    with pytest.raises(NetworkSecurityError, match="max download size"):
        asyncio.run(
            safe_request(
                "GET",
                "https://example.com/huge",
                spec,
                max_bytes=500,
                transport_factory=_mock_transport(lambda req: httpx.Response(200, content=b"x" * 2000)),
            )
        )


def test_search_providers_caching_rate_limit_and_robots(tmp_path, monkeypatch):
    audit.set_online_enabled(True)
    monkeypatch.setattr("jarvis.config.SECRETS_PATH", tmp_path / "secrets.json")

    # DuckDuckGo HTML parsing + Instant Answer fallback on bot check
    ddg_html = """
    <html><body>
      <div class="result"><a class="result__a" href="https://docs.python.org/3.14/">Python 3.14 Docs</a>
      <a class="result__snippet">Official Python 3.14 release documentation.</a></div>
    </body></html>
    """
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if req.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=ddg_html)
        if req.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": "Tavily Hit", "url": "https://example.org/news", "content": "Fresh snippet", "published_date": "2026-11-11"}]})
        if req.url.host == "api.search.brave.com":
            assert req.headers.get("x-subscription-token") == "brave-secret-123"
            return httpx.Response(200, json={"web": {"results": [{"title": "Brave Hit", "url": "https://brave.example/a", "description": "Brave desc", "page_age": "1d"}]}})
        if req.url.host == "docs.example.org" and req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\nAllow: /public/\n")
        if req.url.host == "docs.example.org" and req.url.path == "/public/page":
            return httpx.Response(200, text="<html><head><title>Public Page</title></head><body><script>bad()</script><p>Hello clean text.</p></body></html>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    spec = InternetSpec(enabled=True, provider="duckduckgo", allow_page_fetch=True, rate_limit_rpm=20, cache_ttl_s=300)
    svc = WebSearchService(spec, transport_factory=_mock_transport(handler))

    res1 = asyncio.run(svc.search("python 3.14"))
    assert res1["cached"] is False and res1["results"][0]["title"] == "Python 3.14 Docs"
    res2 = asyncio.run(svc.search("python 3.14"))
    assert res2["cached"] is True and len(calls) == 1  # second call hit TTL cache

    # Tavily without key raises SearchConfigError; with key works
    svc.spec = spec.model_copy(update={"provider": "tavily"})
    monkeypatch.delenv("JARVIS_TAVILY_API_KEY", raising=False)
    with pytest.raises(SearchConfigError, match="Tavily API key"):
        asyncio.run(svc.search("latest release"))
    set_provider_api_key("tavily", "tvly-secret-xyz", secrets_path=tmp_path / "secrets.json")
    assert get_provider_api_key("tavily", secrets_path=tmp_path / "secrets.json") == "tvly-secret-xyz"
    # Configure Brave via env var and test
    svc.spec = spec.model_copy(update={"provider": "brave"})
    monkeypatch.setenv("JARVIS_BRAVE_API_KEY", "brave-secret-123")
    br = asyncio.run(svc.search("brave query"))
    assert br["results"][0]["url"] == "https://brave.example/a"

    # robots.txt blocks /private/ and allows /public/
    with pytest.raises(Exception, match="robots.txt"):
        asyncio.run(svc.fetch_page("https://docs.example.org/private/secret"))
    page = asyncio.run(svc.fetch_page("https://docs.example.org/public/page"))
    assert "Hello clean text." in page["content"] and "bad()" not in page["content"]

    # Rate limit triggers cleanly once RPM exceeded
    svc.spec = spec.model_copy(update={"rate_limit_rpm": 2, "cache_ttl_s": 0})
    with pytest.raises(SearchRateLimitError):
        for i in range(5):
            asyncio.run(svc.search(f"uncached query {i}"))


def test_api_keys_never_exposed_to_frontend_or_logs(settings, assistant, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.config.SECRETS_PATH", tmp_path / "secrets.json")
    secret = "tvly-super-secret-value-999"
    with TestClient(create_app(settings, assistant)) as c:
        r = c.post("/api/internet/key", json={"provider": "tavily", "api_key": secret}, headers=H)
        assert r.status_code == 200 and secret not in r.text
        for ep in ("/api/config", "/api/status", "/api/internet", "/api/drill/connections"):
            txt = c.get(ep).text
            assert secret not in txt, ep
        assert c.get("/api/config").json()["api_keys_configured"]["tavily"] is True
        # Reject passing api_key in PUT /api/config
        bad = c.put("/api/config", json={"internet": {"api_key": secret}}, headers=H)
        assert bad.status_code == 422
        assert "api_key=REDACTED" in sanitize_url(f"https://api.tavily.com/search?api_key={secret}&q=hi")
        assert secret not in sanitize_url(f"https://api.tavily.com/search?api_key={secret}&q=hi")
        c.delete("/api/internet/key/tavily", headers=H)
        assert c.get("/api/config").json()["api_keys_configured"]["tavily"] is False


def test_tool_calling_and_deterministic_fallback_and_source_attribution(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda *a, **k: None)

    class ToolCapableBrain(FakeBrain):
        def __init__(self, s):
            super().__init__(s)
            self.round = 0

        async def stream_events(self, messages, tools=None):
            self.seen.append({"messages": messages, "tools": tools})
            self.round += 1
            if tools and self.round == 1:
                yield {"type": "token", "text": "Checking..."}
                yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "web_search", "arguments": {"query": "Python 3.14 release"}}]}
                return
            yield {"type": "token", "text": "Python 3.14 is documented on python.org."}
            self.last_stats = {"ttft": 0.1, "tokens": 8, "tps": 40.0, "total": 0.2, "model": self.spec.model, "truncated": False}

    # Mock search service
    def search_handler(req):
        return httpx.Response(200, text='<a class="result__a" href="https://docs.python.org/3.14/">Python 3.14</a><a class="result__snippet">Whats new in 3.14</a>')

    assistant.brain = ToolCapableBrain(settings)
    assistant.search = WebSearchService(settings.internet, transport_factory=_mock_transport(search_handler))

    with TestClient(create_app(settings, assistant)) as c:
        c.post("/api/internet/mode", json={"enabled": True}, headers=H)
        with c.stream("POST", "/api/chat/stream", json={"message": "What is new in Python 3.14?"}, headers=H) as s:
            evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
        types_ = [e["type"] for e in evs]
        assert "tool" in types_ and "reset_answer" in types_
        done = evs[-1]
        assert done["type"] == "done" and done["web_sources"][0]["url"] == "https://docs.python.org/3.14/"

        # Test unauthorized tool name is rejected by _execute_tool
        bad_out, bad_srcs = asyncio.run(assistant._execute_tool({"name": "run_shell", "arguments": {"cmd": "dir"}}))
        assert "not permitted" in bad_out and bad_srcs == []

        # Test deterministic fallback when model rejects tools
        class NoToolsBrain(FakeBrain):
            async def stream_events(self, messages, tools=None):
                if tools:
                    raise ToolsUnsupportedError("model does not support tools")
                self.seen.append(messages)
                yield {"type": "token", "text": "Grounded via fallback."}
                self.last_stats = {"ttft": 0.1, "tokens": 4, "tps": 30.0, "total": 0.1, "model": self.spec.model, "truncated": False}

            async def stream(self, messages):
                async for ev in self.stream_events(messages, tools=None):
                    if ev["type"] == "token":
                        yield ev["text"]

        assistant.brain = NoToolsBrain(settings)
        r2 = c.post("/api/chat", json={"message": "latest news about Python 3.14"}, headers=H).json()
        assert r2["answer"] == "Grounded via fallback." and r2["web_sources"][0]["url"] == "https://docs.python.org/3.14/"
