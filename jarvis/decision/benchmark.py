"""Held-out evaluation & comparative benchmark runner (Heuristic vs Fast LLM vs Laya)."""
from collections import Counter
import statistics
import time
from typing import Iterable, Sequence

from .classifier import DecisionClassifier
from .dataset import load_decision_dataset


def _macro_f1(y_true: list[str], y_pred: list[str], labels: Iterable[str]) -> float:
    scores: list[float] = []
    for lb in labels:
        tp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == lb and yp == lb)
        fp = sum(1 for yt, yp in zip(y_true, y_pred) if yt != lb and yp == lb)
        fn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == lb and yp != lb)
        denom = (2 * tp) + fp + fn
        scores.append((2 * tp / denom) if denom > 0 else 0.0)
    return round(sum(scores) / max(1, len(scores)), 4)


def _confusion_matrix(y_true: list[str], y_pred: list[str]) -> dict[str, dict[str, int]]:
    mat: dict[str, dict[str, int]] = {}
    for yt, yp in zip(y_true, y_pred):
        mat.setdefault(yt, {})
        mat[yt][yp] = mat[yt].get(yp, 0) + 1
    return mat


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 3)


async def benchmark_classifier(classifier: DecisionClassifier, rows: list[dict]) -> dict:
    """Evaluate a DecisionClassifier strictly on held-out rows without modifying weights."""
    y_dept_t: list[str] = []
    y_dept_p: list[str] = []
    y_int_t: list[str] = []
    y_int_p: list[str] = []
    y_comp_t: list[str] = []
    y_comp_p: list[str] = []
    y_risk_t: list[str] = []
    y_risk_p: list[str] = []

    latencies: list[float] = []
    by_lang: dict[str, dict] = {}
    abstain_expected = 0
    abstain_correct = 0
    abstain_false_positives = 0
    sensitive_total = 0
    sensitive_enforced = 0
    errors = 0

    for r in rows:
        lang = r.get("language") or "en"
        by_lang.setdefault(lang, {"total": 0, "dept_correct": 0, "intent_correct": 0, "latencies": []})
        by_lang[lang]["total"] += 1
        t0 = time.perf_counter()
        try:
            res = await classifier.classify(r["text"])
            env = res.envelope
            lat = res.envelope.latency_ms or ((time.perf_counter() - t0) * 1000.0)
        except Exception:
            errors += 1
            lat = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat)
            continue

        latencies.append(lat)
        by_lang[lang]["latencies"].append(lat)

        y_dept_t.append(r["department"])
        y_dept_p.append(env.department)
        y_int_t.append(r["intent"])
        y_int_p.append(env.intent)
        y_comp_t.append(r["complexity"])
        y_comp_p.append(env.complexity)
        y_risk_t.append(r["risk_tier"])
        y_risk_p.append(env.risk_tier)

        if env.department == r["department"]:
            by_lang[lang]["dept_correct"] += 1
        if env.intent == r["intent"]:
            by_lang[lang]["intent_correct"] += 1

        if r.get("should_abstain"):
            abstain_expected += 1
            if env.abstained or env.intent == "clarification_needed":
                abstain_correct += 1
        elif env.abstained:
            abstain_false_positives += 1

        if r.get("sensitive"):
            sensitive_total += 1
            if env.requires_owner_approval and env.risk_tier == "EXECUTE":
                sensitive_enforced += 1

    n = max(1, len(y_dept_t))
    lats_sorted = sorted(latencies)
    per_lang_out = {}
    for lg, info in by_lang.items():
        tot = max(1, info["total"])
        ll = sorted(info["latencies"])
        per_lang_out[lg] = {
            "samples": info["total"],
            "department_accuracy": round(info["dept_correct"] / tot, 4),
            "intent_accuracy": round(info["intent_correct"] / tot, 4),
            "p50_ms": _percentile(ll, 50),
            "p95_ms": _percentile(ll, 95),
        }

    dept_labels = sorted(set(y_dept_t) | set(y_dept_p))
    int_labels = sorted(set(y_int_t) | set(y_int_p))

    return {
        "classifier": classifier.name,
        "total_rows": len(rows),
        "evaluated": len(y_dept_t),
        "errors": errors,
        "department_accuracy": round(sum(1 for a, b in zip(y_dept_t, y_dept_p) if a == b) / n, 4),
        "department_macro_f1": _macro_f1(y_dept_t, y_dept_p, dept_labels),
        "intent_accuracy": round(sum(1 for a, b in zip(y_int_t, y_int_p) if a == b) / n, 4),
        "intent_macro_f1": _macro_f1(y_int_t, y_int_p, int_labels),
        "complexity_accuracy": round(sum(1 for a, b in zip(y_comp_t, y_comp_p) if a == b) / n, 4),
        "risk_tier_accuracy": round(sum(1 for a, b in zip(y_risk_t, y_risk_p) if a == b) / n, 4),
        "abstention_recall": round(abstain_correct / max(1, abstain_expected), 4),
        "abstention_false_positives": abstain_false_positives,
        "sensitive_security_enforcement_rate": round(sensitive_enforced / max(1, sensitive_total), 4),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "p50": _percentile(lats_sorted, 50),
            "p95": _percentile(lats_sorted, 95),
        },
        "per_language": per_lang_out,
        "department_confusion_matrix": _confusion_matrix(y_dept_t, y_dept_p),
        "intent_error_top": Counter(f"{t}->{p}" for t, p in zip(y_int_t, y_int_p) if t != p).most_common(6),
    }


async def run_comparative_benchmark(classifiers: Sequence[DecisionClassifier], *, split: str = "test") -> dict:
    ds = load_decision_dataset()
    rows = ds.get(split) or []
    results = []
    for clf in classifiers:
        results.append(await benchmark_classifier(clf, rows))
    return {
        "dataset_version": ds["version"],
        "split": split,
        "leakage_free": ds["leakage_free"],
        "split_counts": ds["counts"],
        "results": results,
    }
