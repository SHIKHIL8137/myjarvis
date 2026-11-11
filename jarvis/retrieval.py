"""Keyword retrieval: TF-IDF-ish, title matches weigh 3x. Stdlib only.
Decision: no embeddings. At hundreds of notes keyword search is instant and needs no extra model in RAM/VRAM;
revisit only if retrieval misses are measured (see HARDWARE_BENCHMARK.md)."""
import math
import re
from collections import Counter

STOP = set("a an the and or but if of to in on at for with from by is are was were be been it its this that these those i me my you your "
           "we our do does did what which who how why when where about tell please can could would should have has had not no yes as so "
           "than then there their they them he she his her will just any some jarvis "
           "hello hi hey good morning afternoon evening night thanks thank bye goodbye today well".split())  # greetings are small talk
TOKEN = re.compile(r"[a-z0-9]+|[^\x00-\x7f\s.,;:!?()\[\]{}\"'`\-_/\\|@#$%^&*+=~<>]+")


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall((text or "").lower()) if t not in STOP and len(t) > 1]


def search(notes, query: str, k: int = 4, min_score: float = 0.0) -> list[tuple]:
    """-> [(note, score)] best first. Scores are relative; callers pick their own cut-off."""
    q = set(tokens(query))
    if not q or not notes:
        return []
    docs = [(n, Counter(tokens(n.body)), set(tokens(n.title))) for n in notes]
    df = Counter(t for _, tf, tt in docs for t in set(tf) | tt)
    N = len(docs)
    out = []
    for n, tf, tt in docs:
        s = 0.0
        for t in q:
            idf = math.log(1 + N / (1 + df.get(t, 0)))
            s += idf * (3.0 * (t in tt) + math.log1p(tf.get(t, 0)))
        if s > min_score:
            out.append((n, s))
    return sorted(out, key=lambda x: -x[1])[:k]
