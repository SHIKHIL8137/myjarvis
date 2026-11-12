"""Multilingual transcript post-correction, guarded code-switched translation, semantic verification, and DB persistence."""
from __future__ import annotations

import collections
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
import time
import unicodedata
import uuid

from .config import DATA, TranslationSpec

log = logging.getLogger("jarvis.translator")

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ml": "Malayalam",
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ar": "Arabic",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "mixed": "Malayalam-English mixed",
}

_NUMBER_RE = re.compile(r"\d+(?:[.,:/-]\d+)*")
_TECH_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9._+-]{2,}\b")
_NEGATION_RE = re.compile(
    r"(?:\b(?:no|not|never|none|nothing|neither|nor|without|don't|dont|doesn't|didn't|won't|can't|cannot|shouldn't|couldn't|isn't|aren't|wasn't|weren't)\b"
    r"|ഇല്ല|അല്ല|വേണ്ട|അരുത്|കഴിയില്ല|പറ്റില്ല|കൂടാ|യില്ല|नहीं|मत|ना|இல்லை|வேண்டாம்|மாட்ட)",
    re.I,
)
_DATETIME_ANCHOR_RE = re.compile(
    r"(?:\b\d{1,2}:\d{2}(?:\s?(?:am|pm))?\b|\b\d{1,2}\s?(?:am|pm)\b"
    r"|\b(?:today|tomorrow|yesterday|tonight|morning|afternoon|evening|night)\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|ഇന്ന്|നാളെ|ഇന്നലെ|രാവിലെ|വൈകിട്ട്|രാത്രി)",
    re.I,
)
_QUESTION_START_RE = re.compile(
    r"^(?:who|what|when|where|why|how|which|whose|whom|is|are|was|were|do|does|did|can|could|will|would|should|have|has)\b"
    r"|^(?:എന്ത്|എന്താണ്|എപ്പോൾ|എവിടെ|എങ്ങനെ|ആര്|ഏത്)",
    re.I,
)
_CHATTER_PREFIXES = (
    "here is",
    "here's",
    "sure,",
    "certainly",
    "i fixed",
    "i have corrected",
    "the corrected",
    "note:",
    "explanation:",
)
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "at", "for", "and", "or", "it", "its", "this", "that", "with",
    "from", "by", "as", "my", "your", "our", "me", "you", "please",
}
_RISKY_VOICE_CMD_RE = re.compile(
    r"\b(?:delete|remove|erase|overwrite|modify|move|rename|create|write|run|execute|confirm|approve|click|type|"
    r"open\s+(?:(?:the|my|this|that)\s+)?(?:[A-Za-z0-9_.-]+\s+){0,3}(?:url|file|folder|directory|project|app)|"
    r"(?:find|read|inspect|search)\s+(?:(?:the|my|this|that)\s+)?(?:folder|directory|project)|"
    r"focus\s+window|screenshot|shutdown|restart|cancel)\b",
    re.I,
)


def transcript_requires_voice_clarification(rec: "TranscriptRecord", prompt_text: str) -> str | None:
    """Return a user-facing clarification message if an uncertain/low-confidence voice transcript tries to run an action."""
    text = (prompt_text or "").strip()
    if not text or not _RISKY_VOICE_CMD_RE.search(text):
        return None
    low_conf = rec.confidence is not None and float(rec.confidence) < 0.45
    uncertain_tr = bool(rec.translation_verification and rec.translation_verification.get("uncertain"))
    unsure = (
        rec.status == "uncertain"
        or low_conf
        or (rec.review_needed and (rec.recognition_quality == "low" or uncertain_tr))
    )
    if not unsure:
        return None
    return (
        f"I heard '{text}', but voice confidence or translation verification is uncertain. "
        "Please repeat clearly or confirm in text before I run that action."
    )
_CROSS_TIME_EQUIV: dict[str, set[str]] = {
    "today": {"today", "ഇന്ന്", "आज", "இன்று"},
    "tomorrow": {"tomorrow", "നാളെ", "कल", "நாளை"},
    "yesterday": {"yesterday", "ഇന്നലെ", "कल", "நேற்று"},
}
_SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "ml": (0x0D00, 0x0D7F),
    "hi": (0x0900, 0x097F),
    "ta": (0x0B80, 0x0BFF),
    "te": (0x0C00, 0x0C7F),
    "kn": (0x0C80, 0x0CFF),
}


