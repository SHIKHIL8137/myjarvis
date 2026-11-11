"""Layer 3 Capability & Policy Router + Unified DecisionEngine facade."""
from ..config import Settings, has_provider_api_key
from .classifier import LLMJsonFn, classify_intent
from .rules import evaluate_layer1
from .schemas import DecisionEnvelope, RoutingPlan


class DecisionEngine:
    """3-Layer Fast Decision Engine (L1 Deterministic -> L2 Classifier -> L3 Capability Route)."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def route_for_decision(self, env: DecisionEnvelope) -> RoutingPlan:
        online_cloud = bool(
            not self.settings.offline_enforced
            and self.settings.internet.enabled
            and self.settings.providers.allow_cloud
        )
        local_default = self.settings.boot_brain
        local_think = "local_thinking" if "local_thinking" in self.settings.brains else local_default

        if env.clarifying_question and env.confidence < self.settings.gateway.classifier_confidence_threshold:
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

        if env.department == "dev":
            chain: list[str] = []
            if online_cloud:
                p = self.settings.providers
                if has_provider_api_key("openrouter"):
                    chain.append(f"openrouter:{p.openrouter_model}")
                if has_provider_api_key("anthropic"):
                    chain.append(f"anthropic:{p.anthropic_model}")
                if has_provider_api_key("openai"):
                    chain.append(f"openai:{p.openai_model}")
                if has_provider_api_key("gemini"):
                    chain.append(f"gemini:{p.gemini_model}")
                if has_provider_api_key("huggingface"):
                    chain.append(f"huggingface:{p.huggingface_model}")
            chain.extend([local_think, local_default])
            return RoutingPlan(
                decision=env,
                workflow="workforce_graph",
                primary_model=chain[0],
                fallback_models=chain[1:],
                agent_role="coder_agent" if env.complexity == "heavy_coding" else "architect_agent",
                allowed_tools=["graphify_query", "graphify_affected", "git_worktree", "opencode_run", "qa_verify"],
            )

        if env.department == "assistant" or env.needs_live_web:
            chain = [local_think if env.complexity == "complex_reasoning" else local_default]
            if online_cloud and has_provider_api_key("gemini"):
                chain.append(f"gemini:{self.settings.providers.gemini_model}")
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

    async def decide(self, text: str, *, fast_llm: LLMJsonFn | None = None) -> RoutingPlan:
        l1 = evaluate_layer1(text)
        if l1 is not None:
            return self.route_for_decision(l1)
        env = await classify_intent(
            text,
            fast_llm=fast_llm if self.settings.gateway.enable_llm_classifier else None,
            confidence_threshold=self.settings.gateway.classifier_confidence_threshold,
        )
        return self.route_for_decision(env)

