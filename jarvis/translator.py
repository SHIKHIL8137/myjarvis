"""Translation and transcript-preservation service.
Separates speech transcription from translation and preserves:
original audio ID, original transcript, detected language, corrected transcript, translated text, and target language.
Never silently translates when transcription-only is configured."""
import asyncio
import collections
from dataclasses import asdict, dataclass
import logging
import threading
import uuid

import numpy as np

from .config import TranslationSpec
from .stt import SUPPORTED_LANGUAGES, Whisper

log = logging.getLogger("jarvis.translator")

LANG_NAMES = {
    **SUPPORTED_LANGUAGES,
    "mixed": "Mixed Malayalam and English",
}


def language_label(code: str) -> str:
    return LANG_NAMES.get((code or "").lower(), code or "unknown")


@dataclass
class TranscriptRecord:
    id: str
    audio_id: str | None
    original_transcript: str
    corrected_transcript: str
    detected_language: str
    language_probability: float
    confidence: float
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None
    translated_text: str | None
    target_language: str
    translation_engine: str | None
    model: str
    seconds: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["text"] = self.corrected_transcript
        return d


class TranscriptStore:
    """Thread-safe bounded history of recent voice transcript records."""

    def __init__(self, maxlen: int = 50):
        self._items: collections.OrderedDict[str, TranscriptRecord] = collections.OrderedDict()
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def put(self, rec: TranscriptRecord) -> TranscriptRecord:
        with self._lock:
            self._items[rec.id] = rec
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)
        return rec

    def get(self, rec_id: str) -> TranscriptRecord | None:
        with self._lock:
            return self._items.get(rec_id)

    def latest(self) -> TranscriptRecord | None:
        with self._lock:
            if not self._items:
                return None
            return next(reversed(self._items.values()))

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            vals = list(self._items.values())[-max(1, limit):]
            return [r.to_dict() for r in reversed(vals)]


class Translator:
    """Coordinates transcript preservation, optional correction, and Whisper/Qwen local translation."""

    def __init__(self, stt: Whisper, brain):
        self.stt = stt
        self.brain = brain
        self.records = TranscriptStore()

    async def translate_text(self, text: str, source_language: str, target_language: str) -> str:
        """Translate text locally via the configured Qwen brain. Preserves technical terms and names."""
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        tgt = (target_language or "none").strip().lower()
        if tgt in ("none", "", "auto"):
            return cleaned
        src = (source_language or "auto").strip().lower()
        if src == tgt:
            return cleaned

        src_name = language_label(src) if src != "auto" else "the detected source language"
        tgt_name = language_label(tgt)
        msgs = [
            {
                "role": "system",
                "content": (
                    "You are a precise local translator. Translate the user's text faithfully into the requested target language. "
                    "Preserve technical terms, product names, and proper nouns accurately. "
                    "If the input mixes two languages, translate the entire meaning naturally into the target language. "
                    "Output ONLY the translation with no explanations, quotes, or extra commentary."
                ),
            },
            {
                "role": "user",
                "content": f"Source language: {src_name}\nTarget language: {tgt_name} ({tgt})\n\nText:\n{cleaned}",
            },
        ]
        out = await self.brain.complete(msgs)
        return out.strip()

    async def correct_text_with_llm(self, text: str, language: str = "auto") -> str:
        """Lightly restore punctuation/casing without altering meaning or translating."""
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        lang_name = language_label(language) if language not in ("auto", "") else "the same language"
        msgs = [
            {
                "role": "system",
                "content": (
                    "You fix ASR transcription punctuation, capitalization, and obvious homophone errors without translating "
                    "or changing the language. Keep mixed-language words intact. Output ONLY the corrected transcript."
                ),
            },
            {"role": "user", "content": f"Language: {lang_name}\nTranscript:\n{cleaned}"},
        ]
        try:
            out = await self.brain.complete(msgs)
            return out.strip() or cleaned
        except Exception as e:
            log.warning("LLM transcript correction skipped: %s", e)
            return cleaned

    async def process_stt(
        self,
        stt_result: dict,
        spec: TranslationSpec,
        audio: np.ndarray | None = None,
        target_override: str | None = None,
    ) -> TranscriptRecord:
        """Create a TranscriptRecord from STT output, translating ONLY if explicitly requested."""
        orig = (stt_result.get("original_transcript") or stt_result.get("text") or "").strip()
        src_lang = (stt_result.get("detected_language") or "en").strip().lower()
        corrected = orig
        if orig and spec.auto_correct:
            corrected = await self.correct_text_with_llm(orig, src_lang)

        target = (target_override if target_override is not None else spec.target_language or "none").strip().lower()
        should_translate = bool(orig) and target not in ("none", "", "auto") and (
            target_override is not None or spec.mode == "translate"
        )

        translated: str | None = None
        engine: str | None = None
        if should_translate:
            if src_lang == target:
                translated = corrected
                engine = "same_language"
            elif (
                target == "en"
                and audio is not None
                and len(audio) > 0
                and spec.prefer_whisper_for_english
                and not self.stt.spec.model.endswith(".en")
            ):
                try:
                    tr = await asyncio.to_thread(
                        self.stt.transcribe,
                        audio,
                        language=src_lang,
                        task="translate",
                        save_audio=False,
                    )
                    if tr.get("text", "").strip():
                        translated = tr["text"].strip()
                        engine = "whisper"
                except Exception as e:
                    log.warning("Whisper built-in translate failed, falling back to LLM: %s", e)
            if translated is None:
                translated = await self.translate_text(corrected, src_lang, target)
                engine = "qwen"

        rec = TranscriptRecord(
            id=stt_result.get("audio_id") or uuid.uuid4().hex[:12],
            audio_id=stt_result.get("audio_id"),
            original_transcript=orig,
            corrected_transcript=corrected,
            detected_language=src_lang,
            language_probability=float(stt_result.get("language_probability") or 0.0),
            confidence=float(stt_result.get("confidence") or 0.0),
            avg_logprob=stt_result.get("avg_logprob"),
            no_speech_prob=stt_result.get("no_speech_prob"),
            compression_ratio=stt_result.get("compression_ratio"),
            translated_text=translated,
            target_language=target if should_translate else "none",
            translation_engine=engine,
            model=stt_result.get("model") or self.stt.spec.model,
            seconds=float(stt_result.get("seconds") or 0.0),
        )
        return self.records.put(rec)

    async def apply_correction(
        self,
        record_id: str,
        corrected_text: str,
        target_language: str | None = None,
    ) -> TranscriptRecord:
        """Update corrected_transcript while preserving original_transcript, and re-translate if requested."""
        rec = self.records.get(record_id)
        if rec is None:
            raise KeyError(record_id)
        new_text = (corrected_text or "").strip()
        if not new_text:
            raise ValueError("corrected_transcript cannot be empty")
        rec.corrected_transcript = new_text
        tgt = (target_language if target_language is not None else rec.target_language or "none").strip().lower()
        if tgt not in ("none", "", "auto"):
            rec.translated_text = await self.translate_text(new_text, rec.detected_language, tgt)
            rec.target_language = tgt
            rec.translation_engine = "same_language" if rec.detected_language == tgt else "qwen"
        else:
            rec.translated_text = None
            rec.target_language = "none"
            rec.translation_engine = None
        return self.records.put(rec)

