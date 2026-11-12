"""Ollama client + model manager (native /api/chat: it is the only way to set num_ctx per request).
Reasoning text never reaches callers: the `thinking` field is dropped and any <think>..</think> is filtered.
Supports Ollama tool calling for approved web search/fetch tools with clean fallback if a model lacks tool support."""
import json
import logging
import time
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx

from . import audit
from .config import BrainSpec, InternetSpec, Settings, get_provider_api_key, has_provider_api_key
from .netsec import NetworkSecurityError, safe_request
from .security.pii_firewall import scrub_messages

log = logging.getLogger("jarvis.brain")


class BrainError(Exception):
    """Ollama unreachable, model missing, or bad reply. Message is safe to show the user."""


class ToolsUnsupportedError(BrainError):
    """Raised when the selected Ollama model rejects the `tools` parameter."""


class ThinkFilter:
    """Streaming remover of <think>...</think> blocks, including unpaired </think> prefixes."""

    def __init__(self, *, defer_unopened: bool = False):
        self.buf, self.inside = "", False
        self._defer = bool(defer_unopened)

    @property
    def deferred(self) -> bool:
        return self._defer

    def note_separate_thinking(self) -> str:
        """Called when the backend emits an explicit `message.thinking` field, proving content isn't raw reasoning."""
        if not self._defer:
            return ""
        self._defer = False
        return self.feed("")

    def feed(self, s: str) -> str:
        self.buf += s
        out = ""
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]  # keep a possible partial closing tag
                    return out
                self.buf, self.inside = self.buf[i + 8 :], False
            else:
                open_idx = self.buf.find("<think>")
                close_idx = self.buf.find("</think>")
                if close_idx >= 0 and (open_idx < 0 or close_idx < open_idx):
                    # Unpaired closing tag (e.g., model prompt template started inside <think>)
                    self.buf = self.buf[close_idx + 8 :]
                    self._defer = False
                    out = ""
                    continue
                if open_idx >= 0:
                    if not self._defer:
                        out += self.buf[:open_idx]
                    elif self.buf[:open_idx]:
                        out += self.buf[:open_idx]
                    self._defer = False
                    self.buf, self.inside = self.buf[open_idx + 7 :], True
                    continue
                if self._defer:
                    return out
                keep_open = max((k for k in range(1, 7) if "<think>".startswith(self.buf[-k:])), default=0)
                keep_close = max((k for k in range(1, 8) if "</think>".startswith(self.buf[-k:])), default=0)
                keep = max(keep_open, keep_close)
                out += self.buf[: len(self.buf) - keep]
                self.buf = self.buf[len(self.buf) - keep :]
                return out

    def flush(self) -> str:
        if self.inside:
            self.buf = ""
            return ""
        self._defer = False
        out = self.buf
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

    @property
    def provider(self) -> str:
        p = getattr(self.spec, "provider", None)
        if p:
            return str(p).lower()
        for prefix in ("openai", "anthropic", "openrouter", "gemini", "huggingface"):
            if str(self.current).startswith(f"{prefix}:"):
                return prefix
        return "ollama"

    def _cloud_allowed(self, prov: str) -> str:
        if self.settings.offline_enforced and not (self.settings.internet.enabled and audit.is_online_enabled()):
            raise BrainError(f"Cloud model {prov!r} is blocked in Local Mode. Enable Online Assisted Mode and providers.allow_cloud first.")
        if not self.settings.providers.allow_cloud:
            raise BrainError(f"Cloud provider {prov!r} is disabled (providers.allow_cloud=false). Local mode remains active.")
        key = get_provider_api_key(prov)
        if not key:
            raise BrainError(f"API key for cloud provider {prov!r} is not configured.")
        return key

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
        """Every selectable model: configured local brains, installed Ollama models, and optional cloud entries."""
        inst = await self.installed()
        out = [{
            "key": k,
            "model": s.model,
            "provider": getattr(s, "provider", "ollama"),
            "local": True,
            "thinking": s.thinking,
            "tools": True,
            "installed": s.model in inst,
            "current": k == self.current,
        } for k, s in self.settings.brains.items()]
        known = {s.model for s in self.settings.brains.values()}
        out += [{
            "key": m,
            "model": m,
            "provider": "ollama",
            "local": True,
            "thinking": False,
            "tools": True,
            "installed": True,
            "current": m == self.current,
        } for m in inst if m not in known]

        p = self.settings.providers
        cloud_specs = [
            ("openai", p.openai_model),
            ("anthropic", p.anthropic_model),
            ("openrouter", p.openrouter_model),
            ("gemini", p.gemini_model),
            ("huggingface", p.huggingface_model),
        ]
        for prov_name, prov_model in cloud_specs:
            if has_provider_api_key(prov_name):
                k = f"{prov_name}:{prov_model}"
                out.append({
                    "key": k,
                    "model": prov_model,
                    "provider": prov_name,
                    "local": False,
                    "thinking": False,
                    "tools": prov_name != "gemini",
                    "installed": bool(p.allow_cloud and self.settings.internet.enabled),
                    "current": self.current == k,
                })
        return out

    async def resolve_choice(self, want: str) -> str | None:
        """Resolve natural alias ('fast', 'reasoning', 'openrouter', 'gemini', 'openai', etc.)."""
        q = (want or "").strip().lower()
        aliases = {
            "fast": "local", "quick": "local", "default": self.settings.boot_brain, "normal": "local",
            "instruct": "local", "local": "local", "thinking": "local_thinking", "reasoning": "local_thinking",
            "deep": "local_thinking", "smart": "local_thinking", "best reasoning": "local_thinking",
            "best installed reasoning model": "local_thinking",
        }
        if q in aliases:
            return aliases[q]
        if q.startswith("openai") or q == "gpt":
            return f"openai:{self.settings.providers.openai_model}"
        if q.startswith("anthropic") or "claude" in q:
            return f"anthropic:{self.settings.providers.anthropic_model}"
        if q.startswith("openrouter"):
            return f"openrouter:{self.settings.providers.openrouter_model}"
        if q.startswith("gemini"):
            return f"gemini:{self.settings.providers.gemini_model}"
        if q.startswith("huggingface") or q == "hf":
            return f"huggingface:{self.settings.providers.huggingface_model}"
        inst = await self.installed()
        for m in inst:
            if m.lower() == q:
                return m
        for m in inst:
            if q and (m.lower().startswith(q) or q in m.lower()):
                return m
        return None

    async def switch(self, key_or_model: str) -> dict:
        """Select a configured brain, installed Ollama model, or gated cloud adapter. Never pulls models."""
        if key_or_model.startswith(("openai:", "anthropic:", "openrouter:", "gemini:", "huggingface:")):
            prov, model_id = key_or_model.split(":", 1)
            self._cloud_allowed(prov)
            url_map = {
                "openai": self.settings.providers.openai_base_url,
                "anthropic": self.settings.providers.anthropic_base_url,
                "openrouter": self.settings.providers.openrouter_base_url,
                "gemini": self.settings.providers.gemini_base_url,
                "huggingface": self.settings.providers.huggingface_base_url,
            }
            spec = BrainSpec(base_url=url_map[prov], model=model_id, thinking=False)
            setattr(spec, "provider", prov)
            self.adhoc[key_or_model] = spec
            if key_or_model != self.current:
                if self.provider == "ollama":
                    await self.unload(self.spec.model)
                self.current = key_or_model
            return {"key": key_or_model, "model": model_id, "provider": prov, "thinking": False, "local": False}

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
            if self.provider == "ollama":
                await self.unload(self.spec.model)
            self.current = key
        return {"key": key, "model": spec.model, "provider": "ollama", "thinking": spec.thinking, "local": True}

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

    async def _cloud_complete(self, prov: str, messages: list[dict]) -> AsyncIterator[dict]:
        key = self._cloud_allowed(prov)
        if self.settings.gateway.scrub_outbound_secrets:
            messages, _ = scrub_messages(messages)
        net_spec = InternetSpec(enabled=True, timeout_s=30.0, max_fetch_bytes=self.settings.internet.max_fetch_bytes)
        t0 = time.time()
        try:
            if prov in ("openai", "openrouter", "huggingface", "custom"):
                base_map = {
                    "openai": self.settings.providers.openai_base_url,
                    "openrouter": self.settings.providers.openrouter_base_url,
                    "huggingface": self.settings.providers.huggingface_base_url,
                    "custom": self.settings.providers.custom_base_url,
                }
                url = base_map[prov].rstrip("/") + "/chat/completions"
                host = (urlsplit(url).hostname or "").lower()
                hdrs = {"Authorization": f"Bearer {key}"}
                if prov == "openrouter":
                    hdrs["HTTP-Referer"] = "https://localhost.jarvis.local"
                res = await safe_request(
                    "POST",
                    url,
                    net_spec,
                    allowed_hosts={host} if host else None,
                    json_body={"model": self.spec.model, "messages": messages, "max_tokens": self.spec.max_tokens},
                    headers=hdrs,
                )
                data = json.loads(res.body.decode("utf-8"))
                txt = data["choices"][0]["message"]["content"]
            elif prov == "gemini":
                url = f"{self.settings.providers.gemini_base_url.rstrip('/')}/models/{self.spec.model}:generateContent"
                host = (urlsplit(url).hostname or "").lower()
                sys_parts = [m["content"] for m in messages if m["role"] == "system"]
                contents = [
                    {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"
                ]
                body = {"contents": contents, "generationConfig": {"maxOutputTokens": self.spec.max_tokens}}
                if sys_parts:
                    body["systemInstruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}
                res = await safe_request("POST", url, net_spec, allowed_hosts={host} if host else None, json_body=body, headers={"x-goog-api-key": key})
                data = json.loads(res.body.decode("utf-8"))
                parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts)
            elif prov == "anthropic":
                url = self.settings.providers.anthropic_base_url.rstrip("/") + "/messages"
                host = (urlsplit(url).hostname or "").lower()
                sys_msg = "\n".join(m["content"] for m in messages if m["role"] == "system")
                chat_msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
                res = await safe_request(
                    "POST",
                    url,
                    net_spec,
                    allowed_hosts={host} if host else None,
                    json_body={"model": self.spec.model, "system": sys_msg, "messages": chat_msgs, "max_tokens": self.spec.max_tokens},
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                data = json.loads(res.body.decode("utf-8"))
                txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            else:
                raise BrainError(f"Unsupported cloud provider: {prov}")
        except (NetworkSecurityError, ConnectionError) as e:
            raise BrainError(f"Cloud provider {prov} blocked by security policy: {e}") from e
        except Exception as e:
            raise BrainError(f"Cloud request failed: {e}") from e

        self.last_stats = {"ttft": time.time() - t0, "tokens": len(txt.split()), "tps": 0.0, "total": time.time() - t0, "model": self.spec.model, "truncated": False}
        yield {"type": "token", "text": txt}

    async def stream_events(self, messages: list[dict], tools: list[dict] | None = None, *, _allow_think_fallback: bool = True) -> AsyncIterator[dict]:
        """Stream `/api/chat` (or gated cloud provider) yielding {"type": "token", "text"} and {"type": "tool_calls", "calls"}."""
        if self.provider in ("openai", "anthropic", "openrouter", "gemini", "huggingface", "custom"):
            async for ev in self._cloud_complete(self.provider, messages):
                yield ev
            return
        flt = ThinkFilter(defer_unopened=bool(self.spec.thinking and _allow_think_fallback))
        t0, first = time.time(), None
        collected_calls: list[dict] = []
        saw_visible = False
        saw_separate_thinking = False
        body = self._body(messages, True, tools=tools)
        if not _allow_think_fallback:
            body["think"] = False
        try:
            async with self.client.stream("POST", self.host + "/api/chat", json=body) as r:
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
                    if msg.get("thinking"):
                        saw_separate_thinking = True
                        released = flt.note_separate_thinking()
                        if released:
                            saw_visible = True
                            first = first or time.time() - t0
                            yield {"type": "token", "text": released}
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
                        saw_visible = True
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
        if flt.deferred and self.last_stats.get("truncated") and not saw_separate_thinking and _allow_think_fallback:
            # Thinking model hit max_tokens inside unopened content reasoning; retry once cleanly without thinking
            async for ev in self.stream_events(messages, tools=tools, _allow_think_fallback=False):
                yield ev
            return
        tail = flt.flush()
        if tail:
            saw_visible = True
            yield {"type": "token", "text": tail}
        if collected_calls:
            yield {"type": "tool_calls", "calls": collected_calls}
        elif _allow_think_fallback and self.spec.thinking and not saw_visible and self.last_stats.get("truncated"):
            # Thinking model spent entire token budget in internal `thinking`; retry once with `think=False` cleanly
            async for ev in self.stream_events(messages, tools=tools, _allow_think_fallback=False):
                yield ev

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        async for ev in self.stream_events(messages):
            if ev["type"] == "token":
                yield ev["text"]

    async def complete(self, messages: list[dict]) -> str:
        return "".join([p async for p in self.stream(messages)]).strip()
