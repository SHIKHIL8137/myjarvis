"""Speech-to-text orchestrator & providers:
Faster-Whisper on CPU (int8, local_files_only), optional local Malayalam/Indic ASR adapters,
audio diagnostics + configurable preprocessing profiles, external vocabulary hints,
short-utterance handling, and bounded confidence-based multi-pass retry."""
from __future__ import annotations

import collections
from difflib import SequenceMatcher
import io
import logging
import math
from pathlib import Path
import re
import threading
import time
from typing import Any
import unicodedata
import uuid
import wave

import numpy as np

from .asr_providers import (
    ASROptions,
    ASRProvider,
    FasterWhisperProvider,
    IndicConformerProvider,
    MalayalamASRProvider,
)
from .audio_preprocess import (
    PREPROCESS_PROFILES,
    TARGET_SR,
    AudioDiagnosticReport,
    bandlimited_resample,
    downmix_to_mono,
    parse_wav_raw,
    preprocess_audio,
)
from .config import ROOT, STTSpec

log = logging.getLogger("jarvis.stt")
SR = TARGET_SR

QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {"beam_size": 1, "best_of": 1, "temperature": 0.0, "condition_on_previous_text": False},
    "balanced": {"beam_size": 3, "best_of": 3, "temperature": [0.0, 0.2], "condition_on_previous_text": False},
    "accurate": {"beam_size": 5, "best_of": 5, "temperature": [0.0, 0.2, 0.4], "condition_on_previous_text": True},
}

SUPPORTED_LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "ml": "Malayalam",
    "mixed": "Mixed Malayalam & English",
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ar": "Arabic",
    "ja": "Japanese",
    "zh": "Chinese",
}


def list_local_models() -> list[str]:
    """Return Whisper/CT2 model names available locally under models/whisper/."""
    return FasterWhisperProvider().discover_local_models()


def resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int = SR, remove_dc: bool = True) -> np.ndarray:
    """Bandlimited polyphase resampling -> mono float32 in [-1, 1]."""
    if len(audio) == 0:
        return np.zeros(0, dtype=np.float32)
    mono, _ = downmix_to_mono(audio)
    a = bandlimited_resample(mono, src_sr, dst_sr)
    if remove_dc and len(a) > 16:
        a = a - float(np.mean(a))
    return np.clip(a, -1.0, 1.0).astype(np.float32)


def wav_to_float(data: bytes) -> np.ndarray:
    """Parse a PCM16 WAV (any rate/channels) -> mono float32 at 16 kHz. Raises ValueError on bad input."""
    raw, sr, _ch = parse_wav_raw(data)
    mono, _ = downmix_to_mono(raw)
    return resample_audio(mono, sr, SR)


def float_to_wav(audio: np.ndarray, sr: int = SR, channels: int = 1) -> bytes:
    """Encode float32 [-1, 1] audio as PCM16 WAV bytes (for preserving original utterance audio)."""
    a = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    if a.ndim == 2:
        ch = a.shape[1]
        pcm = (a * 32767.0).astype(np.int16).tobytes()
    else:
        ch = max(1, int(channels))
        pcm = (a * 32767.0).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm)
    return buf.getvalue()


def load_vocab_hints(spec: STTSpec | None = None, override: str | None = None) -> list[str]:
    """Load vocabulary hints from external file + config string without modifying Python source."""
    terms: list[str] = []
    seen: set[str] = set()

    def _add_chunk(raw: str):
        for piece in raw.replace("\n", ",").split(","):
            t = piece.strip()
            if not t or t.startswith("#"):
                continue
            key = t.lower()
            if key not in seen:
                seen.add(key)
                terms.append(t)

    if spec is not None and bool(getattr(spec, "vocab_hints_file", "")):
        p = Path(spec.vocab_hints_file)
        if not p.is_absolute():
            p = ROOT / p
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                _add_chunk(line)

    raw_cfg = override if override is not None else (spec.vocab_hints if spec is not None else "")
    if raw_cfg:
        _add_chunk(raw_cfg)
    return terms


def _script_counts(text: str) -> dict[str, int]:
    counts = {"latin": 0, "malayalam": 0, "devanagari": 0, "tamil": 0, "other": 0}
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
        elif unicodedata.category(ch).startswith("L"):
            counts["other"] += 1
    return counts


