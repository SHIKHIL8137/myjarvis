"""Isolated Laya adapter (HTTP localhost client + subprocess worker) with strict schema validation,
bounded concurrency, timeouts, caching, and circuit breaking."""
import asyncio
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import time

import httpx

from ..config import LayaSpec
from .classifier import ClassifierResult, DecisionClassifier
from .rules import detect_text_language, enforce_security_boundary
from .schemas import (
    JARVIS_LAYA_QUESTIONS,
    ClassifierSchemaError,
    DecisionEnvelope,
    validate_classifier_payload,
)


class IsolatedLayaClassifier(DecisionClassifier):
    """Fail-closed adapter talking ONLY to an isolated localhost Laya process/HTTP server."""

    name = "laya_isolated"

    def __init__(
        self,
        spec: LayaSpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        root_dir: Path | None = None,
    ):
        self.spec = spec
        self._transport = transport
        self._root_dir = Path(root_dir) if root_dir else Path(__file__).resolve().parents[2]
        self._sem = asyncio.Semaphore(max(1, int(spec.max_concurrency)))
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._cache_max = 256
        self._cache_ttl_s = 120.0

        # Circuit breaker & telemetry (never records secrets or raw sensitive prompt bodies)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._metrics: dict = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "timeouts": 0,
            "schema_rejections": 0,
            "circuit_open_rejects": 0,
            "cache_hits": 0,
            "abstained_count": 0,
            "oob_count": 0,
            "last_latency_ms": 0.0,
            "last_error": "",
            "last_model_version": "",
        }

    def update_spec(self, spec: LayaSpec) -> None:
        if spec.max_concurrency != self.spec.max_concurrency:
            self._sem = asyncio.Semaphore(max(1, int(spec.max_concurrency)))
        self.spec = spec

    def metrics(self) -> dict:
        now = time.monotonic()
        return {
            **self._metrics,
            "circuit_open": now < self._circuit_open_until,
            "circuit_retry_after_s": round(max(0.0, self._circuit_open_until - now), 2),
            "consecutive_failures": self._consecutive_failures,
            "cache_entries": len(self._cache),
        }

    def health(self) -> dict:
        return {
            "name": self.name,
            "enabled": bool(self.spec.enabled),
            "mode": self.spec.mode,
            "service_mode": self.spec.service_mode,
            "base_url": self.spec.base_url,
            "checkpoint": self.spec.checkpoint,
            "device": self.spec.device,
            "metrics": self.metrics(),
        }

    async def probe_service_health(self) -> dict:
        if self.spec.service_mode != "http":
            py_path = Path(self.spec.python_bin)
            if not py_path.is_absolute():
                py_path = (self._root_dir / py_path).resolve()
            return {"reachable": py_path.exists(), "service_mode": "subprocess", "python_bin": str(py_path)}
        try:
            async with httpx.AsyncClient(base_url=self.spec.base_url, transport=self._transport, timeout=1.0) as client:
                r = await client.get("/health")
                if r.status_code == 200:
                    return {"reachable": True, "status": r.json()}
                return {"reachable": False, "http_status": r.status_code}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    def _cache_key(self, text: str) -> str:
        raw = f"{self.spec.checkpoint}:{self.spec.abstain_enabled}:{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _record_failure(self, kind: str, detail: str) -> None:
        self._metrics["failures"] += 1
        if kind == "timeout":
            self._metrics["timeouts"] += 1
        elif kind == "schema":
            self._metrics["schema_rejections"] += 1
        self._metrics["last_error"] = detail[:200]
        self._consecutive_failures += 1
        if self._consecutive_failures >= int(self.spec.circuit_breaker_failures):
            self._circuit_open_until = time.monotonic() + float(self.spec.circuit_breaker_cooldown_s)

    def _record_success(self, latency_ms: float, model_version: str = "") -> None:
        self._metrics["successes"] += 1
        self._metrics["last_latency_ms"] = round(latency_ms, 3)
        if model_version:
            self._metrics["last_model_version"] = model_version
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def _invoke_http(self, text: str) -> dict:
        timeout_s = max(0.05, float(self.spec.timeout_ms) / 1000.0)
        payload = {
            "text": text[:8000],
            "checkpoint": self.spec.checkpoint,
            "device": self.spec.device,
            "abstain_enabled": self.spec.abstain_enabled,
            "confidence_threshold": self.spec.confidence_threshold,
            "questions": JARVIS_LAYA_QUESTIONS,
        }
        async with httpx.AsyncClient(base_url=self.spec.base_url, transport=self._transport, timeout=timeout_s) as client:
            resp = await client.post("/internal/decision/classify", json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Laya service returned HTTP {resp.status_code}")
            return resp.json()

    async def _invoke_subprocess(self, text: str) -> dict:
        py_bin = Path(self.spec.python_bin)
        if not py_bin.is_absolute():
            py_bin = (self._root_dir / py_bin).resolve()
        if not py_bin.exists():
            raise FileNotFoundError(f"Isolated Laya python binary not found: {py_bin}")
        payload = json.dumps(
            {
                "text": text[:8000],
                "checkpoint": self.spec.checkpoint,
                "device": self.spec.device,
                "abstain_enabled": self.spec.abstain_enabled,
                "confidence_threshold": self.spec.confidence_threshold,
            },
            ensure_ascii=False,
        )
        proc = await asyncio.create_subprocess_exec(
            str(py_bin),
            "-m",
            "jarvis.decision.laya_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._root_dir),
        )
        timeout_s = max(0.1, float(self.spec.timeout_ms) / 1000.0)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(payload.encode("utf-8")), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise
        if proc.returncode != 0:
            err = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Laya worker failed (rc={proc.returncode}): {err[:200]}")
        return json.loads((stdout_b or b"").decode("utf-8", errors="replace").strip())

    def reset_circuit(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def classify(self, text: str, *, context: dict | None = None) -> ClassifierResult:
        t0 = time.perf_counter()
        self._metrics["requests"] += 1
        lang = detect_text_language(text)
        now_m = time.monotonic()

        if now_m < self._circuit_open_until:
            self._metrics["circuit_open_rejects"] += 1
            env = DecisionEnvelope(
                layer=2,
                department="assistant",
                intent="clarification_needed",
                complexity="standard",
                risk_tier="READ",
                confidence=0.0,
                abstained=True,
                laya_route="review",
                clarifying_question="Decision classifier circuit is temporarily open; please clarify or retry.",
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                language=lang,
                classifier_source="laya_isolated",
                notes=["laya_circuit_open"],
            )
            return ClassifierResult(envelope=enforce_security_boundary(text, env), error="laya_circuit_open")

        ck = self._cache_key(text)
        if self.spec.cache_enabled and ck in self._cache:
            ts_cached, cached_raw = self._cache[ck]
            if (time.time() - ts_cached) <= self._cache_ttl_s:
                self._cache.move_to_end(ck)
                self._metrics["cache_hits"] += 1
                env = self._build_envelope(text, cached_raw, latency_ms=(time.perf_counter() - t0) * 1000.0, lang=lang, cached=True)
                return ClassifierResult(envelope=env, raw=cached_raw, fallback_used=False)
            self._cache.pop(ck, None)

        async with self._sem:
            timeout_s = max(0.05, float(self.spec.timeout_ms) / 1000.0)
            try:
                coro = self._invoke_subprocess(text) if self.spec.service_mode == "subprocess" else self._invoke_http(text)
                raw = await asyncio.wait_for(coro, timeout=timeout_s)
                clean = validate_classifier_payload(raw)
                if clean.get("abstain") or (self.spec.abstain_enabled and clean["confidence"] < float(self.spec.confidence_threshold)):
                    clean["abstain"] = True
                    clean["route"] = "review"
                    self._metrics["abstained_count"] += 1
                if clean.get("unbound"):
                    self._metrics["oob_count"] += 1
                if self.spec.cache_enabled:
                    self._cache[ck] = (time.time(), {**clean, "model_version": raw.get("model_version", "")})
                    while len(self._cache) > self._cache_max:
                        self._cache.popitem(last=False)
                lat = (time.perf_counter() - t0) * 1000.0
                self._record_success(lat, str(raw.get("model_version") or ""))
                env = self._build_envelope(text, clean, latency_ms=lat, lang=lang, cached=False)
                return ClassifierResult(envelope=env, raw=raw, fallback_used=False)
            except asyncio.TimeoutError:
                self._record_failure("timeout", f"timeout after {self.spec.timeout_ms}ms")
                raise
            except ClassifierSchemaError as exc:
                self._record_failure("schema", str(exc))
                raise
            except Exception as exc:
                self._record_failure("error", str(exc))
                raise

    def _build_envelope(self, text: str, clean: dict, *, latency_ms: float, lang: str, cached: bool) -> DecisionEnvelope:
        abstained = bool(clean.get("abstain"))
        clarify = clean.get("clarifying_question")
        if abstained and not clarify:
            clarify = "Could you clarify the goal or target before I proceed?"
        notes = ["laya_classifier", f"checkpoint:{self.spec.checkpoint}"]
        if cached:
            notes.append("laya_cache_hit")
        if abstained:
            notes.append("laya_abstained")
        if clean.get("unbound"):
            notes.append("laya_out_of_distribution")
        env = DecisionEnvelope(
            layer=2,
            department=clean["department"],
            intent="clarification_needed" if abstained else clean["intent"],
            complexity=clean["complexity"],
            needs_live_web=bool(clean["needs_live_web"]),
            needs_project_context=bool(clean["needs_project_context"]),
            risk_tier=clean["risk_tier"],
            confidence=float(clean["confidence"]),
            clarifying_question=clarify,
            latency_ms=round(latency_ms, 3),
            notes=notes,
            language=lang,
            classifier_source="laya_isolated",
            abstained=abstained,
            out_of_distribution=bool(clean.get("unbound")),
            laya_route=str(clean.get("route") or ("review" if abstained else "auto")),
        )
        return enforce_security_boundary(text, env)
