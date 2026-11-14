"""End-to-end understanding run: every held-out request goes through the REAL running app (HTTP /api/chat) with the LOCAL model only.

Run it against an ISOLATED instance (copy of the data, local provider, offline) - never against your real data when a paid provider is active.
Destructive-sounding cases are skipped live (they are covered by approval tests): shutdown, delete, run command.
  python scripts/eval_e2e.py --port 4720 --out benchmarks/understanding_e2e_results.json [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_LIVE = re.compile(r"shut\s*down|delete the file|run the command", re.I)
CMD_GROUP = {
    "stop": "stop", "cancel_task": "stop", "pause_voice": "system",
    "remember": "memory", "recall_memory": "memory", "forget_memory": "memory", "update_memory": "memory", "confirm_memory": "memory", "memory_manage": "memory",
    "find": "files", "search_files": "files", "read_file": "files", "inspect_project": "files", "open_file": "files", "show_notes": "files",
    "filesystem_ops": "files", "filesystem_read": "files", "filesystem_metadata": "files", "filesystem_write": "files", "filesystem_delete": "files",
    "filesystem_blocked": "files", "invalid_path": "files", "permission_required": "files", "document_export": "files", "dev_project_review": "code",
    "dev_autonomous": "code", "system_status": "system", "task_status": "system", "show_recent_tasks": "system", "last_request_status": "system",
    "show_failed_tasks": "system", "count": "system", "docker_ops": "system", "recovery_ops": "system", "take_screenshot": "system", "open_app": "system",
    "focus_window": "system", "list_windows": "system", "media_control": "system", "resume_voice": "system", "approval_required": "system",
    "reminder": "reminder", "reminder_list": "reminder", "deep_research": "web", "web_offline": "web", "clarify": "clarify",
    "clarification_needed": "clarify", "ui_drawer": "system", "ui_focus": "system", "switch": "system",
}
SECRET = re.compile(r"sk-[A-Za-z0-9]{12,}|OPENAI_API_KEY\s*=\s*\S+|TESTSECRET", re.I)


def call(port: int, method: str, path: str, body: dict | None = None, timeout: float = 300):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"X-Jarvis": "1", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")


def group_of(d: dict) -> str:
    cmd = d.get("command")
    if cmd in CMD_GROUP:
        return CMD_GROUP[cmd]
    tools = d.get("tools_used") or []
    if any(str(t).startswith("fs_") for t in tools):
        return "files"
    if "web_search" in tools or "fetch_webpage" in tools:
        return "web"
    return "chat"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4720)
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "understanding_e2e_results.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cases", default=str(ROOT / "benchmarks" / "understanding_heldout.json"))
    args = ap.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[: args.limit]
    rows = []
    for i, c in enumerate(cases, 1):
        if SKIP_LIVE.search(c["text"]):
            rows.append({**c, "skipped": "destructive: not executed live"})
            continue
        try:
            call(args.port, "DELETE", "/api/history")
            if c["context"]:
                call(args.port, "POST", "/api/chat", {"message": c["context"]})
            t0 = time.time()
            d = call(args.port, "POST", "/api/chat", {"message": c["text"]})
            sec = round(time.time() - t0, 1)
        except Exception as e:  # noqa: BLE001
            rows.append({**c, "error": str(e)[:120]})
            print(i, "ERR", c["text"][:40], flush=True)
            continue
        ans = d.get("answer") or ""
        grp = group_of(d)
        clarified = grp == "clarify" or (grp == "chat" and ans.strip().endswith("?") and len(ans) < 260)
        r = {
            **{k: v for k, v in c.items() if k != "group"}, "expect_group": c["group"], "command": d.get("command"), "tools_used": d.get("tools_used"),
            "group": grp, "intent_ok": grp in c["group"], "seconds": sec,
            "memory_ids": d.get("memory_ids") or [], "memory_injected": bool(d.get("memory_ids")), "clarified": clarified,
            "tool_routed": grp in ("files", "web", "system", "code"), "permission_prompt": d.get("command") == "permission_required",
            "fabricated_claim": "Honesty notice" in ans, "secret_leak": bool(SECRET.search(ans)), "answer_len": len(ans), "answer": ans[:300],
            "ok": bool(d.get("ok", True)),
        }
        rows.append(r)
        with open(str(args.out) + ".partial.jsonl", "a", encoding="utf-8") as pf:  # a crash must never lose a 25-minute run
            pf.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(i, "OK " if r["intent_ok"] else "MISS", grp.ljust(8), c["text"][:60], flush=True)
    live = [r for r in rows if "expect_group" in r]
    pct = lambda x, n: round(100 * x / n, 1) if n else None  # noqa: E731
    clar = [r for r in live if r["clarify"]]
    nomem = [r for r in live if not r["memory"]]
    # expected-no-tool: no tool listed AND none of the acceptable groups is a tool-using group
    notool = [r for r in live if not r["tool"] and not ({"files", "web", "system", "code"} & set(r["expect_group"]))]
    summary = {
        "n_live": len(live), "n_skipped": sum("skipped" in r for r in rows), "n_error": sum("error" in r for r in rows),
        "intent_accuracy_pct": pct(sum(r["intent_ok"] for r in live), len(live)),
        "wrong_agent_rate_pct": pct(sum(not r["intent_ok"] for r in live), len(live)),
        "unnecessary_memory_retrieval_pct": pct(sum(r["memory_injected"] for r in nomem), len(nomem)),
        "clarification_recall_pct": pct(sum(r["clarified"] for r in clar), len(clar)),
        "false_clarification_pct": pct(sum(r["clarified"] for r in live if not r["clarify"]), len([r for r in live if not r["clarify"]])),
        "unnecessary_tool_routing_pct": pct(sum(r["tool_routed"] for r in notool), len(notool)),
        "fabricated_claims": sum(r["fabricated_claim"] for r in live), "secret_leaks": sum(r["secret_leak"] for r in live),
        "mean_seconds": round(sum(r["seconds"] for r in live) / max(1, len(live)), 1),
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
