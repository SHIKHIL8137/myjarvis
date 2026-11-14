"""Dynamic model catalog: discovery (OpenAI, OpenRouter, Ollama), TTL cache, filters, task recommendations and validated selection.

It adds NO second provider/registry: the active model stays `Brain.current` (switching goes through `Brain.switch`), credentials come from
`config.get_provider_api_key`, base URLs from `settings.providers`, and every network call is gated by the same offline rule as `Brain._cloud_allowed`.
Keys are never returned, logged or stored here. Provider URLs are never taken from chat input.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from . import audit
from .netsec import safe_request
from .config import get_provider_api_key, has_provider_api_key
from .security.pii_firewall import redact_secrets

TTL_S = 600.0
FETCH_TIMEOUT_S = 12.0
_KEY_RE = re.compile(r"^[a-z]+:[A-Za-z0-9._/:@+-]{1,120}$")
_OPENAI_CHAT = re.compile(r"^(?:gpt-|o\d|chatgpt-)")
_OPENAI_NOT_CHAT = re.compile(r"(?:embedding|moderation|tts|whisper|dall-e|image|realtime|audio|transcribe|search|computer-use)")
TASKS = ("general", "coding", "translation", "reasoning", "long_documents", "fast", "low_cost", "vision", "speech")


def _err(prov: str, status: int | None = None, exc: Exception | None = None) -> tuple[str, str]:
    """(category, safe message) - never contains response bodies or keys."""
    if exc is not None:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout", f"{prov} did not answer within {int(FETCH_TIMEOUT_S)}s."
        return "network", f"Could not reach {prov} ({type(exc).__name__})."
    if status in (401, 403):
        return "auth", f"{prov} rejected the configured credential (HTTP {status}). Check the key in Settings."
    if status == 429:
        return "rate_limited", f"{prov} rate-limited the request (HTTP 429). Try again shortly."
    return "http", f"{prov} answered HTTP {status}."


def _price(v: Any) -> float | None:
    """OpenRouter prices are USD per token as strings; return USD per 1M tokens."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 1_000_000, 4) if f >= 0 else None  # OpenRouter uses -1 for routers with variable pricing: unknown, not "negative"


