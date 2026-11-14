"""Full-pipeline routing evaluation WITHOUT LLM generation latency: the real Assistant (rules -> structured request -> decision engine ->
memory/notes gating -> tool offering) runs in-process on a COPY of the real database; the model is replaced by an instant stub, so the route
(`command` / decision.intent), the memory ids injected, and the tools offered are the production ones, and nothing slow or paid is called.
OS actions are disabled for the run (a request to "open Notepad" must not launch it).
  python scripts/eval_route.py --cases benchmarks/understanding_heldout_v2.json --out benchmarks/understanding_route_v2.json
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
sys.path.insert(0, str(ROOT / "scripts"))

from eval_e2e import CMD_GROUP, SECRET  # noqa: E402
from summarize_e2e import summarise  # noqa: E402

INTENT_GROUP = {
    "general_chat": "chat", "empty": "chat", "filesystem_ops": "files", "project_exploration": "files", "document_export": "files",
    "dev_architecture": "code", "dev_review": "code", "dev_debug": "code", "dev_coding": "code", "web_research": "web", "memory_manage": "memory",
    "system_status": "system", "task_status": "system", "docker_ops": "system", "recovery_ops": "system", "task_steering": "system",
    "sensitive_action_request": "system", "hardware_stop": "stop", "clarification_needed": "clarify", "reminder_manage": "reminder",
}


def build(tmp: Path):
    os.environ["JARVIS_OVERRIDES_FILE"] = str(tmp / "o.json")
    from jarvis.assistant import Assistant
    from jarvis.brain import Brain
    from jarvis.config import load_settings

    class Stub(Brain):
        async def installed(self):
            return ["qwen3:4b-instruct"]

        async def health(self):
            return {"up": True, "version": "stub", "models": ["qwen3:4b-instruct"], "loaded": []}

        async def stream_events(self, messages, tools=None, **kw):
            self.offered = [t["function"]["name"] for t in (tools or [])]
            yield {"type": "token", "text": "OK."}
            self.last_stats = {"ttft": 0.0, "tokens": 1, "tps": 1.0, "total": 0.0, "model": "stub", "truncated": False}

        async def stream(self, messages):
            yield "OK."

        async def complete(self, messages):
            return '{"decision":"query","query":""}'

    s = load_settings()
    s.notes_dir = str(ROOT / "notes")
    s.actions.enabled = False
    src = sqlite3.connect(f"file:{(ROOT / 'data' / 'jarvis.db').as_posix()}?mode=ro", uri=True)
    (tmp / "data").mkdir()
    dst = sqlite3.connect(tmp / "data" / "jarvis.db")
    src.backup(dst)
    dst.close()
    src.close()
    Assistant._warm_speech = lambda self: None  # type: ignore[method-assign]
    a = Assistant(s, data_dir=tmp / "data", brain=Stub(s))
    for text, cat in (("I prefer Neovim as my editor", "preference"), ("My ERP backend uses PostgreSQL and FastAPI", "project_memory"),
                      ("My dog is called Biscuit", "explicit"), ("I prefer short concise answers", "preference"), ("I dislike Mondays", "explicit")):
        a.orchestrator.memory.add_personal_memory(content=text, category=cat, source="explicit")
    return a


async def turn(a, text: str) -> dict:
    meta: dict = {}
    tools: list[str] = []
    answer = ""
    async for ev in a.stream_turn(text, speak=False):
        if ev.get("type") == "meta" and not meta:
            meta = ev
        elif ev.get("type") == "tool":
            tools.append(str(ev.get("name")))
        elif ev.get("type") == "done":
            answer = str(ev.get("answer") or "")
            meta = {**meta, "command_final": ev.get("command")}
    return {"meta": meta, "tools": tools, "answer": answer, "offered": getattr(a.brain, "offered", [])}


async def main(cases_path: Path, out: Path) -> dict:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    a = build(Path(tempfile.mkdtemp(prefix="route-")))
    rows = []
    for c in cases:
        a.history.clear()
        a.pending_clarification = None
        a.brain.offered = []
        if c["context"]:
            await turn(a, c["context"])
            a.brain.offered = []
        r = await turn(a, c["text"])
        meta = r["meta"]
        cmd = meta.get("command") or meta.get("command_final")
        intent = (meta.get("decision") or {}).get("intent") or ""
        grp = CMD_GROUP.get(cmd) if cmd in CMD_GROUP else INTENT_GROUP.get(intent, "chat")
        ans = r["answer"]
        clarified = grp == "clarify" or (grp == "chat" and ans.strip().endswith("?") and len(ans) < 260)
        rows.append({
            **{k: v for k, v in c.items() if k != "group"}, "expect_group": c["group"], "command": cmd, "intent": intent, "group": grp, "intent_ok": grp in c["group"],
            "offered_tools": r["offered"], "tools_used": r["tools"], "memory_ids": meta.get("memory_ids") or [], "memory_injected": bool(meta.get("memory_ids")),
            "clarified": clarified, "tool_routed": grp in ("files", "web", "system", "code"), "permission_prompt": cmd == "permission_required",
            "fabricated_claim": "Honesty notice" in ans, "secret_leak": bool(SECRET.search(ans)), "answer_len": len(ans), "answer": ans[:200], "seconds": 0.0, "ok": True,
        })
    summary = summarise(rows)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "benchmarks" / "understanding_heldout.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "understanding_route.json"))
    ns = ap.parse_args()
    print(json.dumps(asyncio.run(main(Path(ns.cases), Path(ns.out))), indent=1))
