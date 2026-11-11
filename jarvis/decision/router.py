"""Layer 3 Capability & Policy Router with pluggable DecisionClassifier modes & Phase 5 security gates."""
import time
from pathlib import Path
from typing import Any

from ..config import DATA, ROOT, Settings, has_provider_api_key
from .classifier import (
    ClassifierResult,
    DecisionClassifier,
    ExistingHeuristicClassifier,
    ExistingLLMClassifier,
    LLMJsonFn,
)
from .laya_adapter import IsolatedLayaClassifier
from .rules import enforce_security_boundary, evaluate_layer1
from .schemas import DecisionEnvelope, RoutingPlan


class DecisionEngine:
    """3-Layer Fast Decision Engine (L1 Deterministic -> L2 Heuristic/LLM/Laya -> L3 Policy Route)."""

    def __init__(self, settings: Settings, *, laya_classifier: DecisionClassifier | None = None, data_dir: Path | None = None):
        self.settings = settings
        self.data_dir = Path(data_dir) if data_dir else DATA
        self.heuristic_classifier = ExistingHeuristicClassifier()
        self.laya_classifier: DecisionClassifier = laya_classifier or IsolatedLayaClassifier(settings.laya, root_dir=ROOT)
        self._shadow_stats: dict[str, Any] = {
            "total_shadow_runs": 0,
            "agreements": 0,
            "disagreements": 0,
            "laya_errors": 0,
            "laya_abstained": 0,
            "recent": [],
        }

    def refresh_settings(self, new_settings: Settings) -> None:
        self.settings = new_settings
        if isinstance(self.laya_classifier, IsolatedLayaClassifier):
            self.laya_classifier.update_spec(new_settings.laya)

    def _effective_mode(self) -> str:
        laya_cfg = getattr(self.settings, "laya", None)
        if laya_cfg is None or not laya_cfg.enabled:
            return "disabled"
        return str(laya_cfg.mode or "shadow")

    def shadow_summary(self) -> dict:
        return {
            **self._shadow_stats,
            "mode": self._effective_mode(),
            "laya": self.laya_classifier.health(),
        }

    def _cloud_chain(self) -> list[str]:
        p = self.settings.providers
        if not (not self.settings.offline_enforced and self.settings.internet.enabled and p.allow_cloud):
            return []
        paid_set = {x.lower() for x in p.paid_providers}
        chain: list[str] = []

        if has_provider_api_key("openrouter") and (p.openrouter_model.endswith(":free") or p.allow_paid_models):
            chain.append(f"openrouter:{p.openrouter_model}")
        if has_provider_api_key("gemini") and ("gemini" not in paid_set or p.allow_paid_models):
            chain.append(f"gemini:{p.gemini_model}")
        if has_provider_api_key("huggingface") and ("huggingface" not in paid_set or p.allow_paid_models):
            chain.append(f"huggingface:{p.huggingface_model}")

        if p.allow_paid_models:
            if has_provider_api_key("anthropic"):
                chain.append(f"anthropic:{p.anthropic_model}")
            if has_provider_api_key("openai"):
                chain.append(f"openai:{p.openai_model}")
        return chain

    def _record_shadow(self, primary: DecisionEnvelope, laya_res: ClassifierResult | None, err: str | None = None) -> dict:
        self._shadow_stats["total_shadow_runs"] += 1
        if err is not None or laya_res is None:
            self._shadow_stats["laya_errors"] += 1
            rec = {
                "ts": round(time.time(), 3),
                "ok": False,
                "error": str(err or "unknown")[:160],
                "primary_dept": primary.department,
                "primary_intent": primary.intent,
            }
        else:
            le = laya_res.envelope
            agreed = le.department == primary.department and le.intent == primary.intent
            if agreed:
                self._shadow_stats["agreements"] += 1
            else:
                self._shadow_stats["disagreements"] += 1
            if le.abstained:
                self._shadow_stats["laya_abstained"] += 1
            rec = {
                "ts": round(time.time(), 3),
                "ok": True,
                "agreed": agreed,
                "primary_dept": primary.department,
                "primary_intent": primary.intent,
                "laya_dept": le.department,
                "laya_intent": le.intent,
                "laya_confidence": le.confidence,
                "laya_latency_ms": le.latency_ms,
                "laya_abstained": le.abstained,
                "laya_route": le.laya_route,
            }
        recent = list(self._shadow_stats["recent"])
        recent.insert(0, rec)
        self._shadow_stats["recent"] = recent[:25]
        return rec

    def route_for_decision(self, env: DecisionEnvelope) -> RoutingPlan:
        local_default = self.settings.boot_brain
        local_think = "local_thinking" if "local_thinking" in self.settings.brains else local_default

        # Hard Phase 5 gate: sensitive operations NEVER execute via fast_command / direct_chat!
        if env.requires_owner_approval:
            chain = self._cloud_chain()
            workflow = "workforce_graph" if env.department == "dev" else "assistant_graph"
            return RoutingPlan(
                decision=env,
                workflow=workflow,
                primary_model=local_default if workflow == "assistant_graph" else ([*chain, local_think, local_default][0]),
                fallback_models=[local_default],
                agent_role="system" if env.department == "system" else ("coder_agent" if env.department == "dev" else "assistant_coordinator"),
                allowed_tools=["approval_gate"],
            )

        if env.abstained or (env.clarifying_question and env.confidence < self.settings.gateway.classifier_confidence_threshold):
            return RoutingPlan(
                decision=env,
                workflow="clarify",
                primary_model=local_default,
                agent_role="assistant_coordinator",
            )

        if env.layer == 1 and env.command_payload:
            return RoutingPlan(
                decision=env,
                workflow="fast_command",
                primary_model=local_default,
                agent_role="system",
            )

        cloud = self._cloud_chain()
        if env.department == "dev":
            chain = cloud + [local_think, local_default]
            return RoutingPlan(
                decision=env,
                workflow="workforce_graph",
                primary_model=chain[0],
                fallback_models=chain[1:],
                agent_role="coder_agent" if env.complexity == "heavy_coding" else "architect_agent",
                allowed_tools=["graphify_query", "graphify_affected", "git_worktree", "opencode_run", "qa_verify"],
            )

        if env.department == "assistant" or env.needs_live_web:
            chain = [local_think if env.complexity == "complex_reasoning" else local_default] + cloud
            return RoutingPlan(
                decision=env,
                workflow="assistant_graph",
                primary_model=chain[0],
                fallback_models=chain[1:] + ([local_default] if chain[0] != local_default else []),
                agent_role="document_producer" if env.intent == "document_export" else ("research_agent" if env.needs_live_web else "assistant_coordinator"),
                allowed_tools=["memory_search", "doc_export", "reminders", "web_search"],
            )

        return RoutingPlan(
            decision=env,
            workflow="direct_chat",
            primary_model=local_default,
            fallback_models=[],
            agent_role="assistant_coordinator",
        )

    async def _run_primary_baseline(self, text: str, *, fast_llm: LLMJsonFn | None = None) -> DecisionEnvelope:
        use_llm = fast_llm if self.settings.gateway.enable_llm_classifier else None
        if use_llm is not None:
            res = await ExistingLLMClassifier(
                use_llm, confidence_threshold=self.settings.gateway.classifier_confidence_threshold
            ).classify(text)
            return enforce_security_boundary(text, res.envelope)
        res = await self.heuristic_classifier.classify(text)
        return enforce_security_boundary(text, res.envelope)

    async def decide(self, text: str, *, fast_llm: LLMJsonFn | None = None) -> RoutingPlan:
        l1 = evaluate_layer1(text)
        if l1 is not None:
            return self.route_for_decision(enforce_security_boundary(text, l1))

        mode = self._effective_mode()
        if mode == "disabled":
            env = await self._run_primary_baseline(text, fast_llm=fast_llm)
            return self.route_for_decision(env)

        if mode == "shadow":
            primary = await self._run_primary_baseline(text, fast_llm=fast_llm)
            try:
                laya_res = await self.laya_classifier.classify(text)
                primary.shadow_comparison = self._record_shadow(primary, laya_res)
            except Exception as exc:
                primary.shadow_comparison = self._record_shadow(primary, None, err=str(exc))
            return self.route_for_decision(primary)

        if mode == "laya_primary":
            try:
                laya_res = await self.laya_classifier.classify(text)
                env = enforce_security_boundary(text, laya_res.envelope)
                return self.route_for_decision(env)
            except Exception as exc:
                env = DecisionEnvelope(
                    layer=2,
                    department="assistant",
                    intent="clarification_needed",
                    complexity="standard",
                    risk_tier="READ",
                    confidence=0.0,
                    abstained=True,
                    laya_route="review",
                    clarifying_question="The experimental Laya classifier could not decide right now; please rephrase or clarify your request.",
                    notes=["laya_primary_unavailable", f"error:{str(exc)[:100]}"],
                    classifier_source="laya_isolated",
                )
                return self.route_for_decision(enforce_security_boundary(text, env))

        # laya_with_heuristic_fallback
        try:
            laya_res = await self.laya_classifier.classify(text)
            env = enforce_security_boundary(text, laya_res.envelope)
            return self.route_for_decision(env)
        except Exception as exc:
            env = await self._run_primary_baseline(text, fast_llm=fast_llm)
            env.classifier_source = "laya_shadow_fallback"
            env.notes.append(f"laya_fallback:{str(exc)[:100]}")
            return self.route_for_decision(env)