@dataclass
class TranscriptRecord:
    """Immutable original transcript paired with editable correction, translation, diagnostics, and provenance."""

    id: str
    audio_id: str | None
    original_transcript: str
    corrected_transcript: str
    detected_language: str
    language_probability: float | None
    confidence: float | None
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None
    translated_text: str | None = None
    target_language: str = "none"
    translation_engine: str | None = None
    model: str = ""
    seconds: float = 0.0
    created_at: float = field(default_factory=time.time)
    source_language: str = "unknown"
    recognition_model: str = ""
    recognition_quality: str = "medium"
    review_needed: bool = False
    confidence_available: bool = True
    language_probability_available: bool = True
    translation_provider: str | None = None
    status: str = "completed"
    quality_flags: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    provider_status: dict = field(default_factory=dict)
    provider_errors: list[dict] = field(default_factory=list)
    selection_reason: str = ""
    selected_candidate: dict = field(default_factory=dict)
    audio_diagnostics: dict = field(default_factory=dict)
    translation_verification: dict | None = None
    eval_consent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_llm_noise(text: str) -> str:
    """Remove accidental markdown fences, role prefixes, or wrapping quotes without altering content."""
    s = text.strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    for prefix in ("Corrected:", "Correction:", "Translation:", "Translated:", "Output:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :].strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def _count_script_chars(text: str, lang: str) -> tuple[int, int]:
    """Return (target_script_letters, total_letters) cleanly across Unicode categories."""
    lo, hi = _SCRIPT_RANGES.get(lang, (0, -1))
    total = 0
    matched = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        cp = ord(ch)
        if lang == "en":
            if "LATIN" in unicodedata.name(ch, ""):
                matched += 1
        elif lo <= cp <= hi:
            matched += 1
    return matched, total


def _extract_tech_and_entities(text: str, *, include_initial_cap: bool = False) -> list[str]:
    """Extract technical identifiers, mixed-case tokens, acronyms, dotted names, and proper nouns."""
    out: list[str] = []
    seen: set[str] = set()
    for idx, raw_tok in enumerate(text.split()):
        tok = raw_tok.strip(".,!?;:\"'()[]{}")
        if len(tok) < 2:
            continue
        is_tech = (
            ("_" in tok or "." in tok or "/" in tok or "\\" in tok or ":" in tok or "::" in tok)
            or (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok))
            or (sum(1 for c in tok if c.isupper()) >= 2)
            or (any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok))
        )
        is_proper = bool(tok[0].isupper() and (idx > 0 or include_initial_cap))
        if is_tech or is_proper:
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                out.append(tok)
    return out


def _content_words(text: str) -> list[str]:
    words = re.findall(r"[^\W_]{2,}", text.lower(), flags=re.UNICODE)
    return [w for w in words if w not in _STOPWORDS]


def _word_covered(word: str, target_words: list[str], target_squashed: str) -> bool:
    if word in target_words or word in target_squashed:
        return True
    return any(SequenceMatcher(None, word, tw).ratio() >= 0.72 for tw in target_words)


def guard_correction(raw: str, candidate: str) -> tuple[str, bool, list[str]]:
    """Reject hallucinated, conversational, paraphrased, or meaning-altering LLM transcript edits."""
    flags: list[str] = []
    raw_clean = raw.strip()
    cand_raw = (candidate or "").strip()
    cand_clean = _strip_llm_noise(candidate)
    if not raw_clean:
        return "", True, flags
    if not cand_clean:
        return raw_clean, False, ["correction_rejected_empty"]

    # Reject conversational LLM chatter / commentary / multi-paragraph explanations
    cand_low = cand_clean.lower()
    cand_raw_low = cand_raw.lower()
    if "\n\n" in cand_raw or any(cand_low.startswith(p) or cand_raw_low.startswith(p) for p in _CHATTER_PREFIXES):
        flags.append("correction_rejected_llm_chatter")
        return raw_clean, False, flags

    # Reject large length expansion/shrinkage
    if len(cand_clean) > max(int(len(raw_clean) * 1.45), len(raw_clean) + 28):
        flags.append("correction_rejected_expansion")
        return raw_clean, False, flags
    if len(cand_clean) < max(1, int(len(raw_clean) * 0.55)):
        flags.append("correction_rejected_truncation")
        return raw_clean, False, flags

    # Require every numeric sequence in the raw transcript to survive intact
    raw_nums = _NUMBER_RE.findall(raw_clean)
    cand_nums = _NUMBER_RE.findall(cand_clean)
    if raw_nums and raw_nums != cand_nums:
        flags.append("correction_rejected_number_mismatch")
        return raw_clean, False, flags

    # Require dates / times / day anchors to survive intact
    raw_dt = [m.lower().strip() for m in _DATETIME_ANCHOR_RE.findall(raw_clean)]
    cand_dt = [m.lower().strip() for m in _DATETIME_ANCHOR_RE.findall(cand_clean)]
    if raw_dt != cand_dt:
        flags.append("correction_rejected_datetime_mismatch")
        return raw_clean, False, flags

    # Require technical identifiers & proper names to remain present (allowing spacing/casing normalization)
    cand_squashed = re.sub(r"[\s._-]+", "", cand_low)
    raw_squashed = re.sub(r"[\s._-]+", "", raw_clean.lower())
    for ent in _extract_tech_and_entities(raw_clean, include_initial_cap=False):
        ent_sq = re.sub(r"[\s._-]+", "", ent.lower())
        if ent.lower() not in cand_low and ent_sq not in cand_squashed:
            flags.append("correction_rejected_entity_mismatch")
            return raw_clean, False, flags

    # Reject negation loss/flip or local negation-scope shift
    raw_neg_matches = list(_NEGATION_RE.finditer(raw_clean))
    cand_neg_matches = list(_NEGATION_RE.finditer(cand_clean))
    if len(raw_neg_matches) != len(cand_neg_matches):
        flags.append("correction_rejected_negation_change")
        return raw_clean, False, flags
    for rm, cm in zip(raw_neg_matches, cand_neg_matches):
        r_ctx = _content_words(raw_clean[rm.end() : rm.end() + 32])[:2]
        c_ctx = _content_words(cand_clean[cm.end() : cm.end() + 32])[:3]
        if r_ctx and c_ctx and not any(_word_covered(rw, c_ctx, "".join(c_ctx)) for rw in r_ctx):
            flags.append("correction_rejected_negation_change")
            return raw_clean, False, flags

    # Reject question/command/intent flip
    raw_q = raw_clean.endswith("?")
    cand_q = cand_clean.endswith("?")
    raw_interr = bool(_QUESTION_START_RE.match(raw_clean))
    cand_interr = bool(_QUESTION_START_RE.match(cand_clean))
    if (raw_q and not cand_q and not cand_interr) or (not raw_q and not raw_interr and cand_q):
        flags.append("correction_rejected_intent_change")
        return raw_clean, False, flags

    # Detect meaningful word drop or hallucinated word insertion
    raw_cw = _content_words(raw_clean)
    cand_cw = _content_words(cand_clean)
    dropped = [w for w in raw_cw if not _word_covered(w, cand_cw, cand_squashed)]
    invented = [w for w in cand_cw if not _word_covered(w, raw_cw, raw_squashed)]
    if len(dropped) >= 2 or len(invented) >= 2 or (len(raw_cw) <= 4 and (dropped or invented)):
        flags.append("correction_rejected_word_drift")
        return raw_clean, False, flags

    # Reject unnecessary cosmetic-only paraphrases that rewrite sentence structure
    if len(raw_clean) >= 12 and SequenceMatcher(None, raw_clean.lower(), cand_low).ratio() < 0.72:
        flags.append("correction_rejected_paraphrase")
        return raw_clean, False, flags

    return cand_clean, True, flags


