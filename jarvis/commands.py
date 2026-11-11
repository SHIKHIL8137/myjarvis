"""Voice/text routes, checked BEFORE a message goes to the brain. Deterministic, so a 4B model never has to emit control tags.
Every reply is a receipt: it is only produced after the action actually happened (or failed, and says so)."""
import re

from .brain import BrainError
from .retrieval import search

REMEMBER = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:remember(?:\s+that)?|note to self|don'?t let me forget(?:\s+that)?|log this|make a note(?:\s+that)?)[\s:,\-]+(?P<t>.+)$", re.I | re.S)
STOP = re.compile(r"^\W*(?:jarvis\W+)?(?:stop|wait|be quiet|quiet|cancel|enough|shut up|stop talking)\W*$", re.I)
SWITCH = re.compile(r"^\W*(?:jarvis\W+)?(?:please\s+)?(?:switch|change|go)\s+(?:over\s+)?(?:to|back to)\s+(?:the\s+|your\s+)?(?P<n>.+?)(?:\s+(?:model|brain|mode))?\W*$", re.I)
WHICH = re.compile(r"\b(?:which|what)\s+(?:model|brain)\b", re.I)
COUNT = re.compile(r"\bhow many notes\b", re.I)
SHOW = re.compile(r"^\W*(?:jarvis\W+)?(?:please\s+)?(?:show|open|list)\s+(?:me\s+)?(?:my\s+|the\s+)?notes\W*$", re.I)
FIND = re.compile(r"^\W*(?:jarvis\W+)?(?:please\s+)?(?:find\s+(?:information|info|notes|anything)\s+(?:about|on)|search\s+(?:my\s+)?(?:knowledge(?:\s+base)?|notes)"
                  r"(?:\s+(?:for|about))?)(?:\s+(?P<q>.+?))?\W*$", re.I)
WEB_EXPLICIT = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:search\s+(?:the\s+)?(?:web|internet|online)|look\s+up\s+online|google)\b", re.I)
ALIASES = {"thinking": "local_thinking", "reasoning": "local_thinking", "smart": "local_thinking", "deep": "local_thinking",
           "fast": "local", "quick": "local", "default": "local", "normal": "local", "instruct": "local", "local": "local"}


async def route(a, text: str) -> dict | None:
    """-> {"name", "reply", "nodes": [ids]} when handled, else None. `a` is the Assistant."""
    if WEB_EXPLICIT.match(text) and not a.settings.internet.enabled:
        return {
            "name": "web_offline",
            "reply": "I am currently in Local Mode with internet access disabled, sir. Enable Online Assisted Mode in System settings if you would like me to search the web.",
            "nodes": [],
            "ok": False,
        }

    if STOP.match(text):
        a.speaker.stop()
        return {"name": "stop", "reply": "Of course, sir.", "nodes": [], "speak": False}

    m = REMEMBER.match(text)
    if m:
        body = m.group("t").strip()
        try:
            related = [n.title for n, s in search(a.store.all(), body, 2) if s >= 5.0]
            note = a.store.add(body, related=related)
        except Exception as e:  # never let a capture fail silently
            return {"name": "remember", "reply": f"I'm afraid I could not save that, sir: {e}", "nodes": [], "ok": False}
        a.bus.publish({"type": "graph_changed", "new": [note.path]})
        return {"name": "remember", "reply": f"Noted, sir. I've saved it as '{note.title}'.", "nodes": [note.id], "note": note.path, "ok": True}

    if SHOW.match(text):
        return {"name": "show_notes", "reply": f"Opening your notes, sir. There are {len(a.store.all())}.", "nodes": [], "ui": {"open": "notes"}}

    m = FIND.match(text)
    if m:
        q = (m.group("q") or "").strip()
        if not q:
            return {"name": "find", "reply": "What should I look for, sir?", "nodes": [], "ui": {"focus": "search"}}
        hits = search(a.store.all(), q, 6)
        if not hits:
            return {"name": "find", "reply": f"I found nothing about {q} in your notes, sir.", "nodes": [], "ok": False}
        names = ", ".join(n.title for n, _ in hits[:4])
        return {"name": "find", "reply": f"I found {len(hits)} notes about {q}, sir: {names}.", "nodes": [n.id for n, _ in hits],
                "paths": [n.path for n, _ in hits], "ui": {"search": q}}

    m = SWITCH.match(text)
    if m:
        want = m.group("n").strip().lower()
        installed = await a.brain.installed()
        key = ALIASES.get(want) or next((t for t in installed if t.lower() == want or t.lower().startswith(want)), None)
        if key is None:
            return {"name": "switch", "reply": f"I have no model called {want}, sir. I have {', '.join(installed)}.", "nodes": [], "ok": False}
        try:
            info = await a.brain.switch(key)
        except BrainError as e:
            return {"name": "switch", "reply": str(e), "nodes": [], "ok": False}
        a.bus.publish({"type": "model", "key": info["key"], "model": info["model"]})
        a.warm_brain()
        return {"name": "switch", "reply": f"Switched to {info['model']}, sir. It may take a moment to load.", "nodes": [], "ok": True}

    if WHICH.search(text) and len(text) < 60:
        s = a.brain.spec
        return {"name": "which", "reply": f"I'm thinking with {s.model}, running locally on this machine, sir.", "nodes": []}
    if COUNT.search(text):
        n = len(a.store.all())
        return {"name": "count", "reply": f"I have {n} notes indexed, sir.", "nodes": []}
    return None
