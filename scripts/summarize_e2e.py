"""Summarise an eval_e2e.py run from its .partial.jsonl (or final .json) with strict and lenient scoring.

strict   : the group must be one of the acceptable groups.
lenient  : additionally, a file request answered with a folder/permission/clarification prompt counts as correct (that IS the right reply when
           the folder is unresolved or a permission is needed), and a chat-level clarifying question counts for ambiguous requests.
  python scripts/summarize_e2e.py benchmarks/understanding_e2e_results.json.partial.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_GROUPS = {"files", "web", "system", "code"}


def load(path: str) -> list[dict]:
    p = Path(path)
    txt = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        return [json.loads(x) for x in txt.splitlines() if x.strip()]
    return json.loads(txt)["rows"]


def pct(a: int, n: int):
    return round(100 * a / n, 1) if n else None


def summarise(rows: list[dict]) -> dict:
    live = [r for r in rows if "expect_group" in r]

    def lenient_ok(r):
        if r["intent_ok"]:
            return True
        if "files" in r["expect_group"] and r["group"] in ("clarify", "files"):
            return True
        return bool(r["clarify"] and r["clarified"])

    notool = [r for r in live if not r["tool"] and not (TOOL_GROUPS & set(r["expect_group"]))]
    nomem = [r for r in live if not r["memory"]]
    mem = [r for r in live if r["memory"]]
    clar = [r for r in live if r["clarify"]]
    by_cat: dict[str, list] = {}
    for r in live:
        by_cat.setdefault(r["cat"], []).append(r["intent_ok"])
    e2e = [r for r in live if lenient_ok(r) and not r["fabricated_claim"] and not r["secret_leak"] and r["answer_len"] > 0 and not (r["tool_routed"] and not r["tool"] and not (TOOL_GROUPS & set(r["expect_group"])))]
    return {
        "n": len(live),
        "intent_accuracy_strict_pct": pct(sum(r["intent_ok"] for r in live), len(live)),
        "intent_accuracy_lenient_pct": pct(sum(lenient_ok(r) for r in live), len(live)),
        "wrong_agent_rate_strict_pct": pct(sum(not r["intent_ok"] for r in live), len(live)),
        "wrong_agent_rate_lenient_pct": pct(sum(not lenient_ok(r) for r in live), len(live)),
        "unnecessary_tool_routing_pct": pct(sum(r["tool_routed"] for r in notool), len(notool)),
        "unnecessary_memory_retrieval_pct": pct(sum(r["memory_injected"] for r in nomem), len(nomem)),
        "needed_memory_retrieved_pct": pct(sum(r["memory_injected"] for r in mem), len(mem)),
        "clarification_recall_pct": pct(sum(r["clarified"] for r in clar), len(clar)),
        "false_clarification_pct": pct(sum(r["clarified"] for r in live if not r["clarify"]), len([r for r in live if not r["clarify"]])),
        "end_to_end_success_proxy_pct": pct(len(e2e), len(live)),
        "fabricated_claims": sum(r["fabricated_claim"] for r in live),
        "secret_leaks": sum(r["secret_leak"] for r in live),
        "by_category_strict_pct": {k: pct(sum(v), len(v)) for k, v in by_cat.items()},
        "mean_seconds": round(sum(r["seconds"] for r in live) / max(1, len(live)), 1),
    }


if __name__ == "__main__":
    print(json.dumps(summarise(load(sys.argv[1])), indent=1))