def verify_translation(source: str, translated: str, source_lang: str, target_lang: str) -> dict:
    """Deterministically check structural/anchor cues between source and translation.
    Heuristic verification only: never guarantees full semantic equivalence."""
    src = source.strip()
    tr = translated.strip()
    warnings: list[str] = []
    note = "Structural anchor check only (numbers, negation, intent, technical terms, target script); not proof of semantic accuracy."
    if not src:
        return {
            "passed": True,
            "uncertain": False,
            "warnings": [],
            "score": 1.0,
            "structural_check": True,
            "semantic_equivalence_proven": False,
            "check_method": "heuristic_anchors",
            "note": note,
        }
    if not tr:
        return {
            "passed": False,
            "uncertain": True,
            "warnings": ["empty_translation"],
            "score": 0.0,
            "structural_check": True,
            "semantic_equivalence_proven": False,
            "check_method": "heuristic_anchors",
            "note": note,
        }

    # 1. Numbers must be preserved
    src_nums = _NUMBER_RE.findall(src)
    tr_nums = _NUMBER_RE.findall(tr)
    for num in src_nums:
        if num not in tr_nums:
            warnings.append(f"missing_number:{num}")

    # 1b. Explicit time/day anchors preserved (with safe cross-lingual day table)
    src_dt = [m.lower().strip() for m in _DATETIME_ANCHOR_RE.findall(src)]
    tr_low = tr.lower()
    for anchor in src_dt:
        if ":" in anchor or "am" in anchor or "pm" in anchor:
            digits = _NUMBER_RE.findall(anchor)
            if digits and not all(d in tr_nums or d in tr_low for d in digits):
                warnings.append(f"missing_time:{anchor}")
        elif anchor in _CROSS_TIME_EQUIV and source_lang != target_lang:
            if not any(eq in tr_low for eq in _CROSS_TIME_EQUIV[anchor]):
                warnings.append(f"missing_time_anchor:{anchor}")

    # 2. Negation parity check (covering EN + Malayalam/Hindi/Tamil negation particles)
    src_neg = bool(_NEGATION_RE.search(src))
    tr_neg = bool(_NEGATION_RE.search(tr))
    if src_neg != tr_neg:
        warnings.append("negation_mismatch")

    # 3. Question / command intent parity
    src_q = ("?" in src) or bool(_QUESTION_START_RE.match(src))
    tr_q = ("?" in tr) or bool(_QUESTION_START_RE.match(tr)) or any(
        tr.rstrip(".! ").endswith(sfx) for sfx in ("ണോ", "യോ", "മോ", "ല്ലേ", "ണ്ടോ")
    )
    if ("?" in src) != ("?" in tr) or (src_q != tr_q):
        warnings.append("question_tone_mismatch")

    # 4. Target script sanity & untranslated echo detection
    if target_lang in ("en", "ml", "hi", "ta", "te", "kn"):
        matched, total = _count_script_chars(tr, target_lang)
        if total >= 6 and matched == 0:
            warnings.append(f"target_script_missing:{target_lang}")
        elif target_lang != "en" and total >= 10 and (matched / max(1, total)) < 0.25:
            warnings.append(f"low_target_script_ratio:{target_lang}")
    if (
        source_lang != target_lang
        and target_lang not in ("none", "mixed", source_lang)
        and len(src) >= 12
        and tr_low == src.lower()
    ):
        warnings.append("untranslated_copy")

    # 5. Technical identifiers & proper names preserved cleanly across code-switched / target outputs
    for term in _extract_tech_and_entities(src, include_initial_cap=False):
        if target_lang == "en" or any(c.isdigit() or c in "._-/" for c in term) or (
            sum(1 for c in term if c.isupper()) >= 2
        ) or any(c.isupper() for c in term[1:]):
            if term.lower() not in tr_low:
                warnings.append(f"missing_term:{term}")

    # 6. Extreme expansion / truncation anomaly
    ratio = len(tr) / max(1, len(src))
    if len(src) >= 12 and (ratio > 2.8 or ratio < 0.25):
        warnings.append("length_ratio_anomaly")
    elif ratio > 4.0 or ratio < 0.2:
        warnings.append("length_ratio_anomaly")

    warnings = list(dict.fromkeys(warnings))
    score = round(max(0.0, 1.0 - 0.25 * len(warnings)), 3)
    uncertain = len(warnings) > 0
    critical = any(
        w.startswith((
            "empty_translation",
            "missing_number",
            "negation_mismatch",
            "target_script_missing",
            "untranslated_copy",
        ))
        for w in warnings
    )
    return {
        "passed": not critical,
        "uncertain": uncertain,
        "warnings": warnings,
        "score": score,
        "structural_check": True,
        "semantic_equivalence_proven": False,
        "check_method": "heuristic_anchors",
        "note": note,
    }


