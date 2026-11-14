"""Translation evaluation against the running app's POST /api/voice/translate (local model). Automatic checks + stored outputs for human review.

Checks: target script, protected tokens (numbers, file names, API terms) preserved, token/char-bigram F1 vs the reference, language-detection
of the source, and 'translate, don't answer' (the output must not simply answer or add chatter).
  python scripts/eval_translation.py --port 4720
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RANGES = {"ml": (0x0D00, 0x0D7F), "hi": (0x0900, 0x097F), "ta": (0x0B80, 0x0BFF)}
DIGITS = {c: str(i) for base in (0x0966, 0x0D66, 0x0BE6) for i, c in enumerate(chr(base + k) for k in range(10))}


def norm_digits(t: str) -> str:
    return "".join(DIGITS.get(ch, ch) for ch in t)


def script_ratio(t: str, lang: str) -> float:
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return 0.0
    if lang == "en":
        return sum(c.isascii() for c in letters) / len(letters)
    lo, hi = RANGES[lang]
    return sum(lo <= ord(c) <= hi for c in letters) / len(letters)


def f1(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    ca, cb = {}, {}
    for x in a:
        ca[x] = ca.get(x, 0) + 1
    for x in b:
        cb[x] = cb.get(x, 0) + 1
    ov = sum(min(ca[k], cb.get(k, 0)) for k in ca)
    if not ov:
        return 0.0
    p, r = ov / len(a), ov / len(b)
    return 2 * p * r / (p + r)


def toks(t: str, lang: str) -> list[str]:
    t = norm_digits(t).lower()
    if lang == "en":
        return re.findall(r"[a-z0-9.]+", t)
    s = re.sub(r"[\s\W_]+", "", t)
    return [s[i:i + 2] for i in range(max(1, len(s) - 1))]  # char bigrams for Indic scripts


def call(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/voice/translate", data=json.dumps(body).encode(), method="POST", headers={"X-Jarvis": "1", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4720)
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "translation_results.json"))
    args = ap.parse_args()
    cases = json.loads((ROOT / "benchmarks" / "translation_cases.json").read_text(encoding="utf-8"))["cases"]
    rows = []
    for c in cases:
        t0 = time.time()
        try:
            r = call(args.port, {"text": c["text"], "source_language": c["src"], "target_language": c["tgt"]})
            out = r.get("translated_text") or ""
            err = ""
        except Exception as e:  # noqa: BLE001
            r, out, err = {}, "", str(e)[:100]
        keep_ok = all(norm_digits(k).lower() in norm_digits(out).lower() for k in c["keep"])
        row = {
            "id": c["id"], "dir": f"{c['src']}->{c['tgt']}", "source": c["text"], "reference": c["ref"], "output": out, "error": err, "seconds": round(time.time() - t0, 1),
            "script_ratio": round(script_ratio(out, c["tgt"]), 2), "script_ok": script_ratio(out, c["tgt"]) >= (0.9 if c["tgt"] == "en" else 0.6),
            "protected_ok": keep_ok, "f1_vs_reference": round(f1(toks(out, c["tgt"]), toks(c["ref"], c["tgt"])), 2),
            "extra_text": len(out) > 3 * len(c["ref"]) + 20, "verification": (r.get("verification") or {}),
        }
        rows.append(row)
        print(c["id"], row["dir"], "script" if row["script_ok"] else "SCRIPT?", "keep" if keep_ok else "KEEP?", row["f1_vs_reference"], "|", out[:60].replace("\n", " "), flush=True)
    n = len(rows)
    summ = {
        "n": n, "errors": sum(bool(r["error"]) for r in rows), "script_ok_pct": round(100 * sum(r["script_ok"] for r in rows) / n, 1),
        "protected_tokens_ok_pct": round(100 * sum(r["protected_ok"] for r in rows) / n, 1),
        "mean_f1_vs_reference": round(sum(r["f1_vs_reference"] for r in rows) / n, 2), "extra_text_cases": sum(r["extra_text"] for r in rows),
        "by_direction_mean_f1": {d: round(sum(r["f1_vs_reference"] for r in rows if r["dir"] == d) / max(1, sum(1 for r in rows if r["dir"] == d)), 2) for d in sorted({r["dir"] for r in rows})},
    }
    Path(args.out).write_text(json.dumps({"summary": summ, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
