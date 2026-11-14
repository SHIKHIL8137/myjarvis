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


# ---- context isolation helpers (used by Assistant._gather_context) ---------------------------------------------------------
_NO_CONTEXT = re.compile(
    r"\b(?:ignore|forget|disregard)\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|earlier|past|our)\s+(?:context|conversation|chat|messages?|history)\b"
    r"|\bwithout\s+(?:using\s+)?(?:any\s+)?(?:the\s+)?(?:previous|prior|earlier|past|my)\s+(?:context|conversation|chat|memory|memories|history)\b"
    r"|\b(?:do\s*n[o']?t|don'?t|never)\s+use\s+(?:any\s+|the\s+)?(?:previous|prior|earlier|past|my)?\s*(?:context|conversation|chat|memory|memories|history)\b"
    r"|\b(?:new|different|unrelated)\s+(?:topic|subject|question)\b|\bfresh\s+start\b|\bstart\s+(?:over|fresh|a\s+new\s+(?:chat|conversation))\b",
    re.I,
)
_ANAPHORA = re.compile(
    r"\b(?:it|its|that|this|those|these|them|they|he|she|there|then|again|also|too|same|another|other|more|previous|last|earlier|above|"
    r"before|instead|continue|go\s+on|keep\s+going|what\s+about|how\s+about|and\s+(?:what|how|why|the)|why\s+(?:not|so)|really|ok(?:ay)?\s+and)\b",
    re.I,
)


def no_context_requested(text: str) -> bool:
    """The user explicitly asked JARVIS not to use earlier conversation/memory ('ignore previous context', 'new topic'...)."""
    return bool(_NO_CONTEXT.search(text or ""))


def is_followup(text: str) -> bool:
    """A short message only borrows the previous question when it actually depends on it (anaphora/ellipsis), never just for being short."""
    t = (text or "").strip()
    if not t:
        return False
    if _ANAPHORA.search(t):
        return True
    return len(re.findall(r"\w+", t)) <= 2 and len(tokens(t)) <= 1 and not re.search(r"\b(?:stop|cancel|quiet|pause|resume|status)\b", t, re.I)  # bare fragment like "tomorrow?"


def grounded(query: str, *texts: str, min_shared: int = 2, frac: float = 0.5) -> bool:
    """Query-dependent evidence that a memory is about the question: enough shared content words (stop words excluded).

    Ranking scores contain query-independent terms (recency, confidence), so a score threshold alone lets unrelated memories through.
    """
    q = set(tokens(query))
    if not q:
        return False
    d = set(tokens(" ".join(texts)))
    shared = len(q & d)
    if len(q) <= 2 and any(len(t) >= 8 for t in q & d):
        return True  # a short query whose one distinctive word ("quantization") matches is on-topic even if the other word is filler ("versus")
    need = min(min_shared, len(q)) if len(q) <= 2 else max(min_shared, math.ceil(frac * len(q)))
    return shared >= need and shared >= 1


_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'll|i'd|my|mine|me|we|we're|our|ours)\b", re.I)
_QUESTION_START = re.compile(r"^\W*(?:what|whats|who|when|where|why|how|which|can|could|would|will|do|does|did|is|are|am|tell|show|open|find|check|"
                             r"search|read|write|create|make|set|turn|play|stop|cancel|list|run|start|please|hey|hello|hi|ok|okay|jarvis)\b", re.I)


def worth_remembering(text: str) -> bool:
    """Should a free-form utterance become a 'conversation memory'? Only declarative first-person statements that are not noise.

    Questions, commands, greetings, repeated-token speech-recognition hallucinations and fragments are conversation, not memory.
    """
    t = (text or "").strip()
    if len(t.split()) < 6 or "?" in t or _QUESTION_START.match(t):
        return False
    toks = TOKEN.findall(t.lower())
    if toks and len(set(toks)) / len(toks) < 0.6:  # "Qwen, Qwen, Qwen..." / "I can't, I can't..." loops
        return False
    return bool(_FIRST_PERSON.search(t))


_LOCAL_ARTIFACT = re.compile(
    r"\b(?:my|our|the|this)\s+(?:[\w.-]+\s+){0,4}?(?:project|projects|integration|codebase|code|config(?:uration)?|configuration\s+file|file|files|folder|folders|"
    r"directory|repo|repository|app|script|module|setup|source|implementation|\.env)\b"
    r"|\b(?:find|search|locate|grep|inspect|read|open|summari[sz]e|check)\s+(?:the\s+)?(?:[\w.-]+\s+){0,5}?(?:file|files|folder|code|config(?:uration)?|references?|module|script)\b",
    re.I,
)


def refers_to_local_artifact(text: str) -> bool:
    """The request is about the user's own files/projects/code (so general-knowledge answers or "I have no access" are wrong)."""
    return bool(_LOCAL_ARTIFACT.search(text or ""))


LOCAL_ARTIFACT_HINT = (
    "[Instruction for this turn: the user is asking about their OWN local files/projects. Look first with the filesystem tools "
    "(fs_search_files / fs_list_directory / fs_read_file / fs_get_project_structure) before answering; never answer from general knowledge "
    "or claim you lack access without trying. Report only what the tools actually returned. Secrets are redacted by the tools: never reveal or guess them.]\n"
)


_ACTION_CLAIM = re.compile(
    r"\bI\s+(?:have\s+|had\s+|just\s+|already\s+)?(?:attempted|tried|searched|looked|inspected|checked|scanned|opened|read|analy[sz]ed|reviewed|verified|tested|"
    r"ran|executed|accessed|found)\b|\b(?:the\s+)?(?:filesystem\s+)?tools?\s+(?:returned|reported|found)\b|\bno\s+(?:matches|relevant\s+files)\s+(?:were\s+)?found\b",
    re.I,
)


def claims_unperformed_action(answer: str, tools_used: list | None) -> bool:
    """The reply says it searched/opened/read/tested something although no tool ran this turn (a fabricated outcome)."""
    return not tools_used and bool(_ACTION_CLAIM.search(answer or ""))


UNPERFORMED_NOTICE = (
    "\n\nHonesty notice: no file or system tool actually ran for this reply, so any statement above about searching, opening, reading "
    "or testing something is not based on real results. Ask again with a folder name or path and I will run the tools."
)
