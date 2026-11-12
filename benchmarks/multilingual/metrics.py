"""Unicode/Indic-safe ASR evaluation metrics (WER, CER, S/D/I rates, RAM, script profile)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import unicodedata


def normalize_for_eval(text: str) -> str:
    """Normalize text while preserving Unicode letters, Indic combining marks (Mc/Mn), chillu ZWJ/ZWNJ (Cf), and digits."""
    norm = unicodedata.normalize("NFC", (text or "").strip().lower())
    chars: list[str] = []
    for ch in norm:
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S"):
            chars.append(" ")
        elif cat == "Zs" or ch.isspace():
            chars.append(" ")
        else:
            chars.append(ch)
    return " ".join("".join(chars).split())


def tokenize_words(text: str) -> list[str]:
    norm = normalize_for_eval(text)
    return norm.split() if norm else []


def tokenize_chars(text: str) -> list[str]:
    # Collapse whitespace cleanly so CER measures character accuracy without arbitrary double-space drift
    norm = normalize_for_eval(text)
    return list(norm) if norm else []


def levenshtein_ops(ref: list[str], hyp: list[str]) -> tuple[int, int, int, int]:
    """Compute exact (edits, substitutions, deletions, insertions) between ref and hyp sequences."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return m, 0, 0, m
    if m == 0:
        return n, 0, n, 0

    # dp[i][j] = (cost, sub, del, ins)
    dp: list[list[tuple[int, int, int, int]]] = [[(0, 0, 0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)

    for i in range(1, n + 1):
        ri = ref[i - 1]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                c_sub, s_sub, d_sub, i_sub = dp[i - 1][j - 1]
                c_del, s_del, d_del, i_del = dp[i - 1][j]
                c_ins, s_ins, d_ins, i_ins = dp[i][j - 1]
                best = min(
                    (c_sub + 1, s_sub + 1, d_sub, i_sub),
                    (c_del + 1, s_del, d_del + 1, i_del),
                    (c_ins + 1, s_ins, d_ins, i_ins + 1),
                    key=lambda x: x[0],
                )
                dp[i][j] = best
    return dp[n][m]


def compute_text_metrics(ref: str, hyp: str) -> dict:
    """Compute WER, CER, substitution_rate, deletion_rate, insertion_rate for one ref/hyp pair."""
    r_words = tokenize_words(ref)
    h_words = tokenize_words(hyp)
    edits_w, sub_w, del_w, ins_w = levenshtein_ops(r_words, h_words)
    ref_w_len = max(1, len(r_words))
    wer = (edits_w / len(r_words)) if r_words else (0.0 if not h_words else 1.0)

    r_chars = tokenize_chars(ref)
    h_chars = tokenize_chars(hyp)
    edits_c, _, _, _ = levenshtein_ops(r_chars, h_chars)
    cer = (edits_c / len(r_chars)) if r_chars else (0.0 if not h_chars else 1.0)

    return {
        "wer": round(wer, 4),
        "cer": round(cer, 4),
        "ref_words": len(r_words),
        "hyp_words": len(h_words),
        "substitutions": sub_w,
        "deletions": del_w,
        "insertions": ins_w,
        "substitution_rate": round(sub_w / ref_w_len, 4),
        "deletion_rate": round(del_w / ref_w_len, 4),
        "insertion_rate": round(ins_w / ref_w_len, 4),
    }


def memory_snapshot_mb() -> dict[str, float]:
    """Return current process RSS and Peak RSS in MB (Windows K32GetProcessMemoryInfo with safe fallback)."""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return {
            "rss_mb": round(pmc.WorkingSetSize / (1024 * 1024), 1),
            "peak_rss_mb": round(pmc.PeakWorkingSetSize / (1024 * 1024), 1),
        }
    except Exception:
        return {"rss_mb": 0.0, "peak_rss_mb": 0.0}


def script_profile(text: str) -> dict[str, int]:
    """Count characters across key scripts (Latin, Malayalam, Devanagari, Tamil, digits, other)."""
    counts = {"latin": 0, "malayalam": 0, "devanagari": 0, "tamil": 0, "digits": 0, "other_alpha": 0}
    for ch in text or "":
        cp = ord(ch)
        if 0x0D00 <= cp <= 0x0D7F:
            counts["malayalam"] += 1
        elif 0x0900 <= cp <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0B80 <= cp <= 0x0BFF:
            counts["tamil"] += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            counts["latin"] += 1
        elif ch.isdigit():
            counts["digits"] += 1
        elif re.match(r"[^\W\d_]", ch, flags=re.UNICODE):
            counts["other_alpha"] += 1
    return counts

