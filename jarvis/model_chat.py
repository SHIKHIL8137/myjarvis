"""Chat understanding for model management. Token-based (no fixed phrases): a request has a "models" noun (typo-tolerant) or a model target plus an action word.
Everything it does goes through `ModelCatalog` (discovery, recommendation, validated selection) - no second registry."""
from __future__ import annotations

import difflib
import re

from .model_catalog import ModelCatalog

_WORD = re.compile(r"[a-z0-9][a-z0-9.\-:/_]*", re.I)
_LIST = {"show", "list", "display", "see", "available", "connected", "installed", "which", "what", "have", "got", "give", "get", "browse", "catalog", "catalogue"}
_REC = {"recommend", "suggest", "best", "better", "ideal", "suitable", "top", "advise"}
_CMP = {"compare", "versus", "vs", "difference", "between"}
_USE = {"use", "switch", "change", "select", "pick", "try", "set", "go", "swap", "move"}
_SUPER = {"fastest", "cheapest", "quickest"}
_NOISE = {"to", "the", "model", "models", "back", "a", "an", "please", "my", "confirm", "confirmed", "paid", "with", "available", "and", "compare", "me", "now"}
_TASK_WORDS = (
    ("coding", ("cod", "debug", "program", "develop", "software")), ("translation", ("translat",)), ("reasoning", ("reason", "think", "math", "logic")),
    ("long_documents", ("long", "document", "context", "book", "pdf")), ("vision", ("vision", "image", "picture", "photo", "visual")),
    ("speech", ("speech", "transcri", "voice", "audio")), ("low_cost", ("cheap", "cost", "budget", "afford", "economic")),
    ("fast", ("fast", "quick", "speed", "latenc", "snappy")),
)


def _is_models_word(w: str) -> bool:
    return w in ("model", "models", "llm", "llms") or (len(w) >= 5 and bool(difflib.get_close_matches(w, ["models", "model"], n=1, cutoff=0.8)))


def parse_model_request(text: str) -> dict | None:
    toks = [t.lower().strip(".,?!") for t in _WORD.findall(text or "")]
    if not toks or len(toks) > 30:
        return None
    s = set(toks)
    has_models = any(_is_models_word(t) for t in toks)
    provider = next((p for p, ks in (("ollama", ("ollama", "local", "installed")), ("openrouter", ("openrouter",)), ("openai", ("openai", "gpt"))) if s & set(ks)), "")
    free = True if "free" in s else (False if s & {"paid", "premium"} else None)
    task = next((t for t, stems in _TASK_WORDS if any(tok.startswith(stem) for tok in toks for stem in stems)), "")
    # asking ABOUT models as a concept ("what is a language model") is not a management request
    if len(toks) >= 2 and toks[0] in ("what", "whats", "explain", "define", "how") and not (s & (_REC | {"available", "connected", "installed", "free", "using", "current", "active"})) and toks[1] in ("is", "are", "does", "do", "s", "to", "models"):
        if not (has_models and toks[0] == "what" and toks[1] == "models"):
            return None
    ids = [t for t in toks if "/" in t or any(c.isdigit() for c in t)]
    if (s & _CMP) and (has_models or len(ids) >= 2):
        return {"action": "compare", "tokens": toks}
    if re.search(r"\b(?:which|what)\s+(?:model|llm)\b.*\b(?:using|active|current|running|now)\b|\b(?:current|active)\s+model\b", " ".join(toks)):
        return {"action": "current"}
    if (s & _SUPER) and (s & _USE):
        return {"action": "use_best", "task": "low_cost" if "cheapest" in s else "fast"}
    if has_models and (s & _REC or s & _SUPER):
        return {"action": "recommend", "task": task or ("fast" if s & {"fastest", "quickest"} else "low_cost" if "cheapest" in s else "general"), "provider": provider, "free": free}
    if (s & _SUPER) and (s & _USE):
        return {"action": "use_best", "task": "low_cost" if "cheapest" in s else "fast"}
    plural = bool(s & {"models", "llms"}) or any(_is_models_word(t) and t not in ("model", "llm") for t in toks)
    verb = bool(s & (_LIST - {"what", "which", "have", "got", "get", "give"})) or (plural and bool(s & {"what", "which", "many", "count", "number", "have", "got", "installed", "using"}))
    terse = (provider or free is not None) and len(toks) <= 5 and toks[0] not in ("how", "why", "do", "does", "should", "can", "is", "are")
    if has_models and (verb or terse):
        return {"action": "list", "provider": provider, "free": free}
    if s & _USE and toks[0] in (_USE | {"please", "can", "could", "hey", "jarvis", "would"}):
        i = next(i for i, t in enumerate(toks) if t in _USE)
        rest = [t for t in toks[i + 1:] if t not in _NOISE]
        if rest:
            return {"action": "use", "target": " ".join(rest), "confirm": bool(s & {"confirm", "confirmed"}), "back": "back" in s}
    return None


def _fmt_price(m: dict) -> str:
    if m.get("free"):
        return "free"
    if m.get("price_in") is None:
        return "paid (price not provided)"
    return f"${m['price_in']}/${m['price_out']} per 1M in/out"


def _use_reply(key: str, out: dict, extra: str) -> dict:
    if out.get("ok"):
        c = out["current"]
        return {"reply": f"Switched to {c['provider']}:{c['model']}{extra}. The next message will use it.", "ok": True, "ui": None}
    if out.get("status") == "needs_confirmation":
        p = out.get("pricing", {})
        return {"reply": (f"{key} is a paid model (input {p.get('input_per_1m_usd')}, output {p.get('output_per_1m_usd')} USD per 1M tokens). "
                          f"Nothing was switched. Confirm in the model selector, or say 'use {key} confirm'."), "ok": False, "ui": {"model_picker": {"q": key.split(":", 1)[-1]}}}
    return {"reply": f"Could not switch to {key}: {out.get('error')}", "ok": False, "ui": None}


