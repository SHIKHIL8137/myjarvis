"""Unified Model Gateway: capability registry, circuit breakers, exponential backoff, central PII firewall, & concurrency control."""
import asyncio
import collections
from dataclasses import dataclass
import logging
import random
import threading
import time
from typing import AsyncIterator

from ..brain import Brain
from ..config import has_provider_api_key
from ..core.resources import ResourceGovernor
from ..db.engine import DatabaseManager
from ..db.models import AuditEventRow, LLMUsageLogRow
from ..security.pii_firewall import detect_secret, scrub_messages
from .interface import GatewayEvent, GatewayRequest, ProviderCapabilities
from .providers import (
    AnthropicAdapter,
    CloudGatewayError,
    GeminiAdapter,
    OPENAI_COMPAT_PROVIDERS,
    OpenAICompatAdapter,
    check_cloud_egress_allowed,
    is_paid_model,
    parse_model_key,
)

log = logging.getLogger("jarvis.gateway")


@dataclass
class CircuitBreakerState:
    failures: int = 0
    state: str = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
    opened_at: float = 0.0
    last_error: str = ""


def estimate_prompt_tokens(messages: list[dict]) -> int:
    """Conservative ~4 chars/token + per-message framing estimate for preflight context gating."""
    total = 0
    for m in messages or []:
        content = m.get("content", "")
        text = content if isinstance(content, str) else str(content)
        total += 4 + max(1, (len(text) + 3) // 4)
    return total


class ModelGateway:
    """Provider-independent, capability-aware, circuit-broken, firewall-enforced model router."""

    def __init__(self, brain: Brain, db: DatabaseManager | None = None, *, settings=None, governor: ResourceGovernor | None = None):
        self.brain = brain
        self.db = db
        self._fallback_settings = settings
        self._lock = threading.Lock()
        self._rpm_window: collections.deque[float] = collections.deque()
        self._breakers: dict[str, CircuitBreakerState] = {}
        self._cap_cache: dict[str, ProviderCapabilities] = {}
        eff_settings = getattr(brain, "settings", None) or settings
        gw_spec = getattr(eff_settings, "gateway", None)
        self.governor = governor or ResourceGovernor(
            max_local=int(getattr(gw_spec, "max_local_concurrency", 1) or 1),
            max_cloud=int(getattr(gw_spec, "max_cloud_concurrency", 3) or 3),
            max_queue=int(getattr(gw_spec, "max_queue_size", 16) or 16),
        )

    @property
    def settings(self):
        return getattr(self.brain, "settings", None) or self._fallback_settings

    def _audit_firewall(self, decision: str, target: str, details: dict) -> None:
        if self.db is None:
            return
        try:
            with self.db.sync_session() as s:
                s.add(AuditEventRow(actor=details.get("actor", "system"), category="egress_firewall", decision=decision, target=target, details=details))
                s.commit()
        except Exception:
            pass

    def enforce_egress_firewall(self, req: GatewayRequest, provider: str) -> list[dict]:
        """Centralized mandatory firewall: cannot be bypassed casually via `scrub_secrets=False`."""
        priv = (req.project_privacy_level or "private").lower()
        if priv in ("confidential", "restricted") and not req.firewall_override_authorized_by:
            self._audit_firewall("blocked_privacy", provider, {"privacy_level": priv, "reason": "confidential project content"})
            raise CloudGatewayError(
                f"Blocked cloud egress to {provider!r}: project privacy classification is {priv!r}.",
                category="privacy_blocked",
                retryable=False,
            )

        if req.firewall_override_authorized_by:
            # Auditable owner override (logged WITHOUT raw secrets)
            self._audit_firewall(
                "owner_override",
                provider,
                {"actor": req.firewall_override_authorized_by, "reason": req.firewall_override_reason[:200], "privacy_level": priv},
            )
            return req.messages

        scrubbed, hits = scrub_messages(req.messages)
        # Double-check remaining secrets post-scrubbing
        residual = any(detect_secret(str(m.get("content", ""))) for m in scrubbed)
        if hits:
            log.warning("Central egress firewall redacted categories=%s before provider=%s", hits, provider)
            self._audit_firewall("redacted", provider, {"categories": hits, "privacy_level": priv})
        if residual:
            self._audit_firewall("blocked_secret", provider, {"categories": hits, "reason": "unredactable secret"})
            raise CloudGatewayError("Blocked cloud egress: unredactable credential detected.", category="secret_blocked", retryable=False)
        return scrubbed

    def list_capabilities(self) -> list[dict]:
        """Dynamic capability registry across local Ollama brains & configured cloud adapters."""
        s = self.settings
        p = s.providers
        now = round(time.time(), 3)
        out: list[ProviderCapabilities] = []

        for key, spec in s.brains.items():
            cap = ProviderCapabilities(
                provider="ollama",
                model_id=spec.model,
                streaming=True,
                tool_calling=("instruct" in spec.model.lower() or "qwen" in spec.model.lower()),
                vision=("vl" in spec.model.lower()),
                json_mode=True,
                embeddings=False,
                local=True,
                max_ctx=spec.num_ctx,
                input_modalities=("text", "image") if "vl" in spec.model.lower() else ("text",),
                rpm_limit=999,
                pricing_tier="local",
                cost_per_1k_usd=0.0,
                free_tier_available=True,
                last_verified_at=now,
            )
            self._cap_cache[key] = cap
            out.append(cap)

        cloud_entries = [
            ("openrouter", p.openrouter_model, OpenAICompatAdapter("openrouter", p.openrouter_base_url).capabilities(p.openrouter_model)),
            ("gemini", p.gemini_model, GeminiAdapter(p.gemini_base_url).capabilities(p.gemini_model)),
            ("huggingface", p.huggingface_model, OpenAICompatAdapter("huggingface", p.huggingface_base_url).capabilities(p.huggingface_model)),
            ("openai", p.openai_model, OpenAICompatAdapter("openai", p.openai_base_url).capabilities(p.openai_model)),
            ("anthropic", p.anthropic_model, AnthropicAdapter(p.anthropic_base_url).capabilities(p.anthropic_model)),
        ]
        for prov, mid, cap in cloud_entries:
            self._cap_cache[f"{prov}:{mid}"] = cap
            out.append(cap)

        return [
            {
                **c.to_dict(),
                "breaker_state": self.breaker_status(c.provider),
                "key_configured": True if c.local else has_provider_api_key(c.provider),
                "paid_authorized": True if c.pricing_tier != "paid" else bool(p.allow_paid_models),
            }
            for c in out
        ]

    def get_capability(self, model_key: str) -> ProviderCapabilities:
        if not self._cap_cache:
            self.list_capabilities()
        if model_key in self._cap_cache:
            return self._cap_cache[model_key]
        prov, mid = parse_model_key(model_key, self.settings)
        if prov == "ollama":
            return ProviderCapabilities(provider="ollama", model_id=mid, streaming=True, tool_calling=True, json_mode=True, local=True, pricing_tier="local")
        if prov in OPENAI_COMPAT_PROVIDERS:
            return OpenAICompatAdapter(prov, getattr(self.settings.providers, OPENAI_COMPAT_PROVIDERS[prov][0])).capabilities(mid)
        if prov == "gemini":
            return GeminiAdapter(self.settings.providers.gemini_base_url).capabilities(mid)
        if prov == "anthropic":
            return AnthropicAdapter(self.settings.providers.anthropic_base_url).capabilities(mid)
        return ProviderCapabilities(provider=prov, model_id=mid, local=False, pricing_tier="paid")

    def breaker_status(self, provider: str) -> str:
        with self._lock:
            br = self._breakers.get(provider)
            if not br:
                return "CLOSED"
            if br.state == "OPEN" and (time.time() - br.opened_at) >= self.settings.gateway.circuit_breaker_cooldown_s:
                br.state = "HALF_OPEN"
            return br.state

    def _record_breaker_success(self, provider: str) -> None:
        with self._lock:
            self._breakers[provider] = CircuitBreakerState(failures=0, state="CLOSED")

    def _record_breaker_failure(self, provider: str, err: str) -> None:
        with self._lock:
            br = self._breakers.setdefault(provider, CircuitBreakerState())
            br.failures += 1
            br.last_error = err[:200]
            if br.failures >= int(self.settings.gateway.circuit_breaker_failures):
                br.state = "OPEN"
                br.opened_at = time.time()

    def inspect_candidate_eligibility(self, req: GatewayRequest, candidates: list[str]) -> tuple[list[str], list[dict]]:
        """Filter model candidates by streaming, tools, vision, json_mode, prompt+completion context fit, paid auth, & breaker."""
        eligible: list[str] = []
        rejections: list[dict] = []
        est_prompt = estimate_prompt_tokens(req.messages)
        total_needed = est_prompt + max(0, int(req.max_tokens))

        for key in candidates:
            prov, mid = parse_model_key(key, self.settings)
            cap = self.get_capability(key)
            reason = ""
            detail = ""
            if req.require_streaming and not cap.streaming:
                reason, detail = "streaming_unsupported", f"{key} does not support streaming"
            elif req.require_tools and not cap.tool_calling:
                reason, detail = "tools_unsupported", f"{key} does not support tool calling"
            elif req.require_vision and not cap.vision:
                reason, detail = "vision_unsupported", f"{key} does not support vision"
            elif req.json_mode and not cap.json_mode:
                reason, detail = "json_mode_unsupported", f"{key} does not support json_mode"
            elif total_needed > cap.max_ctx:
                reason, detail = (
                    "context_window_exceeded",
                    f"estimated tokens ({est_prompt} prompt + {req.max_tokens} max_tokens = {total_needed}) > max_ctx={cap.max_ctx}",
                )
            elif is_paid_model(prov, mid, self.settings) and not self.settings.providers.allow_paid_models:
                reason, detail = "paid_blocked", f"{key} is a paid model and allow_paid_models=False"
            elif self.breaker_status(prov) == "OPEN":
                reason, detail = "circuit_open", f"circuit breaker OPEN for {prov}"

            if reason:
                rejections.append({"model": key, "reason": reason, "detail": detail})
            else:
                eligible.append(key)
        return eligible, rejections

    def select_eligible_candidates(self, req: GatewayRequest, candidates: list[str]) -> list[str]:
        """Filter model candidates by required capabilities, prompt+completion context window, paid auth, & breaker."""
        eligible, _ = self.inspect_candidate_eligibility(req, candidates)
        return eligible

    def _check_rate_and_budget(self) -> None:
        now = time.time()
        limit = int(self.settings.gateway.default_rpm_limit)
        with self._lock:
            while self._rpm_window and now - self._rpm_window[0] > 60.0:
                self._rpm_window.popleft()
            if len(self._rpm_window) >= limit:
                raise CloudGatewayError(f"Gateway RPM limit ({limit}/min) reached.", category="rate_limit", retryable=True)
            self._rpm_window.append(now)

        if self.db is not None:
            since = now - 86400.0
            with self.db.sync_session() as s:
                rows = s.query(LLMUsageLogRow).filter(LLMUsageLogRow.created_at >= since).all()
                used = sum(r.prompt_tokens + r.completion_tokens for r in rows)
                if used >= self.settings.gateway.daily_token_budget:
                    raise CloudGatewayError(
                        f"Daily token budget ({self.settings.gateway.daily_token_budget}) exhausted ({used} used in 24h).",
                        category="budget_exceeded",
                        retryable=False,
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
                        status=status[:24],
                    )
                )
                s.commit()
        except Exception:
            pass

    def usage_summary(self) -> dict:
        if self.db is None:
            return {"total_calls": 0, "tokens_24h": 0, "by_provider": {}, "governor": self.governor.stats()}
        since = time.time() - 86400.0
        with self.db.sync_session() as s:
            rows = s.query(LLMUsageLogRow).filter(LLMUsageLogRow.created_at >= since).all()
        by_p: dict[str, dict] = {}
        total_tokens = 0
        for r in rows:
            tok = r.prompt_tokens + r.completion_tokens
            total_tokens += tok
            b = by_p.setdefault(r.provider, {"calls": 0, "tokens": 0, "avg_ms": 0.0, "errors": 0})
            b["calls"] += 1
            b["tokens"] += tok
            if r.status != "ok":
                b["errors"] += 1
            b["avg_ms"] = round((b["avg_ms"] * (b["calls"] - 1) + r.latency_ms) / b["calls"], 1)
        return {
            "total_calls": len(rows),
            "tokens_24h": total_tokens,
            "daily_budget": self.settings.gateway.daily_token_budget,
            "by_provider": by_p,
            "breakers": {k: self.breaker_status(k) for k in ("ollama", "openrouter", "gemini", "huggingface", "openai", "anthropic")},
            "governor": self.governor.stats(),
        }

    def _build_cloud_adapter(self, prov: str):
        allowed = {h.lower() for h in self.settings.providers.allowed_cloud_hosts}
        if prov in OPENAI_COMPAT_PROVIDERS:
            url_attr, _ = OPENAI_COMPAT_PROVIDERS[prov]
            return OpenAICompatAdapter(prov, getattr(self.settings.providers, url_attr), allowed_hosts=allowed)
        if prov == "gemini":
            return GeminiAdapter(self.settings.providers.gemini_base_url, allowed_hosts=allowed)
        if prov == "anthropic":
            return AnthropicAdapter(self.settings.providers.anthropic_base_url, allowed_hosts=allowed)
        raise CloudGatewayError(f"Unsupported provider {prov!r}", category="unsupported", retryable=False)

    async def stream_with_fallback(
        self,
        req: GatewayRequest,
        *,
        fallback_models: list[str] | None = None,
        transport_factory=None,
    ) -> AsyncIterator[GatewayEvent]:
        """Execute streaming model call across eligible fallback chain with breaker, retry/backoff, & firewall."""
        raw_candidates = [req.model_key] + [m for m in (fallback_models or []) if m != req.model_key]
        candidates, rejections = self.inspect_candidate_eligibility(req, raw_candidates)
        if not candidates:
            cats = [r["reason"] for r in rejections]
            if cats and all(c == "paid_blocked" for c in cats):
                raise CloudGatewayError(
                    f"Blocked paid model call for {req.model_key!r}: paid models require explicit owner authorization (allow_paid_models=False).",
                    category="paid_blocked",
                    retryable=False,
                )
            if cats and all(c == "circuit_open" for c in cats):
                raise CloudGatewayError(
                    f"Circuit breaker OPEN for all candidate providers ({'; '.join(r['detail'] for r in rejections)}).",
                    category="circuit_open",
                    retryable=True,
                )
            detail = "; ".join(f"{r['model']}: {r['detail']}" for r in rejections) or "no candidates provided"
            raise CloudGatewayError(
                f"No eligible model satisfies request requirements ({detail}).",
                category="no_eligible_model",
                retryable=False,
            )

        last_err: Exception | None = None
        fallback_reason = ""
        max_attempts = int(self.settings.gateway.retry_max_attempts)
        base_delay = float(self.settings.gateway.retry_base_delay_s)

        for key in candidates:
            prov, model_id = parse_model_key(key, self.settings)
            if self.breaker_status(prov) == "OPEN":
                fallback_reason = f"breaker_open:{prov}"
                last_err = CloudGatewayError(f"Circuit breaker OPEN for {prov!r}", category="circuit_open", retryable=True)
                continue

            for attempt in range(max_attempts):
                if req.cancel_event and req.cancel_event.is_set():
                    raise CloudGatewayError("Request cancelled by caller", category="cancelled", retryable=False)
                try:
                    self._check_rate_and_budget()
                    if prov == "ollama":
                        async with self.governor.acquire(local=True, timeout_s=float(req.timeout_s or self.settings.gateway.request_timeout_s)):
                            t0 = time.perf_counter()
                            if key != self.brain.current and key in self.settings.brains:
                                await self.brain.switch(key)
                            txt_len = 0
                            async for ev in self.brain.stream_events(req.messages, tools=req.tools):
                                if req.cancel_event and req.cancel_event.is_set():
                                    raise CloudGatewayError("Request cancelled", category="cancelled", retryable=False)
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
                                "retries": attempt,
                                "fallback_reason": fallback_reason,
                                "cost_usd": 0.0,
                            }
                            self._record_breaker_success("ollama")
                            self._record_usage(req, usage, status="ok")
                            yield GatewayEvent(type="done", usage=usage)
                            return

                    # Cloud provider: enforce paid auth + offline + key + centralized PII/privacy firewall BEFORE retry
                    api_key = check_cloud_egress_allowed(prov, self.settings, model_id=model_id)
                    clean_msgs = self.enforce_egress_firewall(req, prov)
                    scrubbed_req = GatewayRequest(**{**req.__dict__, "messages": clean_msgs, "model_key": key})
                    adapter = self._build_cloud_adapter(prov)
                    cap = adapter.capabilities(model_id)

                    async with self.governor.acquire(local=False, timeout_s=float(req.timeout_s or self.settings.gateway.request_timeout_s)):
                        final_usage: dict = {"provider": prov, "model": model_id}
                        async for ev in adapter.complete(
                            scrubbed_req,
                            api_key=api_key,
                            model_id=model_id,
                            max_fetch_bytes=self.settings.internet.max_fetch_bytes,
                            transport_factory=transport_factory,
                        ):
                            if ev.type == "done":
                                u = dict(ev.usage)
                                tot_k = (int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))) / 1000.0
                                u["retries"] = attempt
                                u["fallback_reason"] = fallback_reason
                                u["cost_usd"] = round(tot_k * cap.cost_per_1k_usd, 6)
                                final_usage = u
                                yield GatewayEvent(type="done", usage=final_usage)
                            else:
                                yield ev
                        self._record_breaker_success(prov)
                        self._record_usage(req, final_usage, status="ok")
                        return
                except CloudGatewayError as e:
                    last_err = e
                    self._record_usage(req, {"provider": prov, "model": model_id}, status=f"err:{e.category}")
                    if e.retryable:
                        self._record_breaker_failure(prov, str(e))
                        if attempt + 1 < max_attempts:
                            delay = base_delay * (2 ** attempt) + random.uniform(0.0, base_delay * 0.5)
                            await asyncio.sleep(delay)
                            continue
                    fallback_reason = f"{prov}:{e.category}"
                    break
                except Exception as e:
                    last_err = e
                    self._record_breaker_failure(prov, str(e))
                    self._record_usage(req, {"provider": prov, "model": model_id}, status="error")
                    fallback_reason = f"{prov}:exception"
                    break

        raise CloudGatewayError(f"All model candidates failed: {last_err}") from last_err
