"""Layer 2 Fast Intent Classifier: deterministic lexical scoring + optional fast local LLM JSON classification."""
import json
import time
from typing import Awaitable, Callable

from .rules import DEV_KEYWORDS, DOC_EXPORT_RE, REMINDER_RE, WEB_RESEARCH_RE
from .schemas import DecisionEnvelope

LLMJsonFn = Callable[[list[dict]], Awaitable[str]]


def heuristic_classify(text: str) -> DecisionEnvelope:
    """Zero-latency fallback classifier yielding structured DecisionEnvelope without an LLM call."""
    t0 = time.perf_counter()
    s = (text or "").strip()
    is_dev = bool(DEV_KEYWORDS.search(s))
    doc_m = DOC_EXPORT_RE.search(s)
    is_rem = bool(REMINDER_RE.search(s))
    is_web = bool(WEB_RESEARCH_RE.search(s))

    if is_dev:
        needs_write = any(w in s.lower() for w in ("implement", "fix", "add ", "edit", "refactor", "create", "migrate", "write"))
        return DecisionEnvelope(
            layer=2,
            department="dev",
            intent="dev_coding" if needs_write else "dev_architecture",
            complexity="heavy_coding" if needs_write else "complex_reasoning",
            needs_live_web=is_web,
            needs_project_context=True,
            risk_tier="WRITE" if needs_write else "READ",
            confidence=0.84,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    if doc_m:
        fmt = doc_m.group(1).lower()
        return DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="document_export",
            complexity="standard",
            risk_tier="WRITE",
            confidence=0.90,
            command_payload={"format": fmt},
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    if is_rem:
        return DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="reminder_manage",
            complexity="trivial",
            risk_tier="WRITE",
            confidence=0.88,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    if is_web:
        return DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="web_research",
            complexity="standard",
            needs_live_web=True,
            risk_tier="READ",
            confidence=0.86,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    return DecisionEnvelope(
        layer=2,
        department="chat",
        intent="general_chat",
        complexity="trivial" if len(s.split()) < 10 else "standard",
        risk_tier="READ",
        confidence=0.82,
        latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
    )


async def classify_intent(text: str, *, fast_llm: LLMJsonFn | None = None, confidence_threshold: float = 0.75) -> DecisionEnvelope:
    """Run heuristic classification refined optionally by a constrained JSON fast-model classification."""
    base = heuristic_classify(text)
    if fast_llm is None:
        return base

    t0 = time.perf_counter()
    prompt = [
        {
            "role": "system",
            "content": (
                "Classify the user request strictly into JSON with keys: "
                "department (assistant|dev|system|chat), intent (short_snake_case), "
                "complexity (trivial|standard|complex_reasoning|heavy_coding), "
                "needs_live_web (bool), needs_project_context (bool), risk_tier (READ|WRITE|EXECUTE), "
                "confidence (0..1), clarifying_question (string or null)."
            ),
        },
        {"role": "user", "content": text[:1000]},
    ]
    try:
        raw = await fast_llm(prompt)
        data = json.loads(raw.strip())
        if isinstance(data, dict):
            conf = float(data.get("confidence", base.confidence))
            if conf < confidence_threshold and data.get("clarifying_question"):
                base.clarifying_question = str(data["clarifying_question"])
            base.department = str(data.get("department") or base.department)
            base.intent = str(data.get("intent") or base.intent)
            base.complexity = str(data.get("complexity") or base.complexity)
            base.needs_live_web = bool(data.get("needs_live_web", base.needs_live_web))
            base.needs_project_context = bool(data.get("needs_project_context", base.needs_project_context))
            base.risk_tier = str(data.get("risk_tier") or base.risk_tier)
            base.confidence = conf
            base.latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            base.notes.append("llm_classifier")
    except Exception:
        base.notes.append("heuristic_fallback")
    return base

