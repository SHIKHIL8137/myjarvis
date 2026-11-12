"""Brain + model manager against a mocked Ollama (httpx.MockTransport): streaming, think filtering, errors, switching."""
import asyncio
import json

import httpx
import pytest

from jarvis.brain import Brain, BrainError, ThinkFilter


def run(c):
    return asyncio.run(c)


def ndjson(*chunks):
    return "\n".join(json.dumps(c) for c in chunks).encode()


def make(settings, handler):
    return Brain(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_think_filter_across_chunk_boundaries():
    f = ThinkFilter()
    out = "".join(f.feed(x) for x in ["Hel", "lo <thi", "nk>secret ", "reasoning</th", "ink> world", "!"]) + f.flush()
    assert out == "Hello  world!" and "secret" not in out
    g = ThinkFilter()
    assert g.feed("a < b and <th") + g.flush() == "a < b and <th"  # lone '<' is not swallowed forever


def test_stream_ignores_thinking_field_and_reports_stats(settings):
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=ndjson(
            {"message": {"thinking": "let me think about this"}}, {"message": {"content": "Hello"}},
            {"message": {"content": " sir."}}, {"done": True, "eval_count": 10, "eval_duration": 2_000_000_000, "done_reason": "stop"}))
    b = make(settings, h)
    text = run(b.complete([{"role": "user", "content": "hi"}]))
    assert text == "Hello sir." and "think" not in text
    assert seen["body"]["options"]["num_ctx"] == 4096 and seen["body"]["think"] is False and seen["body"]["model"] == "qwen3:4b-instruct"
    assert b.last_stats["tps"] == 5.0 and b.last_stats["truncated"] is False


def test_thinking_model_requests_think_true_but_hides_it(settings):
    seen = {}

    def h(req):
        seen["b"] = json.loads(req.content)
        return httpx.Response(200, content=ndjson({"message": {"content": "<think>hmm</think>Answer."}}, {"done": True, "eval_count": 1, "eval_duration": 1}))
    b = make(settings, h)
    b.current = "local_thinking"
    assert run(b.complete([{"role": "user", "content": "x"}])) == "Answer." and seen["b"]["think"] is True


def test_errors_are_friendly(settings):
    def refuse(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(BrainError, match="Ollama"):
        run(make(settings, refuse).complete([{"role": "user", "content": "x"}]))
    with pytest.raises(BrainError, match="ollama pull"):
        run(make(settings, lambda r: httpx.Response(404, json={"error": "model not found"})).complete([{"role": "user", "content": "x"}]))
    with pytest.raises(BrainError, match="boom"):
        run(make(settings, lambda r: httpx.Response(200, content=ndjson({"error": "boom"}))).complete([{"role": "user", "content": "x"}]))
    assert run(make(settings, refuse).health())["up"] is False


def test_switch_unloads_previous_and_never_pulls(settings):
    calls = []

    def h(req):
        calls.append((req.method, req.url.path, req.content))
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct"}, {"name": "qwen3:4b"}, {"name": "qwen2.5vl:3b"}]})
        return httpx.Response(200, json={})
    b = make(settings, h)
    assert run(b.switch("local_thinking"))["model"] == "qwen3:4b" and b.current == "local_thinking"
    unload = [c for c in calls if c[1] == "/api/generate"]
    assert unload and json.loads(unload[0][2]) == {"model": "qwen3:4b-instruct", "keep_alive": 0}
    assert run(b.switch("qwen2.5vl:3b"))["model"] == "qwen2.5vl:3b"  # any installed model
    with pytest.raises(BrainError, match="not installed"):
        run(b.switch("llama99:1b"))
    assert not any(c[1] == "/api/pull" for c in calls)
    cat = run(b.catalogue())
    assert [m["key"] for m in cat][:2] == ["local", "local_thinking"] and sum(m["current"] for m in cat) == 1


def test_stream_events_parses_tool_calls_and_handles_unsupported(settings):
    from jarvis.brain import ToolsUnsupportedError

    def tool_handler(req):
        body = json.loads(req.content)
        assert body["tools"][0]["function"]["name"] == "web_search"
        return httpx.Response(200, content=ndjson(
            {"message": {"content": "", "tool_calls": [{"id": "tc1", "function": {"name": "web_search", "arguments": {"query": "python 3.14"}}}]}},
            {"done": True, "eval_count": 4, "eval_duration": 1_000_000_000, "done_reason": "stop"},
        ))

    async def collect():
        b = make(settings, tool_handler)
        evs = [ev async for ev in b.stream_events([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "web_search"}}])]
        await b.client.aclose()
        return evs

    evs = run(collect())
    assert evs == [{"type": "tool_calls", "calls": [{"id": "tc1", "name": "web_search", "arguments": {"query": "python 3.14"}}]}]

    b2 = make(settings, lambda r: httpx.Response(400, json={"error": "model does not support tools"}))
    with pytest.raises(ToolsUnsupportedError):
        run(collect_bad(b2))


async def collect_bad(b):
    async for _ in b.stream_events([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "web_search"}}]):
        pass


def test_unpaired_closing_think_and_truncated_fallback_never_leak(settings):
    """If a thinking model omits opening <think> and emits </think> (or truncates mid-thought), never expose reasoning."""
    # Case 1: Unpaired </think> split across chunks in local_thinking stream
    def h_unpaired(req):
        body = json.loads(req.content)
        assert body["think"] is True
        return httpx.Response(200, content=ndjson(
            {"message": {"content": "Okay, the user wants me to say hello cleanly. "}},
            {"message": {"content": "I will answer briefly.</th"}},
            {"message": {"content": "ink>\nHello, sir."}},
            {"done": True, "eval_count": 12, "eval_duration": 1_000_000_000, "done_reason": "stop"},
        ))

    b = make(settings, h_unpaired)
    b.current = "local_thinking"
    out = run(b.complete([{"role": "user", "content": "Say hello"}]))
    assert out == "Hello, sir." and "user wants" not in out

    # Case 2: Hit token limit inside unopened reasoning -> automatically retries once with think=False
    calls = []

    def h_trunc(req):
        body = json.loads(req.content)
        calls.append(body["think"])
        if body["think"]:
            return httpx.Response(200, content=ndjson(
                {"message": {"content": "Very long unopened reasoning without closing tag..."}},
                {"done": True, "eval_count": 512, "eval_duration": 1_000_000_000, "done_reason": "length"},
            ))
        return httpx.Response(200, content=ndjson(
            {"message": {"content": "Good day, sir."}},
            {"done": True, "eval_count": 4, "eval_duration": 1_000_000_000, "done_reason": "stop"},
        ))

    b2 = make(settings, h_trunc)
    b2.current = "local_thinking"
    out2 = run(b2.complete([{"role": "user", "content": "Say hello"}]))
    assert calls == [True, False] and out2 == "Good day, sir."