class AudioBufferStore:
    """Bounded in-memory + optional disk ring store preserving original utterance audio pre-preprocessing."""

    def __init__(self, maxlen: int = 32, disk_dir: Path | None = None):
        self._items: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._maxlen = maxlen
        self._disk_dir = Path(disk_dir) if disk_dir else None
        self._lock = threading.Lock()

    def set_disk_dir(self, disk_dir: Path | None) -> None:
        with self._lock:
            self._disk_dir = Path(disk_dir) if disk_dir else None

    def put(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = SR,
        channels: int = 1,
        raw_wav_bytes: bytes | None = None,
        audio_id: str | None = None,
    ) -> str:
        aid = audio_id or uuid.uuid4().hex[:12]
        raw_arr = np.asarray(audio, dtype=np.float32).copy()
        wav_bytes = raw_wav_bytes if raw_wav_bytes is not None else float_to_wav(raw_arr, sample_rate, channels)
        mono16 = resample_audio(raw_arr, sample_rate, SR, remove_dc=False) if (sample_rate != SR or raw_arr.ndim > 1) else raw_arr
        with self._lock:
            self._items[aid] = {
                "mono16": mono16,
                "raw": raw_arr,
                "sample_rate": int(sample_rate),
                "channels": int(channels),
                "wav_bytes": wav_bytes,
            }
            self._items.move_to_end(aid)
            while len(self._items) > self._maxlen:
                evicted_id, _ = self._items.popitem(last=False)
                if self._disk_dir:
                    try:
                        (self._disk_dir / f"{evicted_id}.wav").unlink(missing_ok=True)
                    except OSError:
                        pass
            if self._disk_dir:
                try:
                    self._disk_dir.mkdir(parents=True, exist_ok=True)
                    (self._disk_dir / f"{aid}.wav").write_bytes(wav_bytes)
                except OSError as e:
                    log.debug("could not persist original audio %s: %s", aid, e)
        return aid

    def get(self, audio_id: str) -> np.ndarray | None:
        with self._lock:
            item = self._items.get(audio_id)
            if item is not None:
                return item["mono16"].copy()
            d = self._disk_dir
        if d:
            p = d / f"{audio_id}.wav"
            if p.exists():
                return wav_to_float(p.read_bytes())
        return None

    def get_raw(self, audio_id: str) -> tuple[np.ndarray, int, int] | None:
        with self._lock:
            item = self._items.get(audio_id)
            if item is not None:
                return item["raw"].copy(), int(item["sample_rate"]), int(item["channels"])
            d = self._disk_dir
        if d:
            p = d / f"{audio_id}.wav"
            if p.exists():
                return parse_wav_raw(p.read_bytes())
        return None

    def get_wav_bytes(self, audio_id: str) -> bytes | None:
        with self._lock:
            item = self._items.get(audio_id)
            if item is not None:
                return item["wav_bytes"]
            d = self._disk_dir
        if d:
            p = d / f"{audio_id}.wav"
            if p.exists():
                return p.read_bytes()
        return None

    def delete(self, audio_id: str) -> bool:
        removed = False
        with self._lock:
            if audio_id in self._items:
                del self._items[audio_id]
                removed = True
            d = self._disk_dir
        if d:
            p = d / f"{audio_id}.wav"
            if p.exists():
                p.unlink(missing_ok=True)
                removed = True
        return removed

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            d = self._disk_dir
        if d and d.exists():
            for p in d.glob("*.wav"):
                n += 1
                p.unlink(missing_ok=True)
        return n


def _compute_confidence(segments: list) -> tuple[float, float | None, float | None, float | None]:
    """Compute bounded confidence [0..1] + raw Whisper decoder quality metrics."""
    if not segments:
        return 0.0, None, None, None
    weights = [max(1, len(getattr(s, "tokens", None) or [1])) for s in segments]
    total_w = sum(weights)
    avg_logprob = sum(float(s.avg_logprob) * w for s, w in zip(segments, weights)) / total_w
    no_speech = max(float(s.no_speech_prob) for s in segments)
    comp_ratio = max(float(s.compression_ratio) for s in segments)
    prob = math.exp(max(-5.0, min(0.0, avg_logprob)))
    conf = max(0.0, min(1.0, prob * (1.0 - 0.5 * no_speech)))
    return round(conf, 3), round(avg_logprob, 3), round(no_speech, 3), round(comp_ratio, 2)


