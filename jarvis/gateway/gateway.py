"""Unified Model Gateway orchestrating local Ollama + gated cloud providers with rate/token governance & fallback."""
import collections
import logging
import threading
import time
from typing import AsyncIterator

from ..brain import Brain
from ..db.engine import DatabaseManager
from ..db.models import LLMUsageLogRow
from ..security.pii_firewall import scrub_messages
from .interface import GatewayEvent, GatewayRequest
from .providers import (
    AnthropicAdapter,
    CloudGatewayError,
    GeminiAdapter,
    OPENAI_COMPAT_PROVIDERS,
    OpenAICompatAdapter,
    check_cloud_egress_allowed,
    parse_model_key,
)

log = logging.getLogger("jarvis.gateway")


class ModelGateway:
    """Rate-limited, PII-scrubbed, telemetry-logging router over Ollama and external model providers."""

    def __init__(self, brain: Brain, db: DatabaseManager | None = None):
        self.brain = brain
        self.db = db
        self._lock = threading.Lock()
        self._rpm_window: collections.deque[float] = collections.deque()

    @property
    def settings(self):
        return self.brain.settings

    def _check_rate_and_budget(self) -> None:
        now = time.time()
        limit = int(self.settings.gateway.default_rpm_limit)
        with self._lock:
            while self._rpm_window and now - self._rpm_window[0] > 60.0:
                self._rpm_window.popleft()
            if len(self._rpm_window) >= limit:
                raise CloudGatewayError(f"Gateway RPM limit ({limit}/min) reached.")
            self._rpm_window.append(now)

        if self.db is not None:
            since = now - 86400.0
            with self.db.sync_session() as s:
                rows = s.query(LLMUsageLogRow).filter(LLMUsageLogRow.created_at >= since).all()
                used = sum(r.prompt_tokens + r.completion_tokens for r in rows)
                if used >= self.settings.gateway.daily_token_budget:
                    raise CloudGatewayError(
                        f"Daily token budget ({self.settings.gateway.daily_token_budget}) exhausted ({used} used in 24h)."
                    )

    def _record_usage(self, req: GatewayRequest, usage: dict, status: str = "ok") -> None:
        if self.db is None:
            return
        try:
            with self.db.sync_session() as s:
                s.add(
                    LLMUsageLogRow(
                        session_id=req.session_id,
                        run_id=req.run_id,
                        decision_layer=req.decision_layer,
                        provider=str(usage.get("provider") or "ollama"),
                        model=str(usage.get("model") or req.model_key),
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        latency_ms=float(usage.get("latency_ms") or 0.0),
                        estimated_cost_usd=float(usage.get("cost_usd") or 0.0),
                        status=status,
                    )
                )
                s.commit()
        except Exception:
            pass

    def usage_summary(self) -> dict:
        if self.db is None:
            return {"total_calls": 0, "tokens_24h": 0, "by_provider": {}}
        since = time.time() - 86400.0
        with self.db.sync_session() as s:
            rows = s.query(LLMUsageLogRow).filter(LLMUsageLogRow.created_at >= since).all()
        by_p: dict[str, dict] = {}
        total_tokens = 0
        for r in rows:
            tok = r.prompt_tokens + r.completion_tokens
            total_tokens += tok
            b = by_p.setdefault(r.provider, {"calls": 0, "tokens": 0, "avg_ms": 0.0})
            b["calls"] += 1
            b["tokens"] += tok
            b["avg_ms"] = round((b["avg_ms"] * (b["calls"] - 1) + r.latency_ms) / b["calls"], 1)
        return {
            "total_calls": len(rows),
            "tokens_24h": total_tokens,
            "daily_budget": self.settings.gateway.daily_token_budget,
            "by_provider": by_p,
        }

    async def stream_with_fallback(
        self,
        req: GatewayRequest,
        *,
        fallback_models: list[str] | None = None,
    ) -> AsyncIterator[GatewayEvent]:
        """Try primary model key and bounded fallback chain with PII scrubbing for cloud calls."""
        candidates = [req.model_key] + [m for m in (fallback_models or []) if m != req.model_key]
        last_err: Exception | None = None

        for key in candidates:
            prov, model_id = parse_model_key(key, self.settings)
            try:
                self._check_rate_and_budget()
                if prov == "ollama":
                    t0 = time.perf_counter()
                    # Ensure target local brain key/model without dropping existing settings if matching
                    if key != self.brain.current and key in self.settings.brains:
                        await self.brain.switch(key)
                    txt_len = 0
                    async for ev in self.brain.stream_events(req.messages, tools=req.tools):
                        if ev["type"] == "token":
                            txt_len += len(ev["text"].split())
                            yield GatewayEvent(type="token", text=ev["text"])
                        elif ev["type"] == "tool_calls":
                            yield GatewayEvent(type="tool_calls", calls=ev["calls"])
                    st = dict(self.brain.last_stats or {})
                    usage = {
                        "provider": "ollama",
                        "model": st.get("model") or model_id,
                        "prompt_tokens": int(st.get("prompt_tokens") or 0),
                        "completion_tokens": int(st.get("tokens") or txt_len),
                        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                    }
                    self._record_usage(req, usage, status="ok")
                    yield GatewayEvent(type="done", usage=usage)
                    return

                # Cloud adapters strictly require Online Mode + allow_cloud + key + outbound secret scrubbing
                api_key = check_cloud_egress_allowed(prov, self.settings)
                clean_msgs = req.messages
                if req.scrub_secrets and self.settings.gateway.scrub_outbound_secrets:
                    clean_msgs, hits = scrub_messages(req.messages)
                    if hits:
                        log.warning("Outbound PII/Secret firewall redacted %s before calling %s", hits, prov)
                scrubbed_req = GatewayRequest(**{**req.__dict__, "messages": clean_msgs, "model_key": key})

                allowed = {h.lower() for h in self.settings.providers.allowed_cloud_hosts}
                adapter: OpenAICompatAdapter | GeminiAdapter | AnthropicAdapter
                if prov in OPENAI_COMPAT_PROVIDERS:
                    url_attr, _ = OPENAI_COMPAT_PROVIDERS[prov]
                    adapter = OpenAICompatAdapter(prov, getattr(self.settings.providers, url_attr), allowed_hosts=allowed)
                elif prov == "gemini":
                    adapter = GeminiAdapter(self.settings.providers.gemini_base_url, allowed_hosts=allowed)
                elif prov == "anthropic":
                    adapter = AnthropicAdapter(self.settings.providers.anthropic_base_url, allowed_hosts=allowed)
                else:
                    raise CloudGatewayError(f"Unsupported provider {prov!r}")

                final_usage: dict = {"provider": prov, "model": model_id}
                async for ev in adapter.complete(
                    scrubbed_req,
                    api_key=api_key,
                    model_id=model_id,
                    max_fetch_bytes=self.settings.internet.max_fetch_bytes,
                ):
                    if ev.type == "done":
                        final_usage = ev.usage
                    yield ev
                self._record_usage(req, final_usage, status="ok")
                return
            except Exception as e:
                last_err = e
                self._record_usage(req, {"provider": prov, "model": model_id}, status="error")
                log.warning("Gateway provider %s (%s) failed: %s", prov, model_id, e)
                continue

        raise CloudGatewayError(f"All model candidates failed: {last_err}")
