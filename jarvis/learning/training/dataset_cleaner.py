"""Dataset cleaning, deduplication, language filtering, quality scoring, and PII hygiene."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re

from ..knowledge_pipeline import detect_script_language

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass
class CleaningReport:
    dataset_id: str
    version: int
    input_count: int
    kept_count: int
    removed_duplicates: int = 0
    removed_low_quality: int = 0
    removed_language: int = 0
    language_breakdown: dict[str, int] = field(default_factory=dict)
    output_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_text(text: str) -> str:
    s = re.sub(r"\r\n?", "\n", text or "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _prompt_response_pair(rec: dict) -> tuple[str, str]:
    if "messages" in rec:
        users = [m.get("content", "") for m in rec["messages"] if m.get("role") == "user"]
        assistants = [m.get("content", "") for m in rec["messages"] if m.get("role") == "assistant"]
        return "\n".join(users), "\n".join(assistants)
    if "instruction" in rec:
        p = f"{rec.get('instruction', '')}\n{rec.get('input', '')}".strip()
        return p, str(rec.get("output", ""))
    return str(rec.get("prompt", "")), str(rec.get("response") or rec.get("completion") or "")


def _quality_score(prompt: str, response: str) -> float:
    plen, rlen = len(prompt.strip()), len(response.strip())
    if plen < 4 or rlen < 8:
        return 0.1
    words = response.split()
    if not words:
        return 0.1
    uniq_ratio = len({w.lower() for w in words}) / max(1, len(words))
    score = 0.65
    if rlen >= 30:
        score += 0.15
    if uniq_ratio >= 0.45:
        score += 0.15
    if response.rstrip()[-1:] in ".!?।\"'`)]":
        score += 0.05
    if uniq_ratio < 0.25 and len(words) > 12:
        return 0.2  # repetitive spam
    return min(1.0, round(score, 3))


def _mask_pii(text: str) -> str:
    return _EMAIL_RE.sub("[redacted-email]", text)


def _clean_record(rec: dict, mask_email: bool) -> dict:
    clean_fn = (lambda t: _mask_pii(_normalize_text(t))) if mask_email else _normalize_text
    if "messages" in rec:
        return {
            "messages": [
                {"role": m["role"], "content": clean_fn(m["content"])}
                for m in rec["messages"]
            ]
        }
    if "instruction" in rec:
        out = {
            "instruction": clean_fn(rec["instruction"]),
            "input": clean_fn(rec.get("input", "")),
            "output": clean_fn(rec["output"]),
        }
        return out
    return {
        "prompt": clean_fn(rec.get("prompt", "")),
        "response": clean_fn(rec.get("response") or rec.get("completion") or ""),
    }


class DatasetCleaner:
    """Cleans validated datasets and persists immutable cleaned version files."""

    def __init__(self, cleaned_dir: Path | None = None):
        from . import CLEANED_DIR
        self.cleaned_dir = Path(cleaned_dir) if cleaned_dir else CLEANED_DIR

    def clean(
        self,
        records: list[dict],
        *,
        dataset_id: str,
        version: int,
        allowed_languages: set[str] | None = None,
        min_quality: float = 0.55,
        mask_email: bool = True,
    ) -> tuple[CleaningReport, list[dict]]:
        seen_hashes: set[str] = set()
        kept: list[dict] = []
        rm_dup = 0
        rm_qual = 0
        rm_lang = 0
        langs: dict[str, int] = {}

        for rec in records:
            cleaned = _clean_record(rec, mask_email=mask_email)
            prompt, response = _prompt_response_pair(cleaned)
            fp_src = re.sub(r"\s+", " ", f"{prompt}||{response}".lower()).strip()
            fp = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()
            if fp in seen_hashes:
                rm_dup += 1
                continue
            seen_hashes.add(fp)

            lang = detect_script_language(f"{prompt} {response}")
            if allowed_languages and lang not in allowed_languages:
                rm_lang += 1
                continue

            q = _quality_score(prompt, response)
            if q < min_quality:
                rm_qual += 1
                continue

            langs[lang] = langs.get(lang, 0) + 1
            kept.append(cleaned)

        out_dir = self.cleaned_dir / dataset_id / f"v{version}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "records.jsonl"
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
        out_path.write_text((payload + "\n") if payload else "", encoding="utf-8")

        report = CleaningReport(
            dataset_id=dataset_id,
            version=version,
            input_count=len(records),
            kept_count=len(kept),
            removed_duplicates=rm_dup,
            removed_low_quality=rm_qual,
            removed_language=rm_lang,
            language_breakdown=langs,
            output_path=str(out_path),
        )
        return report, kept

