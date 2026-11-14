"""Per-request tool selection.

Offering every registered tool (built-ins + every MCP server's tools) to a 4B local model hurts accuracy and latency,
so each turn only the most relevant tools are exposed, plus a `discover_tools` meta-tool that can widen the set.

Scoring is generic - nothing here knows tool names or counts:
  * keyword score : token overlap between the request and the tool's name/description/parameter names (stemmed, IDF-weighted)
  * embedding score: cosine similarity using the project's existing embedding providers (`jarvis.memory.embeddings`);
    used only when a provider is supplied, otherwise keyword-only (the documented fallback).
"""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

DEFAULT_MAX_TOOLS = 6
DISCOVER = "discover_tools"

_EMB_CACHE: dict[tuple[str, str], np.ndarray] = {}
_STOP = frozenset("a an the to of for in on at and or is are be it my me i you your please can could would with from this that do does what how".split())
_SYNONYMS = {  # tiny domain-neutral normalisation, not tool identities
    "find": "search", "lookup": "search", "look": "search", "locate": "search", "google": "search", "browse": "search",
    "show": "read", "open": "read", "view": "read", "display": "read", "cat": "read", "list": "list", "ls": "list",
    "remove": "delete", "erase": "delete", "trash": "delete", "rm": "delete",
    "email": "mail", "message": "mail", "text": "mail",
    "lamp": "light", "lights": "light", "bulb": "light",
    "remind": "task", "reminder": "task", "todo": "task", "to-do": "task",
    "temperature": "climate", "thermostat": "climate", "heating": "climate",
}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    out = []
    for t in re.split(r"[^A-Za-z0-9]+", text.lower()):
        if t and t not in _STOP:
            t = _SYNONYMS.get(t, t)
            out.append(_SYNONYMS.get(_stem(t), _stem(t)))
    return out


def tool_name(t: dict) -> str:
    return (t.get("function") or {}).get("name", "")


def _doc(t: dict) -> str:
    f = t.get("function") or {}
    props = " ".join(((f.get("parameters") or {}).get("properties") or {}).keys())
    # the name counts triple: it is the strongest, least ambiguous signal
    return f"{(f.get('name') or '')} {(f.get('name') or '')} {(f.get('name') or '')} {f.get('description', '')} {props}"


def discover_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": DISCOVER,
            "description": "Find additional tools when none of the available tools can do what the user asked. Pass a short description of the capability you need.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "capability needed, e.g. 'turn off a light'"}}, "required": ["query"]},
        },
    }


def rank_tools(query: str, tools: list[dict], embedder=None) -> list[tuple[float, dict]]:
    """All tools with a relevance score in [0,1], best first."""
    tools = [t for t in tools if tool_name(t) and tool_name(t) != DISCOVER]
    if not tools:
        return []
    q = tokenize(query)
    docs = [tokenize(_doc(t)) for t in tools]
    df = Counter(w for d in docs for w in set(d))
    n = len(docs)
    idf = {w: math.log(1 + n / c) for w, c in df.items()}
    kw = []
    for d in docs:
        ds = set(d)
        hit = sum(idf.get(w, 0.0) for w in set(q) if w in ds)
        denom = sum(idf.get(w, 0.0) for w in set(q) if w in idf) or 1.0
        kw.append(hit / denom)
    scores = np.array(kw, dtype=np.float32)
    if embedder is not None and q:
        try:
            qv = np.asarray(embedder.embed_one(query), dtype=np.float32)
            tag = getattr(embedder, "version_tag", "emb")
            texts = [" ".join(d) for d in docs]
            miss = [x for x in texts if (tag, x) not in _EMB_CACHE]
            if miss:  # tool descriptions rarely change: embed each once, not every turn
                for x, v in zip(miss, embedder.embed_batch(miss)):
                    _EMB_CACHE[(tag, x)] = np.asarray(v, dtype=np.float32)
            dv = np.stack([_EMB_CACHE[(tag, x)] for x in texts])
            nq, nd = np.linalg.norm(qv) or 1.0, np.linalg.norm(dv, axis=1)
            nd[nd == 0] = 1.0
            emb = (dv @ qv) / (nd * nq)
            scores = 0.65 * scores + 0.35 * np.clip(emb, 0, 1)
        except Exception:  # noqa: BLE001  embedding failure must never break a turn: keyword-only fallback
            pass
    order = sorted(range(n), key=lambda i: (-float(scores[i]), tool_name(tools[i])))
    return [(float(scores[i]), tools[i]) for i in order]


def select_tools(query: str, tools: list[dict], k: int = DEFAULT_MAX_TOOLS, embedder=None) -> list[dict]:
    """Top-k relevant tools + the discover_tools meta-tool (always present)."""
    k = max(1, int(k))
    real = [t for t in tools if tool_name(t) and tool_name(t) != DISCOVER]
    if len(real) <= k:  # nothing to filter: offering a small registry whole is the most accurate choice
        return real + [discover_tool_schema()]
    ranked = rank_tools(query, tools, embedder)
    chosen = [t for s, t in ranked[:k] if s > 0]
    if not chosen:
        # No signal at all. Measured with the local 4B model: it does not reliably call discover_tools (0/12 held-out misses were
        # rescued), so hiding tools here would just make the request fail. Offer the registry whole while it is modest (<= 3k),
        # and only fall back to discover_tools alone for registries too large to offer.
        return (real if len(real) <= 3 * k else []) + [discover_tool_schema()]
    return chosen + [discover_tool_schema()]


def discover_more(query: str, all_tools: list[dict], exposed: list[dict], n: int = DEFAULT_MAX_TOOLS, embedder=None) -> list[dict]:
    """Tools not yet exposed that best match `query` (the discover_tools implementation)."""
    have = {tool_name(t) for t in exposed}
    rest = [t for t in all_tools if tool_name(t) not in have and tool_name(t) != DISCOVER]
    return [t for s, t in rank_tools(query, rest, embedder)[:n] if s > 0]


def describe(tools: list[dict]) -> list[dict]:
    return [{"name": tool_name(t), "description": (t["function"].get("description") or "")[:160]} for t in tools]
