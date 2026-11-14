"""Evaluate tool selection + intent judge on benchmarks/phase4_eval.json; prints accuracy, confusions and latency.

  python scripts/eval_phase4.py            # offline (hash embeddings, scripted LLM verdicts for ambiguous utterances)
  python scripts/eval_phase4.py --live-llm # also time the real local Ollama judge on the ambiguous utterances

Results are written to benchmarks/phase4_results.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.assistant import _fs_tools, _web_tools  # noqa: E402
from jarvis.intent_judge import IntentJudge  # noqa: E402
from jarvis.memory.embeddings import DeterministicHashEmbeddingProvider  # noqa: E402
from jarvis.tool_select import DISCOVER, rank_tools, select_tools, tool_name  # noqa: E402

DATA = json.loads((ROOT / "benchmarks" / "phase4_eval.json").read_text(encoding="utf-8"))
if "--heldout" in sys.argv:  # held-out examples: same catalogue, never tuned on
    _held = json.loads((ROOT / "benchmarks" / "phase4_heldout.json").read_text(encoding="utf-8"))
    DATA = {"mcp_tools": DATA["mcp_tools"], "tool_select": _held["tool_select"], "intent": _held["intent"]}


def catalogue() -> list[dict]:
    mcp = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                          "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in t["params"]}}}}
        for t in DATA["mcp_tools"]
    ]
    return _fs_tools() + _web_tools(True) + mcp


def eval_tools(embedder=None, k: int = 6) -> dict:
    cat = catalogue()
    rows, lat = [], []
    for ex in DATA["tool_select"]:
        t0 = time.perf_counter()
        chosen = [tool_name(t) for t in select_tools(ex["q"], cat, k=k, embedder=embedder)]
        lat.append((time.perf_counter() - t0) * 1000)
        ranked = [tool_name(t) for _, t in rank_tools(ex["q"], cat, embedder)]
        rows.append({"q": ex["q"], "expect": ex["expect"], "chosen": chosen,
                     "recall_at_k": any(e in chosen for e in ex["expect"]), "top1": ranked[0] in ex["expect"]})
    n = len(rows)
    return {"n": n, "k": k, "catalogue_size": len(cat), "recall_at_k": sum(r["recall_at_k"] for r in rows) / n, "top1": sum(r["top1"] for r in rows) / n,
            "misses": [r for r in rows if not r["recall_at_k"]], "latency_ms_mean": statistics.mean(lat), "latency_ms_max": max(lat),
            "discover_always_present": all(DISCOVER in r["chosen"] for r in rows)}


class ScriptedBrain:
    """Stands in for the LLM so ambiguous-case routing is deterministic offline."""

    def __init__(self):
        self.verdict = "query"

    async def complete(self, messages):
        return json.dumps({"decision": self.verdict, "query": ""})


async def eval_intent(brain=None) -> dict:
    scripted = brain is None
    brain = brain or ScriptedBrain()
    judge = IntentJudge(brain, timeout_s=8.0 if not scripted else 1.5)
    rows, fast_lat, llm_lat = [], [], []
    for ex in DATA["intent"]:
        if scripted:
            brain.verdict = ex.get("llm_says", "query")
        r = await judge.judge(ex["text"], recent_tts=ex.get("tts_recent"), playback_recent=ex.get("playback", False), deliberate=ex.get("deliberate", False))
        ok = r.decision == ex["expect"] and (not ex.get("query") or r.query.strip().lower().rstrip(".") == ex["query"])
        (fast_lat if r.path == "fast" else llm_lat).append(r.latency_ms)
        rows.append({"text": ex["text"], "expect": ex["expect"], "got": r.decision, "path": r.path, "query": r.query, "ok": ok, "ms": round(r.latency_ms, 2)})
    n = len(rows)
    confusion: dict[str, int] = {}
    for r in rows:
        if not r["ok"]:
            key = f"{r['expect']}->{r['got']}"
            confusion[key] = confusion.get(key, 0) + 1
    return {"n": n, "accuracy": sum(r["ok"] for r in rows) / n, "confusions": confusion, "wrong": [r for r in rows if not r["ok"]],
            "fast_path_n": len(fast_lat), "fast_ms_max": max(fast_lat or [0]), "fast_ms_mean": statistics.mean(fast_lat or [0]),
            "llm_path_n": len(llm_lat), "llm_ms": [round(x) for x in llm_lat], "llm_mode": "scripted" if scripted else "live-ollama"}


async def amain(live: bool) -> dict:
    out = {"tool_select_keyword_only": eval_tools(None), "tool_select_hash_embeddings": eval_tools(DeterministicHashEmbeddingProvider()), "intent_offline": await eval_intent()}
    if live:
        from jarvis.brain import Brain
        from jarvis.config import load_settings

        b = Brain(load_settings())
        await b.complete([{"role": "user", "content": "hi"}])  # warm the model so latency reflects steady state
        out["intent_live_llm"] = await eval_intent(b)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-llm", action="store_true")
    ap.add_argument("--heldout", action="store_true")
    res = asyncio.run(amain(ap.parse_args().live_llm))
    (ROOT / "benchmarks" / ("phase4_heldout_results.json" if "--heldout" in sys.argv else "phase4_results.json")).write_text(json.dumps(res, indent=2), encoding="utf-8")
    for k, v in res.items():
        print(k, {a: b for a, b in v.items() if a not in ("misses", "wrong")})
        for m in v.get("misses", []) + v.get("wrong", []):
            print("   MISS", m)
