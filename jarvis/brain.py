"""Ollama client + model manager (native /api/chat: it is the only way to set num_ctx per request).
Reasoning text never reaches callers: the `thinking` field is dropped and any <think>..</think> is filtered.
Supports Ollama tool calling for approved web search/fetch tools with clean fallback if a model lacks tool support."""
import json
import logging
import time
from typing import AsyncIterator

import httpx

from .config import BrainSpec, Settings

log = logging.getLogger("jarvis.brain")


class BrainError(Exception):
    """Ollama unreachable, model missing, or bad reply. Message is safe to show the user."""


class ToolsUnsupportedError(BrainError):
    """Raised when the selected Ollama model rejects the `tools` parameter."""


class ThinkFilter:
    """Streaming remover of <think>...</think> blocks (tags may be split across chunks)."""

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, s: str) -> str:
        self.buf += s
        out = ""
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]  # keep a possible partial closing tag
                    return out
                self.buf, self.inside = self.buf[i + 8:], False
            else:
                i = self.buf.find("<think>")
                if i >= 0:
                    out, self.buf, self.inside = out + self.buf[:i], self.buf[i + 7:], True
                    continue
                keep = max((k for k in range(1, 7) if "<think>".startswith(self.buf[-k:])), default=0)
                out += self.buf[:len(self.buf) - keep]
                self.buf = self.buf[len(self.buf) - keep:]
                return out

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


class Brain:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.current = settings.boot_brain
        self.adhoc: dict[str, BrainSpec] = {}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=3))
        self.last_stats: dict = {}

    # ---- specs ---------------------------------------------------------------------------------------
    @property
    def spec(self) -> BrainSpec:
        return self.settings.brains.get(self.current) or self.adhoc[self.current]

    @property
    def host(self) -> str:
        return self.spec.base_url.rsplit("/v1", 1)[0]

    # ---- ollama state --------------------------------------------------------------------------------
    async def _get(self, path: str, timeout: float = 5):
        try:
            r = await self.client.get(self.host + path, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, OSError) as e:
            raise BrainError(f"Ollama is not reachable at {self.host} ({type(e).__name__}). Start the Ollama app and try again.") from e

    async def installed(self) -> list[str]:
        return [m["name"] for m in (await self._get("/api/tags"))["models"]]

    async def running(self) -> list[dict]:
        return (await self._get("/api/ps"))["models"]

    async def health(self) -> dict:
        try:
            v = (await self._get("/api/version", 2))["version"]
            return {"up": True, "version": v, "models": await self.installed(), "loaded": [m["name"] for m in await self.running()]}
        except BrainError as e:
            return {"up": False, "error": str(e)}

    async def catalogue(self) -> list[dict]:
        """Every selectable model: configured brains first, then other installed Ollama models."""
        inst = await self.installed()
        out = [{"key": k, "model": s.model, "thinking": s.thinking, "installed": s.model in inst, "current": k == self.current}
               for k, s in self.settings.brains.items()]
        known = {s.model for s in self.settings.brains.values()}
        out += [{"key": m, "model": m, "thinking": False, "installed": True, "current": m == self.current} for m in inst if m not in known]
        return out

    async def switch(self, key_or_model: str) -> dict:
        """Select a configured brain or any installed model. Unloads the previous one to free VRAM. Never pulls models."""
        inst = await self.installed()
        if key_or_model in self.settings.brains:
            key, spec = key_or_model, self.settings.brains[key_or_model]
        elif key_or_model in inst:
            base = self.settings.brains[self.settings.boot_brain]
            key, spec = key_or_model, base.model_copy(update={"model": key_or_model, "thinking": False})
            self.adhoc[key] = spec
        else:
            raise BrainError(f"Model {key_or_model!r} is not installed. Installed: {', '.join(inst)}. Run `ollama pull <tag>` if you want it.")
        if spec.model not in inst:
            raise BrainError(f"Model {spec.model!r} is not installed. Run `ollama pull {spec.model}` first.")
        if key != self.current:
            await self.unload(self.spec.model)
            self.current = key
        return {"key": key, "model": spec.model, "thinking": spec.thinking}

    async def unload(self, model: str) -> None:
        try:
            await self.client.post(self.host + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=15)
        except httpx.HTTPError:
            log.warning("could not unload %s", model)

    async def warm(self) -> float:
        t = time.time()
        await self.client.post(self.host + "/api/generate", timeout=300, json={
            "model": self.spec.model, "prompt": "", "keep_alive": self.spec.keep_alive, "options": {"num_ctx": self.spec.num_ctx}})
        return time.time() - t

    # ---- chat ----------------------------------------------------------------------------------------
    def _body(self, messages: list[dict], stream: bool, tools: list[dict] | None = None) -> dict:
        s = self.spec
        body = {
            "model": s.model,
            "messages": messages,
            "stream": stream,
            "think": s.thinking,
            "keep_alive": s.keep_alive,
            "options": {"num_ctx": s.num_ctx, "num_predict": s.max_tokens, "temperature": 0.6},
        }
        if tools:
            body["tools"] = tools
        return body

    async def stream_events(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Stream `/api/chat` yielding {"type": "token", "text"} and {"type": "tool_calls", "calls"}."""
        flt, t0, first = ThinkFilter(), time.time(), None
        collected_calls: list[dict] = []
        try:
            async with self.client.stream("POST", self.host + "/api/chat", json=self._body(messages, True, tools=tools)) as r:
                if r.status_code == 404:
                    raise BrainError(f"Model {self.spec.model!r} not found in Ollama. Run `ollama pull {self.spec.model}`.")
                if r.status_code == 400 and tools:
                    body_txt = (await r.aread()).decode("utf-8", errors="replace")
                    if "tool" in body_txt.lower():
                        raise ToolsUnsupportedError(body_txt)
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    c = json.loads(line)
                    if c.get("error"):
                        if tools and "tool" in str(c["error"]).lower():
                            raise ToolsUnsupportedError(c["error"])
                        raise BrainError(c["error"])
                    msg = c.get("message") or {}
                    if msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            fn = tc.get("function") or {}
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"raw": args}
                            collected_calls.append({
                                "id": tc.get("id") or f"call_{len(collected_calls)}",
                                "name": fn.get("name", ""),
                                "arguments": args if isinstance(args, dict) else {},
                            })
                    piece = flt.feed(msg.get("content", ""))  # `thinking` field intentionally ignored
                    if piece:
                        first = first or time.time() - t0
                        yield {"type": "token", "text": piece}
                    if c.get("done"):
                        n = c.get("eval_count", 0)
                        ed = c.get("eval_duration") or 1
                        self.last_stats = {
                            "ttft": first,
                            "tokens": n,
                            "tps": n / (ed / 1e9),
                            "total": time.time() - t0,
                            "prompt_tokens": c.get("prompt_eval_count", 0),
                            "prompt_s": (c.get("prompt_eval_duration") or 0) / 1e9,
                            "load_s": (c.get("load_duration") or 0) / 1e9,
                            "model": self.spec.model,
                            "truncated": c.get("done_reason") == "length",
                        }
        except httpx.HTTPError as e:
            raise BrainError(f"Ollama request failed ({type(e).__name__}). Is Ollama running?") from e
        tail = flt.flush()
        if tail:
            yield {"type": "token", "text": tail}
        if collected_calls:
            yield {"type": "tool_calls", "calls": collected_calls}

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        async for ev in self.stream_events(messages):
            if ev["type"] == "token":
                yield ev["text"]

    async def complete(self, messages: list[dict]) -> str:
        return "".join([p async for p in self.stream(messages)]).strip()
