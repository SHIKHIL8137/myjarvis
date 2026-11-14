"""Typo-tolerant text for ROUTING ONLY (the rule-based command/intent matchers). The language model always receives the user's original words.

Only the commonest keyboard slips are repaired - adjacent-letter swaps ("fiels", "cancle", "waht") and one dropped *interior* letter
("downlods", "serch", "remmber", "temprature") - and only toward a fixed vocabulary of command words, never toward ordinary English.
Dropped first letters and substitutions are deliberately NOT repaired because they collide with real words ("older" vs "folder").
"""
from __future__ import annotations

import re

VOCAB = (
    "remember reminder search folder folders directory files documents downloads desktop pictures project projects memory status "
    "temperature processor cancel stop quiet silence settings configuration notes online internet usage what whats where which "
    "please show open find list explain forget schedule screenshot volume microphone"
).split()

# real English words that happen to be one slip away from a vocabulary word: never "repaired"
_REAL = frozenset(
    "flies older form fold from order other there their these three where here were wher more most tell well still shown shows "
    "past pats stoop spot post pots tops opens oven pen open sear seach sash fires fines fields filed filer tiles lifes".split()
)


def _variants(word: str) -> set[str]:
    out: set[str] = set()
    for i in range(len(word) - 1):  # adjacent swap
        if word[i] != word[i + 1]:
            out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
    for i in range(1, len(word) - 1):  # one dropped interior letter
        out.add(word[:i] + word[i + 1:])
    return out


_MAP: dict[str, str] = {}
for _w in VOCAB:
    if len(_w) >= 4:
        for _v in _variants(_w):
            if len(_v) >= 4 and _v not in VOCAB and _v not in _REAL:
                _MAP.setdefault(_v, _w)

_WORD = re.compile(r"[A-Za-z]+")


def normalize_for_routing(text: str) -> str:
    """Return `text` with known single-slip typos of command words repaired (case preserved loosely: replacements are lower-case)."""
    if not text:
        return text

    def fix(m: re.Match[str]) -> str:
        w = m.group(0)
        rep = _MAP.get(w.lower())
        return rep if rep else w

    return _WORD.sub(fix, text)