def _has_repetition_loop(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    no_space = "".join(cleaned.split())
    if re.search(r"(.)\1{5,}", no_space):
        return True
    if len(no_space) >= 16:
        # Single character spam like 'വവവവവ...' or 'ༀༀༀ...' or '1-2-3-4-4-4...'
        unique_ratio = len(set(no_space)) / max(1, len(no_space))
        if unique_ratio < 0.22:
            return True
    words = [re.sub(r"[^\w]+", "", w.lower(), flags=re.UNICODE) for w in cleaned.split()]
    words = [w for w in words if w]
    # 3+ identical words in a row (e.g. "samayak samayak samayak")
    for idx in range(len(words) - 2):
        if words[idx] == words[idx + 1] == words[idx + 2]:
            return True
    # Repeated bigrams (e.g. "it is it is it is")
    if len(words) >= 6:
        bigrams = list(zip(words, words[1:]))
        if bigrams and collections.Counter(bigrams).most_common(1)[0][1] >= 3:
            return True
        uniq = len(set(words)) / len(words)
        if uniq < 0.42:
            return True
    return False


def evaluate_transcript_quality(
    result: dict,
    diag: AudioDiagnosticReport | dict | None = None,
    *,
    requested_language: str = "auto",
) -> dict:
    """Evaluate ASR quality indicators without confusing confidence or length for truth."""
    text = (result.get("text") or "").strip()
    det_lang = (result.get("detected_language") or "en").lower()
    lang_prob = float(result.get("language_probability") or 0.0)
    avg_lp = result.get("avg_logprob")
    no_sp = result.get("no_speech_prob")
    comp_r = result.get("compression_ratio")
    dur = float(result.get("duration_s") or 0.0)
    vad_dur = float(result.get("duration_after_vad_s") or 0.0)

    if isinstance(diag, AudioDiagnosticReport):
        d = diag.to_dict()
    else:
        d = dict(diag or {})

    if diag is None:
        has_speech_energy = bool(dur > 0.0 or text) and not (no_sp is not None and float(no_sp) >= 0.9 and not text)
    else:
        has_speech_energy = bool(
            (d.get("peak_amplitude", 0.0) >= 0.012 and not d.get("empty_or_too_short", False))
            and (d.get("vad_speech_duration_s", 0.0) >= 0.08 or d.get("rms_level", 0.0) >= 0.008)
        )

    flags: list[str] = []
    retry_reasons: list[str] = []

    if not text:
        flags.append("empty_transcript")
        if has_speech_energy:
            flags.append("empty_with_speech_energy")
            retry_reasons.append("empty_transcript_despite_speech_energy")

    if (comp_r is not None and float(comp_r) >= 2.25) or _has_repetition_loop(text):
        flags.append("repetition_hallucination")
        retry_reasons.append("high_compression_ratio_or_repetition")

    if avg_lp is not None and float(avg_lp) < (-0.75 if dur < 1.5 else -0.88):
        flags.append("low_avg_logprob")
        retry_reasons.append(f"low_avg_logprob:{avg_lp}")
    elif avg_lp is not None and float(avg_lp) < -0.68 and dur >= 1.5 and text:
        flags.append("borderline_confidence")

    if no_sp is not None and float(no_sp) >= 0.72 and text:
        flags.append("high_no_speech_prob")
        retry_reasons.append(f"high_no_speech_prob:{no_sp}")

    req_l = (requested_language or "auto").strip().lower()
    if req_l in ("auto", "") and lang_prob < 0.45 and text:
        flags.append("low_language_confidence")
        retry_reasons.append(f"low_language_probability:{lang_prob}")

    # Language mismatch when a specific monolingual language was explicitly requested
    if req_l not in ("auto", "mixed", "", "unknown") and det_lang not in ("", "unknown") and det_lang != req_l and text:
        flags.append("language_mismatch")
        retry_reasons.append(f"language_mismatch:{req_l}!={det_lang}")

    words = text.split()

    # Script consistency check: catch Devanagari/other wrong scripts & Latin-only hallucinations when Malayalam expected
    sc = _script_counts(text)
    if req_l == "ml" and text:
        if sc["devanagari"] > 0 or sc["other"] > sc["malayalam"] + sc["latin"]:
            flags.append("script_mismatch")
            retry_reasons.append("malayalam_expected_non_malayalam_script")
        elif sc["malayalam"] == 0 and len(words) >= 2 and sc["latin"] >= 4:
            flags.append("script_mismatch")
            retry_reasons.append("malayalam_expected_latin_only")
    elif det_lang == "ml" and text and req_l != "mixed":
        if sc["devanagari"] > 0 or sc["other"] > sc["malayalam"] + sc["latin"]:
            flags.append("script_mismatch")
            retry_reasons.append("malayalam_expected_non_malayalam_script")
    elif req_l == "mixed" and text:
        if sc["devanagari"] > 0 or sc["other"] > sc["malayalam"] + sc["latin"]:
            flags.append("script_mismatch")
            retry_reasons.append("mixed_unexpected_script")
    elif (req_l == "ta" or det_lang == "ta") and text:
        if sc["devanagari"] > 0 or (sc["tamil"] == 0 and sc["latin"] == 0 and sc["other"] > 0):
            flags.append("script_mismatch")
            retry_reasons.append("tamil_expected_non_tamil_script")
    elif req_l == "hi" and text:
        if sc["malayalam"] > 0 or sc["tamil"] > 0 or (sc["devanagari"] == 0 and sc["other"] > 0):
            flags.append("script_mismatch")
            retry_reasons.append("hindi_expected_wrong_script")

    # Suspiciously long output on very short audio (never drop valid short commands <= 3 words)
    if text and 0.0 < dur < 0.85 and (len(words) >= 8 or len(text) >= 55):
        flags.append("short_audio_long_hallucination")
        retry_reasons.append("short_audio_long_hallucination")

    # Language-aware plausible speaking rate check (guards short blips without flagging valid short commands)
    speech_window = max(0.25, vad_dur or dur)
    words_per_sec = len(words) / speech_window if words else 0.0
    max_wps = 5.8 if (req_l in ("ml", "ta", "hi") or det_lang in ("ml", "ta", "hi")) else 7.2
    is_short_command = len(words) <= 3 and dur <= 2.5
    if words and not is_short_command and (
        words_per_sec > max_wps or (dur >= 3.0 and vad_dur >= 1.5 and len(words) == 1 and words_per_sec < 0.35)
    ):
        flags.append("implausible_word_rate")
        retry_reasons.append("implausible_word_rate")

    critical_flags = {
        "empty_transcript",
        "empty_with_speech_energy",
        "repetition_hallucination",
        "script_mismatch",
        "short_audio_long_hallucination",
        "low_avg_logprob",
        "high_no_speech_prob",
    }
    if any(f in critical_flags for f in flags) or len(retry_reasons) >= 2:
        tier = "low"
    elif flags:
        tier = "medium"
    else:
        tier = "high"

    return {
        "quality_tier": tier,
        "review_needed": tier != "high",
        "flags": flags,
        "retry_recommended": bool(retry_reasons) and has_speech_energy,
        "retry_reasons": retry_reasons,
        "has_speech_energy": has_speech_energy,
        "script_profile": sc,
        "words_per_sec": round(words_per_sec, 2),
    }


def _candidate_score(
    candidate: dict,
    evaluation: dict,
    requested_language: str,
    all_candidates: list[dict] | None = None,
) -> float:
    """Compare alternative transcripts cleanly using quality tier, script match, structural penalties, and cross-pass consistency."""
    text = (candidate.get("text") or "").strip()
    if not text:
        return -10.0
    flags = set(evaluation.get("flags", []))
    tier = evaluation.get("quality_tier", "medium")
    score = 2.0 + (2.4 if tier == "high" else (0.6 if tier == "medium" else -1.6))
    avg_lp = candidate.get("avg_logprob")
    no_sp = candidate.get("no_speech_prob")
    lang_p = float(candidate.get("language_probability") or 0.0)
    comp_r = float(candidate.get("compression_ratio") or 1.0)

    if avg_lp is not None:
        score += max(-2.5, min(0.0, float(avg_lp))) * 1.1
    if no_sp is not None:
        score -= float(no_sp) * 1.6
    score += 0.35 * lang_p

    # Strong structural penalties — never reward noisy gibberish or length bias
    if "repetition_hallucination" in flags or comp_r >= 2.25:
        score -= 5.0
    if "short_audio_long_hallucination" in flags:
        score -= 5.5
    if "script_mismatch" in flags:
        score -= 4.5
    if "implausible_word_rate" in flags:
        score -= 2.5
    if "language_mismatch" in flags:
        score -= 2.0
    if "high_no_speech_prob" in flags:
        score -= 2.0
    if "low_language_confidence" in flags:
        score -= 0.8

    req_l = (requested_language or "auto").lower()
    sc = evaluation.get("script_profile") or {}
    if req_l == "ml" and sc.get("malayalam", 0) > 0 and sc.get("devanagari", 0) == 0:
        score += 1.5
    if req_l == "mixed" and sc.get("malayalam", 0) > 0 and sc.get("latin", 0) > 0 and sc.get("devanagari", 0) == 0:
        score += 1.4

    # Cross-pass consistency bonus when independent attempts agree without critical flags
    if all_candidates and tier != "low":
        norm_self = " ".join(text.lower().split())
        others = [c for c in all_candidates if c is not candidate and (c.get("text") or "").strip()]
        for other in others:
            norm_other = " ".join(str(other.get("text") or "").lower().split())
            if norm_other and SequenceMatcher(None, norm_self, norm_other).ratio() >= 0.78:
                score += 0.8
                break
    return round(score, 4)


class Whisper:
    """Unified Speech-to-Text service orchestrating audio preprocessing, provider routing,
    vocabulary hints, quality evaluation, and confidence-based retries."""

    def __init__(self, spec: STTSpec):
        self.spec = spec
        self._m = None
        self._loaded_key: tuple[str, str, int] | None = None
        self._lock = threading.Lock()
        disk_dir = Path(spec.audio_store_dir) if getattr(spec, "preserve_original_audio", True) else None
        if disk_dir and not disk_dir.is_absolute():
            disk_dir = ROOT / disk_dir
        self.audio_store = AudioBufferStore(maxlen=32, disk_dir=disk_dir)
        self.faster_whisper = FasterWhisperProvider(max_cached=getattr(spec, "max_cached_models", 2))
        self.malayalam_provider = MalayalamASRProvider()
        self.indic_conformer = IndicConformerProvider()
        self._providers: dict[str, ASRProvider] = {
            "faster_whisper": self.faster_whisper,
            "malayalam_whisper": self.malayalam_provider,
            "indic_conformer": self.indic_conformer,
        }

    def provider_inventory(self) -> list[dict]:
        return [p.metadata() for p in self._providers.values()]

    def unload_models(self, model_name: str | None = None) -> None:
        self.faster_whisper.unload(model_name)
        if model_name is None or model_name == "malayalam_whisper":
            self.malayalam_provider.unload()
        if model_name is None or model_name == "indic_conformer":
            self.indic_conformer.unload()
        with self._lock:
            self._m = None
            self._loaded_key = None

    @property
    def ready(self) -> bool:
        return any((ROOT / "models" / "whisper").glob(f"*faster-whisper-{self.spec.model}")) or any(
            (ROOT / "models" / "whisper").glob(f"*{self.spec.model}*")
        ) or any(p.is_available() for p in (self.malayalam_provider, self.indic_conformer))

    def load(self, model_name: str | None = None):
        target_model = model_name or self.spec.model
        key = (target_model, self.spec.compute_type, self.spec.cpu_threads)
        with self._lock:
            if self._m is None or self._loaded_key != key:
                self.faster_whisper.max_cached = max(1, int(getattr(self.spec, "max_cached_models", 2)))
                self._m = self.faster_whisper.load(
                    target_model,
                    compute_type=self.spec.compute_type,
                    cpu_threads=self.spec.cpu_threads,
                )
                self._loaded_key = key
        return self._m

    def _run_whisper_model(
        self,
        model_obj,
        model_label: str,
        audio: np.ndarray,
        *,
        whisper_lang: str | None,
        task: str,
        beam_size: int,
        best_of: int,
        temperature,
        condition_on_previous_text: bool,
        enable_vad: bool,
        vad_params: dict,
        hotwords: str | None,
        initial_prompt: str | None,
        use_segment_multi: bool,
        word_timestamps: bool,
    ) -> tuple[list, object, dict]:
        kwargs: dict = dict(
            language=whisper_lang,
            task=task,
            beam_size=beam_size,
            best_of=best_of,
            temperature=temperature,
            condition_on_previous_text=condition_on_previous_text,
            vad_filter=enable_vad,
            vad_parameters=vad_params if enable_vad else None,
            hotwords=hotwords,
            initial_prompt=initial_prompt,
            no_speech_threshold=0.65,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )
        if use_segment_multi:
            kwargs["multilingual"] = True
        if word_timestamps:
            kwargs["word_timestamps"] = True
        t0 = time.perf_counter()
        segs_it, info = model_obj.transcribe(audio, **kwargs)
        segs = list(segs_it)
        elapsed = round(time.perf_counter() - t0, 2)

        text = " ".join(s.text.strip() for s in segs if getattr(s, "text", "").strip()).strip()
        if text and not self.spec.preserve_punctuation:
            text = text.rstrip(".!?")

        conf, avg_lp, no_sp, comp_r = _compute_confidence(segs)
        detected_lang = getattr(info, "language", None) or (whisper_lang or "en")
        lang_prob = round(float(getattr(info, "language_probability", 0.0) or 0.0), 3)
        dur = round(float(getattr(info, "duration", len(audio) / SR) or 0.0), 2)
        dur_vad = round(float(getattr(info, "duration_after_vad", dur) or 0.0), 2)
        words = []
        if word_timestamps:
            for s in segs:
                for w in (getattr(s, "words", None) or []):
                    words.append({
                        "word": getattr(w, "word", ""),
                        "start": round(float(getattr(w, "start", 0.0)), 3),
                        "end": round(float(getattr(w, "end", 0.0)), 3),
                        "probability": round(float(getattr(w, "probability", 0.0)), 3),
                    })
        res = {
            "provider": "faster_whisper",
            "model": model_label,
            "text": text,
            "detected_language": detected_lang,
            "language_probability": lang_prob,
            "confidence": conf,
            "avg_logprob": avg_lp,
            "no_speech_prob": no_sp,
            "compression_ratio": comp_r,
            "duration_s": dur,
            "duration_after_vad_s": dur_vad,
            "words": words,
            "seconds": elapsed,
        }
        return segs, info, res

    def _resolve_special_provider(
        self, req_lang: str, model_override: str | None = None
    ) -> tuple[ASRProvider | None, str | None, str | None]:
        if model_override in self._providers and model_override != "faster_whisper":
            prov = self._providers[model_override]
            if prov.is_available():
                return prov, model_override, None
            return None, model_override, f"Local weights for specialist provider {model_override!r} are not installed."

        if req_lang == "ml":
            configured = (getattr(self.spec, "malayalam_model", "") or "").strip()
            if configured:
                if configured in self._providers and configured != "faster_whisper":
                    prov_ml = self._providers[configured]
                    if prov_ml.is_available():
                        return prov_ml, configured, None
                    return None, configured, f"Configured Malayalam provider {configured!r} is unavailable locally."
                return None, configured, f"Configured Malayalam model {configured!r} was not recognized locally."
            for auto_prov in (self.malayalam_provider, self.indic_conformer):
                if auto_prov.is_available():
                    return auto_prov, auto_prov.name, None
        return None, None, None

    def _choose_special_provider(self, req_lang: str, model_override: str | None = None) -> ASRProvider | None:
        prov, _name, _reason = self._resolve_special_provider(req_lang, model_override=model_override)
        return prov

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = SR,
        channels: int = 1,
        raw_wav_bytes: bytes | None = None,
        preprocessing_profile: str | None = None,
        language: str | None = None,
        quality: str | None = None,
        task: str = "transcribe",
        vocab_hints: str | None = None,
        vad_filter: bool | None = None,
        multilingual: bool | None = None,
        save_audio: bool = True,
        model_override: str | None = None,
        allow_retry: bool = True,
    ) -> dict:
        """Preprocess (safely) and transcribe mono/stereo audio across supported sample rates.
        Returns transcript, quality indicators, audio diagnostics, retry provenance, and preserved audio_id."""
        t_total = time.perf_counter()
        raw_arr = np.asarray(audio, dtype=np.float32)

        # Preserve genuine pre-preprocessing audio BEFORE any filtering/resampling
        audio_id = None
        if save_audio and raw_arr.size > 0:
            audio_id = self.audio_store.put(
                raw_arr,
                sample_rate=sample_rate,
                channels=channels,
                raw_wav_bytes=raw_wav_bytes,
            )

        prof = (preprocessing_profile or getattr(self.spec, "preprocessing_profile", "light") or "light").strip().lower()
        if prof not in PREPROCESS_PROFILES:
            prof = "light"

        raw_dur = (raw_arr.shape[0] if raw_arr.ndim >= 1 else 0) / float(max(1, sample_rate))
        is_short = 0.0 < raw_dur < 1.5
        pad_ms = int(getattr(self.spec, "short_utterance_pad_ms", 160) or 0) if is_short else 0
        proc_audio, diag = preprocess_audio(
            raw_arr,
            src_sr=sample_rate,
            dst_sr=SR,
            channels=channels,
            profile=prof,
            pad_short_ms=pad_ms,
        )

        target_model = (model_override or self.spec.model).strip()
        req_lang = (language if language is not None else self.spec.language or "auto").strip().lower()
        req_quality = (quality or self.spec.quality or "balanced").strip().lower()
        preset = QUALITY_PRESETS.get(req_quality, QUALITY_PRESETS["balanced"])

        # Short-circuit empty or pure-noise recordings without feeding silence into Whisper hallucination loops
        if len(proc_audio) == 0 or ("empty_recording" in diag.warnings) or (
            "mostly_silence" in diag.warnings and diag.vad_speech_duration_s < 0.05 and diag.peak_amplitude < 0.008
        ):
            return {
                "text": "",
                "original_transcript": "",
                "detected_language": "en" if req_lang in ("auto", "mixed", "") else req_lang,
                "language_probability": 0.0,
                "confidence": 0.0,
                "avg_logprob": None,
                "no_speech_prob": 1.0,
                "compression_ratio": None,
                "duration_s": diag.duration_s,
                "duration_after_vad_s": 0.0,
                "model": target_model,
                "provider": "none",
                "task": task,
                "audio_id": audio_id,
                "seconds": round(time.perf_counter() - t_total, 2),
                "recognition_quality": "low",
                "review_needed": True,
                "quality_flags": ["no_speech_energy", *diag.warnings],
                "retry_triggered": False,
                "retry_reasons": [],
                "attempts": [],
                "provider_status": {},
                "provider_errors": [],
                "audio_diagnostics": diag.to_dict(),
                "words": [],
            }

        # Unified candidate pipeline: explicit/local Malayalam specialist runs first ONLY on explicit 'ml'
        # (never blindly on 'mixed'/'auto'), and only short-circuits if high quality with zero flags.
        special, req_special_name, special_unavail_reason = self._resolve_special_provider(
            req_lang, model_override=model_override
        )
        provider_status: dict[str, Any] = {}
        provider_errors: list[dict[str, str]] = []
        extra_flags: list[str] = []
        if req_special_name:
            provider_status = {
                "requested": req_special_name,
                "available": special is not None,
                "reason": special_unavail_reason or "ready",
            }
            if special is None and special_unavail_reason:
                extra_flags.append("provider_unavailable")
                provider_errors.append({"provider": req_special_name, "error": special_unavail_reason})

        attempts: list[dict[str, Any]] = []
        candidates: list[tuple[dict, dict, str]] = []
        tried_providers: set[str] = set()
        tried_fw_configs: set[tuple] = set()

        run_special_first = (
            special is not None
            and task == "transcribe"
            and (req_lang == "ml" or (model_override in self._providers and model_override != "faster_whisper"))
        )
        if run_special_first and special is not None:
            tried_providers.add(special.name)
            try:
                res_spec = special.transcribe(
                    proc_audio,
                    ASROptions(
                        language="ml" if req_lang == "ml" else req_lang,
                        quality=req_quality,
                        beam_size=preset["beam_size"],
                        cpu_threads=self.spec.cpu_threads,
                    ),
                )
                eval_spec = evaluate_transcript_quality(res_spec, diag, requested_language=req_lang)
                score_spec = _candidate_score(res_spec, eval_spec, req_lang)
                reason_spec = f"primary_{special.name}"
                attempts.append({
                    "provider": res_spec.get("provider", special.name),
                    "model": res_spec.get("model", special.name),
                    "text": res_spec.get("text", ""),
                    "detected_language": res_spec.get("detected_language", "ml"),
                    "language_probability": res_spec.get("language_probability", 0.0),
                    "confidence": res_spec.get("confidence", 0.0),
                    "avg_logprob": res_spec.get("avg_logprob"),
                    "compression_ratio": res_spec.get("compression_ratio"),
                    "quality_tier": eval_spec["quality_tier"],
                    "flags": eval_spec["flags"],
                    "score": score_spec,
                    "reason": reason_spec,
                })
                candidates.append((res_spec, eval_spec, reason_spec))
                # Short-circuit ONLY on verified clean high quality; otherwise continue into Faster-Whisper fallback
                if (
                    eval_spec["quality_tier"] == "high"
                    and not eval_spec["flags"]
                    and bool((res_spec.get("text") or "").strip())
                ):
                    return {
                        **res_spec,
                        "original_transcript": res_spec["text"],
                        "initial_pass_transcript": res_spec["text"],
                        "task": task,
                        "audio_id": audio_id,
                        "seconds": round(time.perf_counter() - t_total, 2),
                        "recognition_quality": eval_spec["quality_tier"],
                        "review_needed": False,
                        "quality_flags": list(dict.fromkeys([*eval_spec["flags"], *extra_flags])),
                        "retry_triggered": False,
                        "retry_reasons": [],
                        "attempts": attempts,
                        "provider_status": provider_status,
                        "provider_errors": provider_errors,
                        "audio_diagnostics": diag.to_dict(),
                    }
                log.info(
                    "Malayalam provider %s returned tier=%s flags=%s; continuing to Faster-Whisper fallback",
                    special.name,
                    eval_spec["quality_tier"],
                    eval_spec["flags"],
                )
            except Exception as e:
                err_msg = str(e)
                log.warning("Specialized Malayalam provider %s failed: %s; falling back to Faster-Whisper", special.name, err_msg)
                provider_errors.append({"provider": special.name, "error": err_msg})
                attempts.append({
                    "provider": special.name,
                    "model": req_special_name or special.name,
                    "text": "",
                    "detected_language": req_lang,
                    "language_probability": 0.0,
                    "confidence": 0.0,
                    "avg_logprob": None,
                    "compression_ratio": None,
                    "quality_tier": "low",
                    "flags": ["provider_error"],
                    "error": err_msg,
                    "score": -100.0,
                    "reason": f"primary_{special.name}_error",
                })
            if int(getattr(self.spec, "max_cached_models", 2)) <= 1:
                try:
                    special.unload()
                except Exception:
                    pass

        fw_model = self.spec.model if (target_model in self._providers and target_model != "faster_whisper") else target_model
        m = self.load(fw_model) if fw_model != self.spec.model else self.load()
        is_multi = bool(getattr(getattr(m, "model", None), "is_multilingual", not fw_model.endswith(".en")))
        if not is_multi:
            if req_lang not in ("auto", "en", ""):
                raise ValueError(
                    f"Whisper model {fw_model!r} only supports English; switch STT model to 'small' or 'base' for {req_lang}."
                )
            if task == "translate":
                raise ValueError(f"Whisper model {fw_model!r} is English-only and cannot translate non-English speech.")
            whisper_lang = "en"
        elif req_lang in ("auto", "mixed", ""):
            whisper_lang = None
        else:
            whisper_lang = req_lang

        beam_size = self.spec.beam_size if self.spec.beam_size is not None else preset["beam_size"]
        best_of = self.spec.best_of if self.spec.best_of is not None else preset["best_of"]
        cond_prev = False if is_short else preset["condition_on_previous_text"]
        use_vad = self.spec.vad_filter if vad_filter is None else vad_filter

        # Guarded vocabulary hints: never inject ASCII hints into explicit Indic modes,
        # and avoid forcing prompt priors into ultra-short commands (< 0.9s).
        hint_terms = load_vocab_hints(self.spec, override=vocab_hints)
        hints = ", ".join(hint_terms)
        if len(hints) > 380:
            hints = hints[:380].rsplit(",", 1)[0].strip()
        ascii_hints = bool(hints) and all(ord(ch) < 128 for ch in hints)
        hotwords = hints if (hints and is_multi and not (ascii_hints and whisper_lang not in (None, "en"))) else None
        initial_prompt = None
        if not is_short and hints and (whisper_lang == "en" or not is_multi or (whisper_lang is not None and not ascii_hints)):
            initial_prompt = hints

        use_segment_multi = bool(multilingual) if multilingual is not None else (is_multi and whisper_lang is None)
        vad_params = {
            "threshold": self.spec.vad_threshold,
            "min_speech_duration_ms": self.spec.min_speech_duration_ms,
            "min_silence_duration_ms": self.spec.min_silence_duration_ms,
            "speech_pad_ms": self.spec.speech_pad_ms,
        }

        rms = float(np.sqrt(np.mean(proc_audio ** 2))) if len(proc_audio) else 0.0
        with self._lock:
            fw_sig = (fw_model, whisper_lang, beam_size, best_of, cond_prev, bool(use_vad), bool(hotwords))
            tried_fw_configs.add(fw_sig)
            segs, _info, primary_res = self._run_whisper_model(
                m,
                fw_model,
                proc_audio,
                whisper_lang=whisper_lang,
                task=task,
                beam_size=beam_size,
                best_of=best_of,
                temperature=preset["temperature"],
                condition_on_previous_text=cond_prev,
                enable_vad=use_vad,
                vad_params=vad_params,
                hotwords=hotwords,
                initial_prompt=initial_prompt,
                use_segment_multi=use_segment_multi,
                word_timestamps=bool(getattr(self.spec, "word_timestamps", False)),
            )
            # Short-utterance recovery preserved: if VAD dropped audible speech, retry once without VAD
            if use_vad and not segs and len(proc_audio) >= int(0.15 * SR) and rms > 0.005:
                no_vad_sig = (fw_model, whisper_lang, beam_size, best_of, cond_prev, False, bool(hotwords))
                tried_fw_configs.add(no_vad_sig)
                segs, _info, primary_res = self._run_whisper_model(
                    m,
                    fw_model,
                    proc_audio,
                    whisper_lang=whisper_lang,
                    task=task,
                    beam_size=beam_size,
                    best_of=best_of,
                    temperature=preset["temperature"],
                    condition_on_previous_text=cond_prev,
                    enable_vad=False,
                    vad_params=vad_params,
                    hotwords=hotwords,
                    initial_prompt=initial_prompt,
                    use_segment_multi=use_segment_multi,
                    word_timestamps=bool(getattr(self.spec, "word_timestamps", False)),
                )

        primary_eval = evaluate_transcript_quality(primary_res, diag, requested_language=req_lang)
        fw_reason = "fallback_faster_whisper" if candidates or provider_errors else "primary_pass"
        fw_score = _candidate_score(primary_res, primary_eval, req_lang)
        attempts.append({
            "provider": primary_res["provider"],
            "model": primary_res["model"],
            "text": primary_res["text"],
            "detected_language": primary_res["detected_language"],
            "language_probability": primary_res["language_probability"],
            "confidence": primary_res["confidence"],
            "avg_logprob": primary_res["avg_logprob"],
            "compression_ratio": primary_res["compression_ratio"],
            "quality_tier": primary_eval["quality_tier"],
            "flags": primary_eval["flags"],
            "score": fw_score,
            "reason": fw_reason,
        })
        candidates.append((primary_res, primary_eval, fw_reason))

        retry_triggered = bool(candidates and len(candidates) >= 2 or provider_errors)
        max_retries = min(2, int(getattr(self.spec, "max_retry_attempts", 2) or 0))
        can_retry = (
            allow_retry
            and bool(getattr(self.spec, "retry_on_low_quality", True))
            and max_retries > 0
            and task == "transcribe"
            and primary_eval["retry_recommended"]
            and (model_override is None or model_override in self._providers)
        )

        # When caller monkeypatched self.load in tests, retry with the same loaded model instead of loading disk weights
        custom_loaded = type(m).__name__ != "WhisperModel"

        if can_retry:
            retry_triggered = True
            log.info("STT retry triggered (%s) for audio_id=%s", ", ".join(primary_eval["retry_reasons"]), audio_id)
            retry_count = 0

            # Candidate A: try a genuinely different Faster-Whisper model / decoding configuration
            local_models = self.faster_whisper.discover_local_models()
            acc = QUALITY_PRESETS["accurate"]
            alt_specs: list[tuple[str, str | None, int, int, bool, str]] = []
            alt_model = "small" if (not custom_loaded and fw_model == "base" and "small" in local_models) else fw_model
            alt_specs.append((
                alt_model,
                whisper_lang,
                acc["beam_size"],
                acc["best_of"],
                False if is_short else use_vad,
                f"fallback_{alt_model}_accurate",
            ))
            if use_vad:
                alt_specs.append((fw_model, whisper_lang, acc["beam_size"], acc["best_of"], False, f"fallback_{fw_model}_novad"))
            if is_multi and req_lang == "mixed" and primary_res.get("detected_language") not in ("ml", "en", "mixed"):
                alt_specs.append((alt_model, "ml", acc["beam_size"], acc["best_of"], False if is_short else use_vad, "fallback_mixed_ml_hint"))
            if is_multi and whisper_lang is not None and "language_mismatch" in primary_eval["flags"]:
                alt_specs.append((alt_model, None, acc["beam_size"], acc["best_of"], False if is_short else use_vad, "fallback_auto_lang"))

            for cand_model, cand_lang, cand_beam, cand_best, cand_vad, cand_reason in alt_specs:
                if retry_count >= max_retries:
                    break
                sig = (cand_model, cand_lang, cand_beam, cand_best, False, bool(cand_vad), bool(hotwords))
                if sig in tried_fw_configs:
                    continue
                tried_fw_configs.add(sig)
                retry_count += 1
                try:
                    if cand_model != fw_model and int(getattr(self.spec, "max_cached_models", 2)) <= 1:
                        self.faster_whisper.unload(fw_model)
                    alt_m = m if cand_model == fw_model else self.load(cand_model)
                    with self._lock:
                        _s2, _i2, cand_res = self._run_whisper_model(
                            alt_m,
                            cand_model,
                            proc_audio,
                            whisper_lang=cand_lang,
                            task=task,
                            beam_size=cand_beam,
                            best_of=cand_best,
                            temperature=acc["temperature"],
                            condition_on_previous_text=False,
                            enable_vad=cand_vad,
                            vad_params=vad_params,
                            hotwords=hotwords,
                            initial_prompt=initial_prompt,
                            use_segment_multi=(bool(multilingual) if multilingual is not None else (is_multi and cand_lang is None)),
                            word_timestamps=bool(getattr(self.spec, "word_timestamps", False)),
                        )
                    cand_eval = evaluate_transcript_quality(cand_res, diag, requested_language=req_lang)
                    cand_score = _candidate_score(cand_res, cand_eval, req_lang)
                    attempts.append({
                        "provider": cand_res["provider"],
                        "model": cand_res["model"],
                        "text": cand_res["text"],
                        "detected_language": cand_res["detected_language"],
                        "language_probability": cand_res["language_probability"],
                        "confidence": cand_res["confidence"],
                        "avg_logprob": cand_res["avg_logprob"],
                        "compression_ratio": cand_res["compression_ratio"],
                        "quality_tier": cand_eval["quality_tier"],
                        "flags": cand_eval["flags"],
                        "score": cand_score,
                        "reason": cand_reason,
                    })
                    candidates.append((cand_res, cand_eval, cand_reason))
                    break
                except Exception as e:
                    err_msg = str(e)
                    log.warning("STT retry candidate (%s) failed: %s", cand_model, err_msg)
                    provider_errors.append({"provider": "faster_whisper", "error": err_msg})

            # Candidate B: optional specialist Malayalam fallback only if not yet tried and Malayalam requested/detected
            if retry_count < max_retries and (
                req_lang in ("ml", "mixed") or primary_res.get("detected_language") == "ml"
            ):
                for sp in (self.malayalam_provider, self.indic_conformer):
                    if retry_count >= max_retries or sp.name in tried_providers or not sp.is_available():
                        continue
                    tried_providers.add(sp.name)
                    retry_count += 1
                    try:
                        cand_res = sp.transcribe(
                            proc_audio,
                            ASROptions(language="ml", quality="accurate", cpu_threads=self.spec.cpu_threads),
                        )
                        cand_eval = evaluate_transcript_quality(cand_res, diag, requested_language=req_lang)
                        cand_score = _candidate_score(cand_res, cand_eval, req_lang)
                        reason_sp = f"fallback_{sp.name}"
                        attempts.append({
                            "provider": cand_res["provider"],
                            "model": cand_res["model"],
                            "text": cand_res["text"],
                            "detected_language": cand_res["detected_language"],
                            "language_probability": cand_res["language_probability"],
                            "confidence": cand_res["confidence"],
                            "avg_logprob": cand_res.get("avg_logprob"),
                            "compression_ratio": cand_res.get("compression_ratio"),
                            "quality_tier": cand_eval["quality_tier"],
                            "flags": cand_eval["flags"],
                            "score": cand_score,
                            "reason": reason_sp,
                        })
                        candidates.append((cand_res, cand_eval, reason_sp))
                    except Exception as e:
                        err_msg = str(e)
                        log.warning("Specialized Malayalam fallback %s failed: %s", sp.name, err_msg)
                        provider_errors.append({"provider": sp.name, "error": err_msg})
                        attempts.append({
                            "provider": sp.name,
                            "model": sp.name,
                            "text": "",
                            "detected_language": "ml",
                            "language_probability": 0.0,
                            "confidence": 0.0,
                            "quality_tier": "low",
                            "flags": ["provider_error"],
                            "error": err_msg,
                            "score": -100.0,
                            "reason": f"fallback_{sp.name}_error",
                        })

        # Final candidate selection with cross-pass consistency scoring (respecting mixed code-switching guard)
        cand_dicts = [c[0] for c in candidates]
        best_res, best_eval, _best_reason = candidates[0]
        best_score = -999.0
        for cand_res, cand_eval, cand_reason in candidates:
            # In mixed mode, avoid dropping technical English Latin tokens in favor of monolingual specialist output
            if (
                req_lang == "mixed"
                and cand_res.get("provider") in ("malayalam_whisper", "indic_conformer")
                and _script_counts(primary_res.get("text", ""))["latin"] > 0
                and _script_counts(cand_res.get("text", ""))["latin"] == 0
                and "script_mismatch" not in primary_eval["flags"]
            ):
                continue
            sc_val = _candidate_score(cand_res, cand_eval, req_lang, all_candidates=cand_dicts)
            for att in attempts:
                if att.get("reason") == cand_reason and att.get("text") == cand_res.get("text"):
                    att["score"] = sc_val
            if sc_val > best_score:
                best_score = sc_val
                best_res, best_eval = cand_res, cand_eval

        initial_text = (attempts[0].get("text") if attempts else primary_res.get("text")) or primary_res.get("text", "")
        combined_flags = list(dict.fromkeys([*best_eval["flags"], *extra_flags]))
        combined_retry_reasons = list(dict.fromkeys(
            reason for _r, ev, _rsn in candidates for reason in ev.get("retry_reasons", [])
        ))

        return {
            **best_res,
            "original_transcript": best_res["text"],
            "initial_pass_transcript": initial_text,
            "task": task,
            "audio_id": audio_id,
            "seconds": round(time.perf_counter() - t_total, 2),
            "recognition_quality": best_eval["quality_tier"],
            "review_needed": bool(best_eval.get("review_needed", best_eval["quality_tier"] != "high")),
            "quality_flags": combined_flags,
            "retry_triggered": retry_triggered,
            "retry_reasons": combined_retry_reasons,
            "attempts": attempts,
            "provider_status": provider_status,
            "provider_errors": provider_errors,
            "audio_diagnostics": diag.to_dict(),
        }