class ModelCatalog:
    def __init__(self, assistant, transport: httpx.AsyncBaseTransport | None = None):
        self.a = assistant
        self.transport = transport  # tests inject a mock transport; production uses the real network
        self.cache: dict[str, dict] = {}

    # ---- gating ----------------------------------------------------------------------------------------
    def _network_block(self) -> str:
        s = self.a.settings
        if not (s.internet.enabled and audit.is_online_enabled()):
            return "offline_blocked"  # same rule safe_request enforces: no internet toggle / Local Mode => no packet leaves
        if not s.providers.allow_cloud:
            return "cloud_disabled"
        return ""

    # ---- discovery -------------------------------------------------------------------------------------
    async def _get(self, base: str, headers: dict) -> dict:
        """GET {base}/models through the project's hardened egress (https only, DNS/SSRF checks, host pinned to the configured base URL, audited)."""
        from urllib.parse import urlsplit
        import json as _json

        url = base.rstrip("/") + "/models"
        spec = self.a.settings.internet.model_copy(update={"enabled": True, "timeout_s": FETCH_TIMEOUT_S, "max_fetch_bytes": 4_000_000})
        host = (urlsplit(url).hostname or "").lower()
        tr = self.transport
        tf = (lambda _target: tr) if tr is not None else None
        res = await safe_request("GET", url, spec, allowed_hosts={host}, headers=headers, max_bytes=4_000_000, transport_factory=tf)
        if res.status_code != 200:
            raise _HttpFail(res.status_code)
        return _json.loads(res.body.decode("utf-8", errors="replace"))

    async def _fetch_openrouter(self) -> list[dict]:
        key = get_provider_api_key("openrouter") or ""
        r = await self._get(self.a.settings.providers.openrouter_base_url, {"Authorization": f"Bearer {key}"} if key else {})
        out = []
        for m in r.get("data", []):
            mid = str(m.get("id") or "")
            if not mid:
                continue
            pr = m.get("pricing") or {}
            pin, pout = _price(pr.get("prompt")), _price(pr.get("completion"))
            arch = m.get("architecture") or {}
            params = list(m.get("supported_parameters") or [])
            out.append({
                "key": f"openrouter:{mid}", "model": mid, "provider": "openrouter", "name": m.get("name") or mid,
                "free": mid.endswith(":free") or (pin == 0 and pout == 0),
                "price_in": pin, "price_out": pout, "context": m.get("context_length"),
                "inputs": list(arch.get("input_modalities") or []), "outputs": list(arch.get("output_modalities") or []),
                "tools": ("tools" in params) if params else None, "reasoning": ("reasoning" in params) if params else None,
                "metadata": "provider",
            })
        return out

    async def _fetch_openai(self) -> list[dict]:
        r = await self._get(self.a.settings.providers.openai_base_url, {"Authorization": f"Bearer {get_provider_api_key('openai')}"})
        out = []
        for m in r.get("data", []):
            mid = str(m.get("id") or "")
            if _OPENAI_CHAT.match(mid) and not _OPENAI_NOT_CHAT.search(mid):
                # the models endpoint gives only id/owner: price, context and capabilities are NOT provided, so they stay None (unknown)
                out.append({"key": f"openai:{mid}", "model": mid, "provider": "openai", "name": mid, "free": False, "price_in": None, "price_out": None,
                            "context": None, "inputs": [], "outputs": [], "tools": None, "reasoning": None, "metadata": "id-only"})
        return out

    async def _local(self) -> list[dict]:
        b = self.a.brain
        try:
            inst = await b.installed()
        except Exception:
            inst = []
        keys = {s.model: k for k, s in self.a.settings.brains.items()}
        return [{"key": keys.get(m, m), "model": m, "provider": "ollama", "name": m, "free": True, "price_in": 0.0, "price_out": 0.0, "context": None,
                 "inputs": [], "outputs": [], "tools": None, "reasoning": None, "metadata": "installed"} for m in inst]

    async def refresh(self, provider: str, *, force: bool = False) -> dict:
        """Return provider state: {provider, status, models, fetched_at, stale, error, error_category}."""
        provider = provider.lower()
        now = time.time()
        ent = self.cache.get(provider)
        if provider == "ollama":
            models = await self._local()
            ent = {"models": models, "fetched_at": now, "error": "", "category": "", "status": "ready" if models else "empty"}
            self.cache[provider] = ent
            return self._view(provider, ent, now)
        if provider not in ("openai", "openrouter"):
            return {"provider": provider, "status": "unsupported", "models": [], "stale": False, "error": "No catalog adapter for this provider.", "error_category": "unsupported"}
        connected = has_provider_api_key(provider)
        if ent and not force and now - ent["fetched_at"] < TTL_S and not ent.get("category"):
            return self._view(provider, ent, now)
        if provider == "openai" and not connected:
            return {"provider": provider, "status": "not_connected", "models": [], "stale": False, "error": "No API key configured.", "error_category": "auth_missing", "connected": False}
        block = self._network_block()
        if block:
            msg = "Offline mode is enforced: no catalog request was made." if block == "offline_blocked" else "Cloud providers are disabled in settings."
            return self._failed(provider, ent, block, msg, now, connected)
        try:
            models = await (self._fetch_openrouter() if provider == "openrouter" else self._fetch_openai())
        except _HttpFail as e:
            cat, msg = _err(provider, e.status)
            return self._failed(provider, ent, cat, msg, now, connected)
        except Exception as e:  # noqa: BLE001 - classified, never echoed raw
            cat, msg = _err(provider, exc=e)
            return self._failed(provider, ent, cat, msg, now, connected)
        ent = {"models": models, "fetched_at": now, "error": "", "category": "", "status": "ready" if models else "empty"}
        self.cache[provider] = ent
        try:
            audit.record_external("model_catalog", provider, provider, True, reason=f"{len(models)} models")
        except Exception:  # noqa: BLE001
            pass
        return self._view(provider, ent, now, connected)

    def _failed(self, provider: str, ent: dict | None, cat: str, msg: str, now: float, connected: bool) -> dict:
        msg = redact_secrets(msg, mask_email=False)[0]
        if ent and ent.get("models"):  # keep serving the old list, but flagged: a cache is never "freshly verified"
            ent = {**ent, "category": cat, "error": msg}
            self.cache[provider] = ent
            return self._view(provider, ent, now, connected, stale=True)
        return {"provider": provider, "status": cat, "models": [], "stale": False, "error": msg, "error_category": cat, "connected": connected}

    def _view(self, provider: str, ent: dict, now: float, connected: bool | None = None, stale: bool | None = None) -> dict:
        if connected is None:
            connected = provider == "ollama" or has_provider_api_key(provider)
        age = now - ent["fetched_at"]
        is_stale = bool(stale) or age >= TTL_S
        cur = self.a.brain.current
        models = []
        for m in ent["models"]:
            models.append({**m, "current": m["key"] == cur, "usable": bool(connected) and (provider == "ollama" or self.a.settings.providers.allow_cloud)})
        return {"provider": provider, "status": "stale" if is_stale and ent.get("models") else ent.get("status", "ready"), "models": models,
                "fetched_at": ent["fetched_at"], "age_s": round(age, 1), "stale": is_stale, "error": ent.get("error", ""),
                "error_category": ent.get("category", ""), "connected": bool(connected)}

    # ---- listing ---------------------------------------------------------------------------------------
    async def listing(self, *, provider: str = "", free: bool | None = None, q: str = "", refresh: bool = False, limit: int = 300) -> dict:
        provs = [provider.lower()] if provider else ["ollama", "openai", "openrouter"]
        states, rows = [], []
        for p in provs:
            st = await self.refresh(p, force=refresh)
            states.append({k: v for k, v in st.items() if k != "models"} | {"count": len(st["models"])})
            rows += st["models"]
        if free is not None:
            rows = [m for m in rows if bool(m["free"]) == free]
        if q:
            terms = [t for t in re.split(r"\s+", q.lower().strip()) if t]
            rows = [m for m in rows if all(t in f"{m['model']} {m['name']} {m['provider']}".lower() for t in terms)]
        rows.sort(key=lambda m: (not m["current"], m["provider"] != "ollama", m["provider"], m["model"]))
        cur = self.a.brain.current
        return {"current": {"key": cur, "model": self.a.brain.spec.model, "provider": self.a.brain.provider}, "providers": states,
                "models": rows[:limit], "total": len(rows), "offline_enforced": bool(self.a.settings.offline_enforced)}

    def find(self, text: str, *, rows: list[dict] | None = None) -> list[dict]:
        """Exact id/key first, then unique-ish substring on id or name. Only models actually discovered can match."""
        pool = rows if rows is not None else [m for e in self.cache.values() for m in e["models"]]
        want = re.sub(r"[\s_]+", "-", (text or "").strip().lower())
        if not want:
            return []
        exact = [m for m in pool if want in (m["key"].lower(), m["model"].lower())]
        if exact:
            return exact
        norm = lambda s: re.sub(r"[\s_.]+", "-", s.lower())  # noqa: E731
        return [m for m in pool if want in norm(m["model"]) or want in norm(m["name"])]

    # ---- recommendations -------------------------------------------------------------------------------
    def recommend(self, task: str, rows: list[dict] | None = None, n: int = 3) -> dict:
        task = task if task in TASKS else "general"
        pool = [m for m in (rows if rows is not None else [m for e in self.cache.values() for m in e["models"]]) if m.get("provider")]
        scored = []
        for m in pool:
            sc, why = _score(task, m)
            if sc > 0:
                scored.append((sc, m, why))
        scored.sort(key=lambda t: (-t[0], bool(not t[1]["free"]), t[1]["model"]))
        recs = [{"key": m["key"], "model": m["model"], "provider": m["provider"], "free": m["free"], "price_in": m["price_in"], "price_out": m["price_out"],
                 "context": m["context"], "score": sc, "reasons": why, "needs_paid_confirmation": not m["free"]} for sc, m, why in scored[:n]]
        return {"task": task, "recommendations": recs, "considered": len(pool),
                "note": "Ranked only from provider metadata (price, context, modalities, tool support) and, where marked [name hint], model-family names. No benchmark data is used."}

    # ---- selection -------------------------------------------------------------------------------------
    async def select(self, key: str, *, confirm_paid: bool = False) -> dict:
        key = (key or "").strip()
        a = self.a
        if not _KEY_RE.match(key) and key not in a.settings.brains:
            return {"ok": False, "status": "invalid", "error": "That is not a valid model identifier."}
        prov = key.split(":", 1)[0] if ":" in key and key not in a.settings.brains else "ollama"
        if prov != "ollama":
            if prov not in ("openai", "openrouter"):
                return {"ok": False, "status": "invalid", "error": f"Provider {prov!r} is not supported for catalog switching."}
            if not has_provider_api_key(prov):
                return {"ok": False, "status": "not_connected", "error": f"{prov} is not connected (no API key configured)."}
            block = self._network_block()
            if block:
                return {"ok": False, "status": block, "error": "Cloud models are blocked while offline mode is enforced." if block == "offline_blocked" else "Cloud providers are disabled in settings."}
            st = self.cache.get(prov) or None
            if not st:
                st0 = await self.refresh(prov)
                if not st0["models"]:
                    return {"ok": False, "status": st0.get("error_category") or "empty", "error": st0.get("error") or "Catalog is empty."}
            if self.cache[prov].get("category") == "auth":
                return {"ok": False, "status": "auth", "error": f"{prov} rejected the configured credential on the last check. Fix the key in Settings, then refresh."}
            mod = next((m for m in self.cache[prov]["models"] if m["key"] == key), None)
            if not mod:
                return {"ok": False, "status": "unavailable", "error": f"{key} was not found in the {prov} catalog for this account."}
            if not mod["free"] and not confirm_paid:
                return {"ok": False, "status": "needs_confirmation", "model": mod, "error": "This is a paid model; confirm to switch.",
                        "pricing": {"input_per_1m_usd": mod["price_in"], "output_per_1m_usd": mod["price_out"]}}
        try:
            before = a.brain.current
            info = await a.brain.switch(key)
        except Exception as e:  # BrainError etc: message already scrubbed of keys by redact below
            return {"ok": False, "status": "rejected", "error": redact_secrets(str(e), mask_email=False)[0]}
        a.bus.publish({"type": "model", **info})
        try:
            audit.record_external("model_switch", f"{before} -> {info['key']}", info["provider"], True, reason="paid-confirmed" if confirm_paid else "user-selected")
        except Exception:  # noqa: BLE001
            pass
        a.warm_brain()
        return {"ok": True, "status": "switched", "current": {"key": info["key"], "model": info["model"], "provider": info["provider"]}}