class TranscriptStore:
    """In-memory LRU store backed optionally by SQLite/PostgreSQL VoiceTranscriptRow."""

    def __init__(self, maxlen: int = 200, db_session_factory=None):
        self._items: collections.OrderedDict[str, TranscriptRecord] = collections.OrderedDict()
        self._maxlen = maxlen
        self._db_session_factory = db_session_factory
        if db_session_factory is not None:
            self._load_recent_from_db()

    def attach_db(self, db_session_factory) -> None:
        self._db_session_factory = db_session_factory
        self._load_recent_from_db()

    def _row_to_record(self, row) -> TranscriptRecord:
        try:
            meta = json.loads(getattr(row, "meta_json", "{}") or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        raw_conf = meta["confidence"] if "confidence" in meta else getattr(row, "confidence", None)
        raw_lp_prob = meta["language_probability"] if "language_probability" in meta else getattr(row, "language_probability", None)
        return TranscriptRecord(
            id=row.id,
            audio_id=row.audio_id,
            original_transcript=row.original_transcript,
            corrected_transcript=row.corrected_transcript,
            detected_language=row.detected_language,
            language_probability=float(raw_lp_prob) if raw_lp_prob is not None else None,
            confidence=float(raw_conf) if raw_conf is not None else None,
            avg_logprob=float(meta["avg_logprob"]) if meta.get("avg_logprob") is not None else None,
            no_speech_prob=float(meta["no_speech_prob"]) if meta.get("no_speech_prob") is not None else None,
            compression_ratio=float(meta["compression_ratio"]) if meta.get("compression_ratio") is not None else None,
            translated_text=row.translated_text,
            target_language=row.target_language,
            translation_engine=row.translation_engine,
            model=row.model,
            seconds=float(row.seconds or 0.0),
            created_at=float(row.created_at or 0.0),
            source_language=str(getattr(row, "source_language", row.detected_language) or row.detected_language),
            recognition_model=str(meta.get("recognition_model", row.model) or row.model),
            recognition_quality=str(getattr(row, "recognition_quality", "medium") or "medium"),
            review_needed=bool(meta.get("review_needed", False)),
            confidence_available=bool(meta.get("confidence_available", raw_conf is not None)),
            language_probability_available=bool(meta.get("language_probability_available", raw_lp_prob is not None)),
            translation_provider=meta.get("translation_provider") or row.translation_engine,
            status=str(meta.get("status", "completed")),
            quality_flags=list(meta.get("quality_flags", [])),
            attempts=list(meta.get("attempts", [])),
            provider_status=dict(meta.get("provider_status", {})),
            provider_errors=list(meta.get("provider_errors", [])),
            selection_reason=str(meta.get("selection_reason", "")),
            selected_candidate=dict(meta.get("selected_candidate", {})),
            audio_diagnostics=dict(meta.get("audio_diagnostics", {})),
            translation_verification=meta.get("translation_verification"),
            eval_consent=bool(meta.get("eval_consent", False)),
        )

    def _persist_row(self, rec: TranscriptRecord) -> None:
        if self._db_session_factory is None:
            return
        from .db.models import VoiceTranscriptRow

        meta = {
            "confidence": rec.confidence,
            "language_probability": rec.language_probability,
            "confidence_available": rec.confidence_available,
            "language_probability_available": rec.language_probability_available,
            "avg_logprob": rec.avg_logprob,
            "no_speech_prob": rec.no_speech_prob,
            "compression_ratio": rec.compression_ratio,
            "recognition_model": rec.recognition_model,
            "review_needed": rec.review_needed,
            "translation_provider": rec.translation_provider,
            "status": rec.status,
            "quality_flags": rec.quality_flags,
            "attempts": rec.attempts,
            "provider_status": rec.provider_status,
            "provider_errors": rec.provider_errors,
            "selection_reason": rec.selection_reason,
            "selected_candidate": rec.selected_candidate,
            "audio_diagnostics": rec.audio_diagnostics,
            "translation_verification": rec.translation_verification,
            "eval_consent": rec.eval_consent,
        }
        try:
            with self._db_session_factory() as s:
                row = s.get(VoiceTranscriptRow, rec.id)
                if row is None:
                    row = VoiceTranscriptRow(
                        id=rec.id,
                        audio_id=rec.audio_id,
                        original_transcript=rec.original_transcript,
                        corrected_transcript=rec.corrected_transcript,
                        detected_language=rec.detected_language,
                        source_language=rec.source_language,
                        language_probability=float(rec.language_probability or 0.0),
                        recognition_quality=rec.recognition_quality,
                        target_language=rec.target_language,
                        translated_text=rec.translated_text,
                        translation_engine=rec.translation_engine,
                        confidence=float(rec.confidence or 0.0),
                        model=rec.model,
                        seconds=rec.seconds,
                        meta_json=json.dumps(meta, ensure_ascii=False),
                        created_at=rec.created_at,
                    )
                    s.add(row)
                else:
                    # original_transcript and audio_id remain immutable after creation
                    row.corrected_transcript = rec.corrected_transcript
                    row.target_language = rec.target_language
                    row.translated_text = rec.translated_text
                    row.translation_engine = rec.translation_engine
                    row.meta_json = json.dumps(meta, ensure_ascii=False)
                s.commit()
        except Exception as e:
            log.debug("transcript DB persist skipped: %s", e)

    def _load_recent_from_db(self) -> None:
        if self._db_session_factory is None:
            return
        from .db.models import VoiceTranscriptRow

        try:
            with self._db_session_factory() as s:
                rows = (
                    s.query(VoiceTranscriptRow)
                    .order_by(VoiceTranscriptRow.created_at.desc())
                    .limit(self._maxlen)
                    .all()
                )
            for row in reversed(rows):
                rec = self._row_to_record(row)
                self._items[rec.id] = rec
        except Exception as e:
            log.debug("transcript DB load skipped: %s", e)

    def put(self, rec: TranscriptRecord) -> TranscriptRecord:
        if rec.id in self._items:
            existing = self._items[rec.id]
            # Preserve immutable original transcript once stored
            rec.original_transcript = existing.original_transcript
            self._items.move_to_end(rec.id)
        self._items[rec.id] = rec
        while len(self._items) > self._maxlen:
            self._items.popitem(last=False)
        self._persist_row(rec)
        return rec

    def get(self, rec_id: str) -> TranscriptRecord | None:
        rec = self._items.get(rec_id)
        if rec is not None or self._db_session_factory is None:
            return rec
        from .db.models import VoiceTranscriptRow

        try:
            with self._db_session_factory() as s:
                row = s.get(VoiceTranscriptRow, rec_id)
                if row is not None:
                    rec = self._row_to_record(row)
                    self._items[rec.id] = rec
                    return rec
        except Exception:
            pass
        return None

    def latest(self) -> TranscriptRecord | None:
        if not self._items:
            return None
        k = next(reversed(self._items))
        return self._items[k]

    def list_recent(self, limit: int = 20) -> list[dict]:
        items = list(reversed(self._items.values()))[:limit]
        return [r.to_dict() for r in items]

    def delete(self, rec_id: str) -> bool:
        removed = self._items.pop(rec_id, None) is not None
        if self._db_session_factory is not None:
            from .db.models import VoiceTranscriptRow

            try:
                with self._db_session_factory() as s:
                    row = s.get(VoiceTranscriptRow, rec_id)
                    if row is not None:
                        s.delete(row)
                        s.commit()
                        removed = True
            except Exception as e:
                log.debug("transcript DB delete skipped: %s", e)
        return removed

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        if self._db_session_factory is not None:
            from .db.models import VoiceTranscriptRow

            try:
                with self._db_session_factory() as s:
                    deleted = s.query(VoiceTranscriptRow).delete()
                    s.commit()
                    count = max(count, int(deleted or 0))
            except Exception as e:
                log.debug("transcript DB clear skipped: %s", e)
        return count


def export_consented_sample(rec: TranscriptRecord, export_dir: str | Path, audio_wav_bytes: bytes | None = None) -> Path:
    """Save a user-approved corrected transcript & optional audio strictly for opt-in evaluation."""
    if not rec.eval_consent:
        raise ValueError("Cannot export evaluation sample without explicit user consent")
    root = Path(export_dir)
    if not root.is_absolute():
        root = DATA.parent / root
    root.mkdir(parents=True, exist_ok=True)
    audio_file: str | None = None
    if audio_wav_bytes:
        wav_path = root / f"{rec.id}.wav"
        wav_path.write_bytes(audio_wav_bytes)
        audio_file = wav_path.name
    meta_path = root / f"{rec.id}.json"
    payload = {
        "id": rec.id,
        "audio_id": rec.audio_id,
        "audio_file": audio_file,
        "detected_language": rec.detected_language,
        "source_language": rec.source_language,
        "original_transcript": rec.original_transcript,
        "corrected_transcript": rec.corrected_transcript,
        "translated_text": rec.translated_text,
        "target_language": rec.target_language,
        "model": rec.model,
        "confidence": rec.confidence,
        "created_at": rec.created_at,
    }
    meta_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta_path


class Translator:
    """Keeps STT transcription separated from optional guarded correction and semantic-verified translation."""

    def __init__(self, stt, brain=None, db_session_factory=None):
        self.stt = stt
        self.brain = brain
        self.records = TranscriptStore(db_session_factory=db_session_factory)

    async def _call_ollama(self, system: str, prompt: str, temperature: float = 0.05) -> str:
        if self.brain is None:
            raise RuntimeError("Local Ollama brain is not attached for translation/correction.")
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if hasattr(self.brain, "stream_raw"):
            stream_it = self.brain.stream_raw(msgs, num_ctx=2048, temperature=temperature)
        elif hasattr(self.brain, "stream"):
            stream_it = self.brain.stream(msgs)
        elif hasattr(self.brain, "complete"):
            return _strip_llm_noise(await self.brain.complete(msgs))
        else:
            raise RuntimeError("Attached brain does not support stream or complete")
        out: list[str] = []
        async for tok in stream_it:
            out.append(tok)
        return _strip_llm_noise("".join(out))

    async def correct_text(
        self,
        text: str,
        language: str = "en",
        vocab_hints: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Conservative ASR correction via Qwen3 4B guarded against hallucinations and meaning shifts."""
        raw = text.strip()
        if not raw or self.brain is None:
            return raw, []
        lang_name = LANGUAGE_NAMES.get(language, language)
        hints_line = ""
        if vocab_hints:
            hints_line = f"Known vocabulary/names (use exact casing/spelling only if matching the spoken phrase): {', '.join(vocab_hints[:40])}\n"
        system = (
            f"You are a strict speech-to-text copy editor for {lang_name} speech (which may include English technical words). "
            "Fix ONLY clear spelling errors, punctuation, capitalization, or misheard vocabulary names. "
            "NEVER paraphrase, NEVER translate, NEVER invent missing words, NEVER change numbers, negations, or intent. "
            "If uncertain, return the input text UNCHANGED. Return only the cleaned transcript."
        )
        prompt = f"{hints_line}Raw transcript:\n{raw}\n\nCorrected transcript:"
        try:
            cand = await self._call_ollama(system, prompt, temperature=0.05)
        except Exception as e:
            log.warning("transcript correction unavailable: %s", e)
            return raw, ["correction_unavailable"]
        cleaned, ok, flags = guard_correction(raw, cand)
        return (cleaned if ok else raw), flags

    async def translate_text_with_verification(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        *,
        verify: bool = True,
        preserve_code_switching: bool = True,
    ) -> tuple[str, dict]:
        """Translate text via Qwen3 4B with code-switching awareness and deterministic verification."""
        if not text.strip():
            return "", {"passed": True, "uncertain": False, "warnings": [], "score": 1.0}
        if not target_lang or target_lang == "none":
            return text, {"passed": True, "uncertain": False, "warnings": [], "score": 1.0}
        src_name = LANGUAGE_NAMES.get(source_lang, source_lang or "detected language")
        tgt_name = LANGUAGE_NAMES.get(target_lang, target_lang)
        if source_lang == target_lang and target_lang != "mixed":
            return text, {"passed": True, "uncertain": False, "warnings": [], "score": 1.0}

        cs_rule = (
            "The input may mix Malayalam/Hindi/Tamil with English words (code-switching); understand the mixed sentence naturally and keep proper names, brands, and technical terms accurate. "
            if preserve_code_switching
            else ""
        )
        system = (
            f"You are an accurate, faithful translator from {src_name} into {tgt_name}. "
            f"{cs_rule}"
            "Preserve the exact meaning, numbers, dates, negations, entities, and sentence intent (question vs command vs statement). "
            "Do NOT summarize, embellish, or invent details. Output ONLY the translation."
        )
        translated = await self._call_ollama(system, f"Text to translate:\n{text}\n\n{tgt_name} translation:", temperature=0.1)
        if not verify:
            return translated, {"passed": True, "uncertain": False, "warnings": [], "score": 1.0}

        report = verify_translation(text, translated, source_lang, target_lang)
        if not report["passed"] or report["warnings"]:
            repair_hint = ", ".join(report["warnings"])
            repair_prompt = (
                f"Previous translation had verification issues ({repair_hint}). Re-translate strictly preserving numbers, negations, technical terms, and {tgt_name} script.\n"
                f"Source text:\n{text}\n\nCorrected {tgt_name} translation:"
            )
            try:
                retry_tr = await self._call_ollama(system, repair_prompt, temperature=0.05)
                retry_report = verify_translation(text, retry_tr, source_lang, target_lang)
                if retry_report["score"] > report["score"]:
                    translated, report = retry_tr, retry_report
            except Exception:
                pass
        return translated, report

    async def translate_text(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        *,
        verify: bool = True,
        preserve_code_switching: bool = True,
    ) -> str:
        """Translate text via Qwen3 4B and return the verified translation string."""
        translated, _ = await self.translate_text_with_verification(
            text,
            source_lang,
            target_lang,
            verify=verify,
            preserve_code_switching=preserve_code_switching,
        )
        return translated

    async def process_stt(
        self,
        stt_res: dict,
        cfg: TranslationSpec,
        audio=None,
        target_override: str | None = None,
    ) -> TranscriptRecord:
        """Build a TranscriptRecord with optional guarded correction & verified translation."""
        rec_id = str(stt_res.get("id") or stt_res.get("audio_id") or uuid.uuid4().hex[:16])
        existing_rec = self.records.get(rec_id)
        raw_text = str(stt_res.get("text", "") or "").strip()
        lang = str(stt_res.get("detected_language", stt_res.get("language", "unknown")) or "unknown")
        raw_lang_prob = stt_res.get("language_probability")
        lang_prob = float(raw_lang_prob) if raw_lang_prob is not None else None
        raw_conf = stt_res.get("confidence")
        conf = float(raw_conf) if raw_conf is not None else None
        flags = list(stt_res.get("quality_flags", []))
        target = (target_override if target_override is not None else cfg.target_language) or "none"

        # 1. Optional conservative LLM post-correction (never alters original_transcript)
        corrected = raw_text
        if raw_text and bool(getattr(cfg, "auto_correct", False) or getattr(cfg, "auto_correct_transcript", False)) and self.brain is not None:
            hints = getattr(self.stt, "vocab_hints", None)
            corrected, corr_flags = await self.correct_text(raw_text, language=lang, vocab_hints=hints)
            flags.extend(corr_flags)

        # 2. Optional explicit translation
        translated: str | None = None
        engine: str | None = None
        verification: dict | None = None

        should_translate = (
            bool(raw_text)
            and target != "none"
            and (target != lang or lang == "mixed")
            and (getattr(cfg, "mode", "transcribe_only") == "translate" or target_override is not None)
        )
        if should_translate:
            if (
                target == "en"
                and lang not in ("en", "mixed")
                and bool(getattr(cfg, "prefer_whisper_for_english", False))
                and audio is not None
                and raw_text == corrected
            ):
                try:
                    w_tr = self.stt.transcribe(
                        audio,
                        task="translate",
                        language=(lang if lang not in ("unknown", "auto") else None),
                        save_audio=False,
                    )
                    w_txt = str(w_tr.get("text", "") or "").strip()
                    if w_txt:
                        w_ver = verify_translation(corrected, w_txt, lang, "en") if bool(getattr(cfg, "verify_translations", True)) else None
                        if w_ver is None or w_ver.get("passed", True) or self.brain is None:
                            translated = w_txt
                            engine = "whisper"
                            verification = w_ver
                except Exception as e:
                    log.warning("whisper translate failed, falling back to qwen: %s", e)

            if translated is None and self.brain is not None:
                try:
                    translated, verification = await self.translate_text_with_verification(
                        corrected,
                        lang,
                        target,
                        verify=bool(getattr(cfg, "verify_translations", True)),
                        preserve_code_switching=bool(getattr(cfg, "preserve_mixed_code_switching", True)),
                    )
                    engine = "qwen"
                except Exception as e:
                    log.warning("qwen translation failed: %s", e)
                    flags.append(f"translation_error:{e}")

        # Never overwrite an earlier valid translation when a retry translation attempt errors out
        if (
            translated is None
            and should_translate
            and existing_rec is not None
            and existing_rec.translated_text
            and existing_rec.target_language == target
            and any(f.startswith("translation_error:") for f in flags)
        ):
            translated = existing_rec.translated_text
            engine = existing_rec.translation_engine
            prev_v = dict(existing_rec.translation_verification or {})
            prev_w = list(prev_v.get("warnings") or [])
            if "translation_retry_failed_kept_previous" not in prev_w:
                prev_w.append("translation_retry_failed_kept_previous")
            verification = {
                **prev_v,
                "uncertain": True,
                "warnings": prev_w,
                "structural_check": True,
                "semantic_equivalence_proven": False,
                "previous_translation_preserved": True,
            }

        rec_quality = str(stt_res.get("recognition_quality") or stt_res.get("quality") or "medium")
        provider_status = dict(stt_res.get("provider_status") or {})
        provider_errors = list(stt_res.get("provider_errors") or [])
        translation_uncertain = bool(verification and verification.get("uncertain"))
        review_needed = bool(
            stt_res.get("review_needed", False)
            or rec_quality != "high"
            or "provider_unavailable" in flags
            or (provider_status and provider_status.get("available") is False)
            or translation_uncertain
            or bool(provider_errors)
        )

        if not raw_text:
            status = "empty"
        elif (
            "uncertain_transcript" in flags
            or rec_quality == "low"
            or translation_uncertain
            or bool(provider_errors)
        ):
            status = "uncertain"
        else:
            status = "completed"

        model_name = str(stt_res.get("model", "") or "")
        avg_lp = stt_res.get("avg_logprob")
        no_sp = stt_res.get("no_speech_prob")
        comp_r = stt_res.get("compression_ratio")
        rec = TranscriptRecord(
            id=rec_id,
            audio_id=stt_res.get("audio_id"),
            original_transcript=raw_text,
            corrected_transcript=corrected,
            detected_language=lang,
            source_language=str(stt_res.get("source_language", lang) or lang),
            language_probability=lang_prob,
            confidence=conf,
            confidence_available=bool(stt_res.get("confidence_available", conf is not None)),
            language_probability_available=bool(stt_res.get("language_probability_available", lang_prob is not None)),
            avg_logprob=float(avg_lp) if avg_lp is not None else None,
            no_speech_prob=float(no_sp) if no_sp is not None else None,
            compression_ratio=float(comp_r) if comp_r is not None else None,
            translated_text=translated,
            target_language=target if (should_translate or translated is not None) else "none",
            translation_engine=engine,
            translation_provider=engine,
            model=model_name,
            recognition_model=model_name,
            recognition_quality=rec_quality,
            review_needed=review_needed,
            seconds=float(stt_res.get("seconds", 0.0) or 0.0),
            status=status,
            quality_flags=flags,
            attempts=list(stt_res.get("attempts", [])),
            provider_status=provider_status,
            provider_errors=provider_errors,
            selection_reason=str(stt_res.get("selection_reason") or ""),
            selected_candidate=dict(stt_res.get("selected_candidate") or {}),
            audio_diagnostics=dict(stt_res.get("audio_diagnostics", {})),
            translation_verification=verification,
            eval_consent=False,
        )
        return self.records.put(rec)

    async def apply_correction(
        self,
        rec_id: str,
        corrected_text: str,
        target_language: str | None = None,
        eval_consent: bool | None = None,
    ) -> TranscriptRecord | None:
        """Persist a manual correction while keeping `original_transcript` completely immutable.
        Never overwrites a previously valid translation if a new translation attempt fails."""
        rec = self.records.get(rec_id)
        if rec is None:
            return None
        rec.corrected_transcript = corrected_text.strip()
        if eval_consent is not None:
            rec.eval_consent = bool(eval_consent)
        tgt = target_language or rec.target_language
        prev_translated = rec.translated_text
        prev_target = rec.target_language
        prev_engine = rec.translation_engine
        prev_provider = rec.translation_provider
        prev_ver = rec.translation_verification
        prev_valid = bool(prev_translated and (prev_ver is None or prev_ver.get("passed", True)))

        if tgt and tgt != "none" and tgt != rec.detected_language and self.brain is not None:
            try:
                new_tr, new_ver = await self.translate_text_with_verification(
                    rec.corrected_transcript,
                    rec.detected_language,
                    tgt,
                )
                if prev_valid and prev_target == tgt and (not new_tr.strip() or not new_ver.get("passed", True)):
                    rec.translated_text = prev_translated
                    rec.translation_engine = prev_engine
                    rec.translation_provider = prev_provider
                    rec.translation_verification = {
                        **(prev_ver or {}),
                        "uncertain": True,
                        "warnings": list(dict.fromkeys([*((prev_ver or {}).get("warnings") or []), "translation_retry_failed_kept_previous"])),
                        "structural_check": True,
                        "semantic_equivalence_proven": False,
                        "previous_translation_preserved": True,
                    }
                else:
                    rec.target_language = tgt
                    rec.translated_text = new_tr
                    rec.translation_verification = new_ver
                    rec.translation_engine = "qwen"
                    rec.translation_provider = "qwen"
            except Exception as e:
                log.warning("translation during apply_correction failed: %s", e)
                if prev_valid and prev_target == tgt:
                    rec.translated_text = prev_translated
                    rec.translation_engine = prev_engine
                    rec.translation_provider = prev_provider
                    rec.translation_verification = {
                        **(prev_ver or {}),
                        "uncertain": True,
                        "warnings": list(dict.fromkeys([*((prev_ver or {}).get("warnings") or []), f"translation_error:{e}", "translation_retry_failed_kept_previous"])),
                        "structural_check": True,
                        "semantic_equivalence_proven": False,
                        "previous_translation_preserved": True,
                    }
                else:
                    rec.quality_flags.append(f"translation_error:{e}")
        elif target_language == "none":
            rec.target_language = "none"
            rec.translated_text = None
            rec.translation_engine = None
            rec.translation_provider = None
            rec.translation_verification = None
        if rec.corrected_transcript != rec.original_transcript and "human_corrected" not in rec.quality_flags:
            rec.quality_flags.append("human_corrected")
        tr_uncertain = bool(rec.translation_verification and rec.translation_verification.get("uncertain"))
        if not rec.corrected_transcript:
            rec.status = "empty"
        elif tr_uncertain:
            rec.status = "uncertain"
        else:
            rec.status = "corrected"
        rec.review_needed = bool(tr_uncertain or rec.recognition_quality != "high")
        return self.records.put(rec)
