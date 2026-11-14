"""Understanding evaluation on benchmarks/understanding_heldout.json (HELD-OUT, never tuned on). No LLM, no network, no paid calls.

Measures with the REAL components: orchestrator.decision.decide (intent + agent role), select_tools / rank_tools over the real built-in tool
schemas, Assistant._gather_context against a *copy* of the real memory database (noise included) + a few known curated facts.
  python scripts/eval_understanding.py [--out benchmarks/understanding_results.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GROUPS = {  # fixed before measuring
    "general_chat": "chat", "empty": "chat", "filesystem_ops": "files", "project_exploration": "files", "document_export": "files",
    "dev_architecture": "code", "dev_review": "code", "dev_debug": "code", "dev_coding": "code", "web_research": "web", "memory_manage": "memory",
    "system_status": "system", "task_status": "system", "docker_ops": "system", "recovery_ops": "system", "task_steering": "system",
    "sensitive_action_request": "system", "hardware_stop": "stop", "clarification_needed": "clarify", "reminder_manage": "reminder",
}
TOOL_GROUPS = {"files", "web", "system", "code"}  # groups whose handlers invoke tools / agents


def build_assistant(tmp: Path):
    os.environ["JARVIS_OVERRIDES_FILE"] = str(tmp / "o.json")
    from jarvis.assistant import Assistant
    from jarvis.config import load_settings

    s = load_settings()
    s.notes_dir = str(ROOT / "notes")
    src = sqlite3.connect(f"file:{(ROOT / 'data' / 'jarvis.db').as_posix()}?mode=ro", uri=True)
    (tmp / "data").mkdir()
    dst = sqlite3.connect(tmp / "data" / "jarvis.db")
    src.backup(dst)  # a COPY of the real database (including its noisy memories); the original is never written
    dst.close()
    src.close()
    Assistant._warm_speech = lambda self: None  # type: ignore[method-assign]
    a = Assistant(s, data_dir=tmp / "data")
    mem = a.orchestrator.memory
    for text, cat in (("I prefer Neovim as my editor", "preference"), ("My ERP backend uses PostgreSQL and FastAPI", "project_memory"),
                      ("My dog is called Biscuit", "explicit"), ("I prefer short concise answers", "preference"), ("I dislike Mondays", "explicit")):
        mem.add_personal_memory(content=text, category=cat, source="explicit")
    return a


async def main(out: Path, cases_path: Path) -> dict:
    from jarvis.assistant import _fs_tools, _web_tools
    from jarvis.tool_select import rank_tools, select_tools, tool_name

    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    a = build_assistant(Path(tempfile.mkdtemp(prefix="understanding-")))
    registry = _fs_tools() + _web_tools(True)
    rows = []
    for c in cases:
        a.history.items.clear() if hasattr(a.history, "items") else None
        if c["context"]:
            a.history.add("user", c["context"])
            a.history.add("assistant", "Understood.")
        plan = await a.orchestrator.decision.decide(c["text"], context=a.get_conversation_context())
        intent = plan.decision.intent
        grp = GROUPS.get(intent, "chat")
        ctx = a._gather_context(c["text"])
        sel = [tool_name(t) for t in select_tools(c["text"], registry, k=6)]
        ranked = [tool_name(t) for _, t in rank_tools(c["text"], registry)]
        want_tool = c["tool"]
        pred_clar = intent == "clarification_needed" or plan.workflow == "clarify"
        rows.append({
            "cat": c["cat"], "text": c["text"], "intent": intent, "group": grp, "agent": plan.agent_role, "workflow": plan.workflow,
            "intent_ok": grp in c["group"], "expect_group": c["group"],
            "tool_expected": want_tool, "top1": ranked[0] if ranked else "", "top1_ok": (ranked[0] in want_tool) if want_tool else None,
            "top6_ok": any(t in sel for t in want_tool) if want_tool else None,
            "expect_clarify": c["clarify"], "pred_clarify": pred_clar,
            "memory_expected": c["memory"], "memory_injected": bool(ctx["memories_ctx"]), "memory_texts": [t[:50] for _, t in ctx["memories_ctx"]],
            "tool_routed": grp in TOOL_GROUPS, "no_tool_expected": not want_tool and not (set(c["group"]) & TOOL_GROUPS),
        })
    n = len(rows)
    tool_rows = [r for r in rows if r["tool_expected"]]
    notool = [r for r in rows if r["no_tool_expected"]]
    mem_no = [r for r in rows if not r["memory_expected"]]
    mem_yes = [r for r in rows if r["memory_expected"]]
    clar_exp = [r for r in rows if r["expect_clarify"]]
    pct = lambda x, d: round(100 * x / d, 1) if d else None  # noqa: E731
    by_cat: dict[str, list[bool]] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r["intent_ok"])
    res = {
        "n": n,
        "intent_accuracy_pct": pct(sum(r["intent_ok"] for r in rows), n),
        "wrong_agent_rate_pct": pct(sum(not r["intent_ok"] for r in rows), n),
        "tool_cases": len(tool_rows),
        "tool_top1_pct": pct(sum(bool(r["top1_ok"]) for r in tool_rows), len(tool_rows)),
        "tool_top6_pct": pct(sum(bool(r["top6_ok"]) for r in tool_rows), len(tool_rows)),
        "unnecessary_tool_routing_pct": pct(sum(r["tool_routed"] for r in notool), len(notool)),
        "unnecessary_memory_retrieval_pct": pct(sum(r["memory_injected"] for r in mem_no), len(mem_no)),
        "needed_memory_retrieved_pct": pct(sum(r["memory_injected"] for r in mem_yes), len(mem_yes)),
        "clarification_recall_pct": pct(sum(r["pred_clarify"] for r in clar_exp), len(clar_exp)),
        "false_clarification_pct": pct(sum(r["pred_clarify"] for r in rows if not r["expect_clarify"]), n - len(clar_exp)),
        "intent_accuracy_by_category_pct": {k: pct(sum(v), len(v)) for k, v in by_cat.items()},
    }
    out.write_text(json.dumps({"summary": res, "rows": rows}, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "understanding_results.json"))
    ap.add_argument("--cases", default=str(ROOT / "benchmarks" / "understanding_heldout.json"))
    ns = ap.parse_args()
    print(json.dumps(asyncio.run(main(Path(ns.out), Path(ns.cases))), indent=1))