class _HttpFail(Exception):
    def __init__(self, status: int):
        self.status = status


_HINTS = {
    "coding": re.compile(r"coder|codex|code|deepseek|gpt-4\.1|qwen3|devstral|claude|o\d-?(?:mini)?$", re.I),
    "reasoning": re.compile(r"(?:^|[-/:])o\d|r1|reason|thinking|think|qwq|gpt-5", re.I),
    "fast": re.compile(r"mini|nano|flash|haiku|small|lite|instruct|\b[1-9]b\b|:free", re.I),
    "translation": re.compile(r"gpt-4|gpt-5|llama-3|qwen|gemma|mistral|claude|gemini|aya|nllb|madlad", re.I),
    "speech": re.compile(r"whisper|transcribe|audio|speech", re.I),
}


def _score(task: str, m: dict) -> tuple[float, list[str]]:
    why: list[str] = []
    sc = 0.0
    ctx, pin = m.get("context"), m.get("price_in")
    name = f"{m['model']}"
    ins = [i.lower() for i in m.get("inputs") or []]
    if task == "vision":
        if "image" not in ins:
            return 0, []  # never claim vision without provider metadata
        sc += 5
        why.append("provider metadata lists image input")
    elif task == "speech":
        if not _HINTS["speech"].search(name):
            return 0, []
        sc += 3
        why.append("[name hint] audio/speech model family")
    elif task == "long_documents":
        if not ctx:
            return 0, []
        sc += min(5.0, ctx / 200_000)
        why.append(f"context window {ctx:,} tokens (provider metadata)")
    elif task == "low_cost":
        if m.get("free"):
            sc += 5
            why.append("free tier (not guaranteed unlimited)")
        elif pin is not None:
            sc += max(0.1, 3 - pin / 2)
            why.append(f"low listed input price ${pin}/1M tokens")
        else:
            return 0, []
    else:
        hint = _HINTS.get(task)
        if hint and hint.search(name):
            sc += 3
            why.append(f"[name hint] {task}-oriented model family")
        elif task != "general":
            sc += 0.5
        if task == "coding" and m.get("tools"):
            sc += 1
            why.append("supports tool calling (provider metadata)")
        if task == "reasoning" and m.get("reasoning"):
            sc += 2
            why.append("advertises reasoning parameters (provider metadata)")
        if task in ("general", "translation") and ctx and ctx >= 32_000:
            sc += 1
            why.append(f"context window {ctx:,} tokens")
        if task == "fast" and m.get("provider") == "ollama":
            sc += 1.5
            why.append("runs locally (no network latency)")
    if m.get("free") and task not in ("low_cost",):
        sc += 0.8
        why.append("free" if m["provider"] != "ollama" else "installed locally, no cost")
    elif pin is not None and task != "low_cost":
        sc -= min(2.0, pin / 5)
        why.append(f"paid: ${pin}/1M input tokens")
    if not why:
        why.append("general-purpose chat model")
    return round(sc, 2), why
