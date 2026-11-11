"""Layer 2 DecisionClassifier abstraction: heuristic, constrained fast LLM, & isolated Laya adapter."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import time
from typing import Awaitable, Callable

from .rules import (
    DEV_KEYWORDS,
    DOC_EXPORT_RE,
    REMINDER_RE,
    WEB_RESEARCH_RE,
    detect_text_language,
    enforce_security_boundary,
)
from .schemas import (
    ClassifierSchemaError,
    DecisionEnvelope,
    validate_classifier_payload,
)

LLMJsonFn = Callable[[list[dict]], Awaitable[str]]


@dataclass
class ClassifierResult:
    envelope: DecisionEnvelope
    raw: dict = field(default_factory=dict)
    fallback_used: bool = False
    error: str | None = None


class DecisionClassifier(ABC):
    """Provider-independent Layer 2 decision classifier contract."""

    name: str = "base"

    @abstractmethod
    async def classify(self, text: str, *, context: dict | None = None) -> ClassifierResult:
        """Classify text into a validated DecisionEnvelope."""

    def health(self) -> dict:
        return {"name": self.name, "ready": True}


def heuristic_classify(text: str) -> DecisionEnvelope:
    """Zero-latency deterministic classifier yielding structured DecisionEnvelope without an LLM call."""
    t0 = time.perf_counter()
    s = (text or "").strip()
    lowered = s.lower()
    lang = detect_text_language(s)
    is_dev = bool(DEV_KEYWORDS.search(s))
    doc_m = DOC_EXPORT_RE.search(s)
    is_rem = bool(REMINDER_RE.search(s))
    is_web = bool(WEB_RESEARCH_RE.search(s))

    # Ambiguity / underspecified cue detection
    is_vague = (
        len(s.split()) <= 6
        and any(w in lowered for w in ("fix that", "do that thing", "handle it", "yesterday", "what we discussed", "അത് ചെയ്യൂ", "അതു ശരിയാക്കൂ"))
    )

    if is_dev:
        needs_write = any(w in lowered for w in ("implement", "fix", "add ", "edit", "refactor", "create", "migrate", "write"))
        env = DecisionEnvelope(
            layer=2,
            department="dev",
            intent="dev_coding" if needs_write else "dev_architecture",
            complexity="heavy_coding" if needs_write else "complex_reasoning",
            needs_live_web=is_web,
            needs_project_context=True,
            risk_tier="WRITE" if needs_write else "READ",
            confidence=0.84,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="heuristic",
        )
        return enforce_security_boundary(s, env)

    if doc_m:
        fmt = doc_m.group(1).lower()
        env = DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="document_export",
            complexity="standard",
            risk_tier="WRITE",
            confidence=0.90,
            command_payload={"format": fmt},
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="heuristic",
        )
        return enforce_security_boundary(s, env)

    if is_rem or ("ഓർമ്മിപ്പിക്കുക" in s):
        env = DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="reminder_manage",
            complexity="trivial",
            risk_tier="WRITE",
            confidence=0.88,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="heuristic",
        )
        return enforce_security_boundary(s, env)

    if is_web or any(k in s for k in ("തിരയുക", "വാർത്ത", "കാലാവസ്ഥ")):
        env = DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="web_research",
            complexity="standard",
            needs_live_web=True,
            risk_tier="READ",
            confidence=0.86,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="heuristic",
        )
        return enforce_security_boundary(s, env)

    if is_vague:
        env = DecisionEnvelope(
            layer=2,
            department="assistant",
            intent="clarification_needed",
            complexity="standard",
            risk_tier="READ",
            confidence=0.45,
            clarifying_question="Could you clarify which file, task, or topic you mean?",
            abstained=True,
            laya_route="review",
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            language=lang,
            classifier_source="heuristic",
        )
        return enforce_security_boundary(s, env)

    env = DecisionEnvelope(
        layer=2,
        department="chat",
        intent="general_chat",
        complexity="trivial" if len(s.split()) < 10 else "standard",
        risk_tier="READ",
        confidence=0.82,
        latency_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        language=lang,
        classifier_source="heuristic",
    )
    return enforce_security_boundary(s, env)


class ExistingHeuristicClassifier(DecisionClassifier):
    """Layer 2 zero-dependency deterministic classifier."""

    name = "heuristic"

    async def classify(self, text: str, *, context: dict | None = None) -> ClassifierResult:
        env = heuristic_classify(text)
        return ClassifierResult(envelope=env, raw=env.to_dict(), fallback_used=False)


class ExistingLLMClassifier(DecisionClassifier):
    """Layer 2 constrained-JSON fast-LLM classifier with strict enum validation & heuristic fallback."""

    name = "llm"

    def __init__(self, fast_llm: LLMJsonFn | None, *, confidence_threshold: float = 0.75):
        self.fast_llm = fast_llm
        self.confidence_threshold = confidence_threshold

    async def classify(self, text: str, *, context: dict | None = None) -> ClassifierResult:
        base = heuristic_classify(text)
        if self.fast_llm is None:
            return ClassifierResult(envelope=base, raw=base.to_dict(), fallback_used=False)

        t0 = time.perf_counter()
        prompt = [
            {
                "role": "system",
                "content": (
                    "Classify the user request strictly into JSON with keys: "
                    "department (assistant|dev|system|chat), "
                    "intent (general_chat|dev_coding|dev_architecture|dev_review|dev_debug|document_export|"
                    "reminder_manage|web_research|memory_manage|system_status|clarification_needed|"
                    "sensitive_action_request|security_audit), "
                    "complexity (trivial|standard|complex_reasoning|heavy_coding), "
                    "needs_live_web (bool), needs_project_context (bool), risk_tier (READ|WRITE|EXECUTE), "
                    "confidence (0..1), clarifying_question (string or null)."
                ),
            },
            {"role": "user", "content": (text or "")[:1000]},
        ]
        try:
            raw = await self.fast_llm(prompt)
            data = json.loads(raw.strip())
            clean = validate_classifier_payload(data)
            if clean["confidence"] < self.confidence_threshold and clean.get("clarifying_question"):
                base.clarifying_question = clean["clarifying_question"]
            base.department = clean["department"]
            base.intent = clean["intent"]
            base.complexity = clean["complexity"]
            base.needs_live_web = clean["needs_live_web"]
            base.needs_project_context = clean["needs_project_context"]
            base.risk_tier = clean["risk_tier"]
            base.confidence = clean["confidence"]
            base.abstained = bool(clean["abstain"] or clean["intent"] == "clarification_needed")
            base.classifier_source = "llm"
            base.latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            base.notes.append("llm_classifier")
            base = enforce_security_boundary(text, base)
            return ClassifierResult(envelope=base, raw=clean, fallback_used=False)
        except (ClassifierSchemaError, Exception) as exc:
            base.notes.append("heuristic_fallback")
            return ClassifierResult(envelope=base, raw=base.to_dict(), fallback_used=True, error=str(exc))


async def classify_intent(text: str, *, fast_llm: LLMJsonFn | None = None, confidence_threshold: float = 0.75) -> DecisionEnvelope:
    """Backward-compatible helper delegating to ExistingHeuristicClassifier / ExistingLLMClassifier."""
    if fast_llm is None:
        return (await ExistingHeuristicClassifier().classify(text)).envelope
    return (await ExistingLLMClassifier(fast_llm, confidence_threshold=confidence_threshold).classify(text)).envelope
