"""3-Layer Fast Decision Engine (L1 Deterministic -> L2 Heuristic/LLM/Isolated Laya -> L3 Capability Router).

Kept lazy via `__getattr__` so isolated worker environments (e.g., Laya venv without `httpx` or `sqlalchemy`)
can import `jarvis.decision.laya_service` directly cleanly.
"""
import importlib

_LAZY = {
    "benchmark_classifier": (".benchmark", "benchmark_classifier"),
    "run_comparative_benchmark": (".benchmark", "run_comparative_benchmark"),
    "DecisionCalibrationService": (".calibration", "DecisionCalibrationService"),
    "ClassifierResult": (".classifier", "ClassifierResult"),
    "DecisionClassifier": (".classifier", "DecisionClassifier"),
    "ExistingHeuristicClassifier": (".classifier", "ExistingHeuristicClassifier"),
    "ExistingLLMClassifier": (".classifier", "ExistingLLMClassifier"),
    "LLMJsonFn": (".classifier", "LLMJsonFn"),
    "classify_intent": (".classifier", "classify_intent"),
    "heuristic_classify": (".classifier", "heuristic_classify"),
    "load_decision_dataset": (".dataset", "load_decision_dataset"),
    "verify_no_split_leakage": (".dataset", "verify_no_split_leakage"),
    "IsolatedLayaClassifier": (".laya_adapter", "IsolatedLayaClassifier"),
    "DecisionEngine": (".router", "DecisionEngine"),
    "detect_sensitive_operations": (".rules", "detect_sensitive_operations"),
    "detect_text_language": (".rules", "detect_text_language"),
    "enforce_security_boundary": (".rules", "enforce_security_boundary"),
    "evaluate_layer1": (".rules", "evaluate_layer1"),
    "ALLOWED_CLASSIFIER_SOURCES": (".schemas", "ALLOWED_CLASSIFIER_SOURCES"),
    "ALLOWED_COMPLEXITIES": (".schemas", "ALLOWED_COMPLEXITIES"),
    "ALLOWED_DEPARTMENTS": (".schemas", "ALLOWED_DEPARTMENTS"),
    "ALLOWED_INTENTS": (".schemas", "ALLOWED_INTENTS"),
    "ALLOWED_RISK_TIERS": (".schemas", "ALLOWED_RISK_TIERS"),
    "ALLOWED_WORKFLOWS": (".schemas", "ALLOWED_WORKFLOWS"),
    "SENSITIVE_OPERATION_CATEGORIES": (".schemas", "SENSITIVE_OPERATION_CATEGORIES"),
    "ClassifierSchemaError": (".schemas", "ClassifierSchemaError"),
    "DecisionEnvelope": (".schemas", "DecisionEnvelope"),
    "JARVIS_LAYA_QUESTIONS": (".schemas", "JARVIS_LAYA_QUESTIONS"),
    "RoutingPlan": (".schemas", "RoutingPlan"),
    "validate_classifier_payload": (".schemas", "validate_classifier_payload"),
}


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = _LAZY[name]
    mod = importlib.import_module(mod_name, __name__)
    val = getattr(mod, attr_name)
    globals()[name] = val
    return val


__all__ = sorted(_LAZY.keys())