async def handle_chat(a, text: str) -> dict | None:
    """{reply, ok, ui} for a model-management request, or None to let normal routing continue."""
    req = parse_model_request(text)
    if not req:
        return None
    cat: ModelCatalog = a.model_catalog
    act = req["action"]
    if act == "current":
        return {"reply": f"You are using {a.brain.spec.model} (key {a.brain.current}, provider: {a.brain.provider}).", "ok": True, "ui": None}
    if act == "list":
        res = await cat.listing(provider=req["provider"], free=req["free"])
        parts = []
        for st in res["providers"]:
            line = f"{st['provider']}: {st['count']} models ({st['status']})"
            if st.get("stale") and st["count"]:
                line += " - STALE, not freshly verified"
            if st.get("error"):
                line += f" - {st['error']}"
            parts.append(line)
        return {"reply": f"Available models ({res['total']} match). Current: {res['current']['provider']}:{res['current']['model']}.\n" + "\n".join(parts) + "\nPick one in the selector below.",
                "ok": True, "ui": {"model_picker": {"provider": req["provider"], "free": req["free"], "task": ""}}}
    if act == "recommend":
        res = await cat.listing(provider=req["provider"], free=req["free"])
        rec = cat.recommend(req["task"], res["models"])
        ui = {"model_picker": {"task": req["task"], "provider": req["provider"], "free": req["free"]}}
        if not rec["recommendations"]:
            return {"reply": f"No discovered model has metadata supporting '{req['task']}'. Connect a provider or refresh the catalog.", "ok": True, "ui": ui}
        lines = [f"{i}. {r['provider']}:{r['model']} ({_fmt_price(r)}) - " + "; ".join(r["reasons"]) for i, r in enumerate(rec["recommendations"], 1)]
        return {"reply": f"Recommended for {req['task'].replace('_', ' ')}:\n" + "\n".join(lines) + "\n" + rec["note"] + " Paid models need your confirmation before switching.", "ok": True, "ui": ui}
    if act == "compare":
        await cat.listing()
        skip = _CMP | _LIST | _REC | _NOISE | {"models", "model", "or", "of"}
        found: list[dict] = []
        for n in [t for t in req["tokens"] if t not in skip]:
            for m in cat.find(n):
                if m not in found:
                    found.append(m)
        if not found:
            found = (await cat.listing())["models"][:4]
        rows = [f"- {m['provider']}:{m['model']}: {_fmt_price(m)}; context {m['context'] or 'not provided'}; inputs {', '.join(m['inputs']) or 'not provided'}; "
                f"tools {'not provided' if m['tools'] is None else ('yes' if m['tools'] else 'no')}" for m in found[:6]]
        return {"reply": "Comparison (provider metadata only; 'not provided' means the provider does not say):\n" + "\n".join(rows), "ok": True, "ui": None}
    if act == "use_best":
        res = await cat.listing()
        rec = cat.recommend(req["task"], [m for m in res["models"] if m["usable"]])
        if not rec["recommendations"]:
            return {"reply": "No usable model found to choose from.", "ok": False, "ui": None}
        top = rec["recommendations"][0]
        return _use_reply(top["key"], await cat.select(top["key"]), " (" + "; ".join(top["reasons"]) + ")")
    if act == "use":
        target = req["target"]
        if target in ("ollama", "local", "installed", "local ollama", "ollama local"):
            return _use_reply(a.settings.boot_brain, await cat.select(a.settings.boot_brain), "")
        near = difflib.get_close_matches(target, ["openrouter", "router", "openai", "ollama", "local"], n=1, cutoff=0.75)
        if near and not re.search(r"[\d/]", target):  # "router" / "open router" / "opnai" mean the provider
            target = {"router": "openrouter", "local": "ollama"}.get(near[0], near[0])
        target = target.replace("open router", "openrouter")
        if target == "openrouter":
            return {"reply": "Which OpenRouter model? Pick one below.", "ok": False, "ui": {"model_picker": {"provider": "openrouter"}}}
        if target == "openai":
            return {"reply": "Which OpenAI model? Pick one below.", "ok": False, "ui": {"model_picker": {"provider": "openai"}}}
        if not (re.search(r"[\d/\-.]", target) or {"openai", "openrouter", "gpt", "claude", "gemini", "deepseek", "llama", "qwen", "mistral"} & set(target.split())):
            return None  # not model-like ("my notes", "voice male"): no catalog lookup, normal routing continues
        await cat.listing(provider="openai" if target.startswith(("gpt", "o1", "o3", "o4", "chatgpt")) else "")
        await cat.refresh("openrouter") if "/" in target or "openrouter" in target else None
        t = target.replace("openrouter", "").replace("openai", "").strip()
        hits = cat.find(t) if t else []
        if not hits and target in ("openai", "openrouter"):
            return {"reply": f"Which {target} model? Pick one below.", "ok": False, "ui": {"model_picker": {"provider": target}}}
        exact = [m for m in hits if m["model"].lower() == t.lower()]
        hits = exact or hits
        if not hits:
            return None  # legacy aliases ("fast", "reasoning", an Ollama tag) continue through the existing switch
        if len(hits) > 1:
            return {"reply": f"{len(hits)} models match '{target}': " + ", ".join(m["key"] for m in hits[:6]) + ". Which one?", "ok": False, "ui": {"model_picker": {"q": t}}}
        return _use_reply(hits[0]["key"], await cat.select(hits[0]["key"], confirm_paid=req["confirm"]), "")
    return None
