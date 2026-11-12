"""Speech-to-text orchestrator & providers:
Faster-Whisper on CPU (int8, local_files_only), optional local Malayalam/Indic ASR adapters,
audio diagnostics + configurable preprocessing profiles, external vocabulary hints,
short-utterance handling, and bounded confidence-based multi-pass retry."""
from __future__ import annotations

import collections
import io
import logging
import math
from pathlib import Path
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
    if len(cleaned) >= 18:
        # Single character spam like 'വവവവവ...' or 'ༀༀༀ...' or '1-2-3-4-4-4...'
        unique_ratio = len(set(cleaned.replace(" ", ""))) / max(1, len(cleaned.replace(" ", "")))
        if unique_ratio < 0.22:
            return True
    words = [w.lower() for w in cleaned.split() if w.strip()]
    if len(words) >= 6:
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

    if no_sp is not None and float(no_sp) >= 0.72 and text:
        flags.append("high_no_speech_prob")
        retry_reasons.append(f"high_no_speech_prob:{no_sp}")

    req_l = (requested_language or "auto").strip().lower()
    if req_l in ("auto", "") and lang_prob < 0.45 and text:
        flags.append("low_language_confidence")
        retry_reasons.append(f"low_language_probability:{lang_prob}")

    # Script consistency check: catch Devanagari/other scripts hallucinated on Malayalam/Tamil audio
    sc = _script_counts(text)
    if (req_l == "ml" or det_lang == "ml") and text:
        if sc["devanagari"] > 0 or sc["other"] > sc["malayalam"] + sc["latin"]:
            flags.append("script_mismatch")
            retry_reasons.append("malayalam_expected_non_malayalam_script")
    if (req_l == "ta" or det_lang == "ta") and text:
        if sc["devanagari"] > 0 or (sc["tamil"] == 0 and sc["latin"] == 0 and sc["other"] > 0):
            flags.append("script_mismatch")
            retry_reasons.append("tamil_expected_non_tamil_script")

    # Plausible speaking rate check (guards against spurious hallucinations on short blips)
    words = text.split()
    speech_window = max(0.25, vad_dur or dur)
    words_per_sec = len(words) / speech_window if words else 0.0
    if words and (words_per_sec > 7.5 or (dur >= 3.0 and vad_dur >= 1.5 and len(words) == 1 and words_per_sec < 0.35)):
        flags.append("implausible_word_rate")
        retry_reasons.append("implausible_word_rate")

    if "empty_transcript" in flags or "repetition_hallucination" in flags or "script_mismatch" in flags:
        tier = "low"
    elif len(retry_reasons) >= 2 or "low_avg_logprob" in flags or "high_no_speech_prob" in flags:
        tier = "low"
    elif flags:
        tier = "medium"
    else:
        tier = "high"

    return {
        "quality_tier": tier,
        "flags": flags,
        "retry_recommended": bool(retry_reasons) and has_speech_energy,
        "retry_reasons": retry_reasons,
        "has_speech_energy": has_speech_energy,
        "script_profile": sc,
        "words_per_sec": round(words_per_sec, 2),
    }


def _candidate_score(candidate: dict, evaluation: dict, requested_language: str) -> float:
    """Compare alternative transcripts without trusting raw transcript length or raw logprob alone."""
    text = (candidate.get("text") or "").strip()
    if not text:
        return -10.0
    flags = set(evaluation.get("flags", []))
    score = 2.0
    avg_lp = candidate.get("avg_logprob")
    no_sp = candidate.get("no_speech_prob")
    lang_p = float(candidate.get("language_probability") or 0.0)
    comp_r = float(candidate.get("compression_ratio") or 1.0)

    if avg_lp is not None:
        score += max(-2.5, min(0.0, float(avg_lp))) * 1.4
    if no_sp is not None:
        score -= float(no_sp) * 1.6
    score += 0.4 * lang_p

    # Strong structural penalties — never reward noisy gibberish just because it is longer
    if "repetition_hallucination" in flags or comp_r >= 2.25:
        score -= 4.5
    if "script_mismatch" in flags:
        score -= 3.5
    if "implausible_word_rate" in flags:
        score -= 2.0
    if "high_no_speech_prob" in flags:
        score -= 1.8
    if "low_language_confidence" in flags:
        score -= 0.7

    req_l = (requested_language or "auto").lower()
    sc = evaluation.get("script_profile") or {}
    if req_l == "ml" and sc.get("malayalam", 0) > 0 and sc.get("devanagari", 0) == 0:
        score += 1.2
    if req_l == "mixed" and sc.get("malayalam", 0) > 0 and sc.get("latin", 0) > 0:
        score += 1.0
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

    def _choose_special_provider(self, req_lang: str, model_override: str | None = None) -> ASRProvider | None:
        if model_override in self._providers and model_override != "faster_whisper":
            prov = self._providers[model_override]
            return prov if prov.is_available() else None
        if req_lang == "ml" and self.spec.malayalam_model:
            prov_ml = self._providers.get(self.spec.malayalam_model)
            if prov_ml and prov_ml.is_available():
                return prov_ml
        return None

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
                "quality_flags": ["no_speech_energy", *diag.warnings],
                "retry_triggered": False,
                "retry_reasons": [],
                "attempts": [],
                "audio_diagnostics": diag.to_dict(),
                "words": [],
            }

        # Check specialized Malayalam ASR provider ONLY for explicit monolingual 'ml' (never blindly on 'mixed')
        special = self._choose_special_provider(req_lang, model_override=model_override)
        if special is not None and task == "transcribe" and req_lang == "ml":
            res_spec = special.transcribe(proc_audio, ASROptions(language="ml", quality=req_quality, beam_size=preset["beam_size"], cpu_threads=self.spec.cpu_threads))
            eval_spec = evaluate_transcript_quality(res_spec, diag, requested_language=req_lang)
            return {
                **res_spec,
                "original_transcript": res_spec["text"],
                "task": task,
                "audio_id": audio_id,
                "seconds": round(time.perf_counter() - t_total, 2),
                "recognition_quality": eval_spec["quality_tier"],
                "quality_flags": eval_spec["flags"],
                "retry_triggered": False,
                "retry_reasons": [],
                "attempts": [{
                    "provider": res_spec["provider"],
                    "model": res_spec["model"],
                    "text": res_spec["text"],
                    "detected_language": res_spec["detected_language"],
                    "confidence": res_spec["confidence"],
                    "quality_tier": eval_spec["quality_tier"],
                    "flags": eval_spec["flags"],
                }],
                "audio_diagnostics": diag.to_dict(),
            }

        m = self.load(target_model) if target_model != self.spec.model else self.load()
        is_multi = bool(getattr(getattr(m, "model", None), "is_multilingual", not target_model.endswith(".en")))
        if not is_multi:
            if req_lang not in ("auto", "en", ""):
                raise ValueError(
                    f"Whisper model {target_model!r} only supports English; switch STT model to 'small' or 'base' for {req_lang}."
                )
            if task == "translate":
                raise ValueError(f"Whisper model {target_model!r} is English-only and cannot translate non-English speech.")
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
            segs, _info, primary_res = self._run_whisper_model(
                m,
                target_model,
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
                segs, _info, primary_res = self._run_whisper_model(
                    m,
                    target_model,
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
        attempts = [{
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
            "score": _candidate_score(primary_res, primary_eval, req_lang),
            "reason": "primary_pass",
        }]

        selected_res = primary_res
        selected_eval = primary_eval
        retry_triggered = False
        max_retries = int(getattr(self.spec, "max_retry_attempts", 2) or 0)
        can_retry = (
            allow_retry
            and bool(getattr(self.spec, "retry_on_low_quality", True))
            and max_retries > 0
            and task == "transcribe"
            and primary_eval["retry_recommended"]
            and model_override is None
        )

        # When caller monkeypatched self.load in tests, retry with the same loaded model instead of loading disk weights
        custom_loaded = type(m).__name__ != "WhisperModel"

        if can_retry:
            retry_triggered = True
            log.info("STT retry triggered (%s) for audio_id=%s", ", ".join(primary_eval["retry_reasons"]), audio_id)
            retry_count = 0

            # Candidate 1: try stronger model / preset (base -> small on real models, or accurate beam=5 without VAD)
            alt_model = (
                "small"
                if (not custom_loaded and target_model == "base" and "small" in self.faster_whisper.discover_local_models())
                else target_model
            )
            if retry_count < max_retries and (alt_model != target_model or req_quality != "accurate"):
                retry_count += 1
                try:
                    alt_m = m if alt_model == target_model else self.load(alt_model)
                    acc = QUALITY_PRESETS["accurate"]
                    with self._lock:
                        _s2, _i2, cand_res = self._run_whisper_model(
                            alt_m,
                            alt_model,
                            proc_audio,
                            whisper_lang=whisper_lang,
                            task=task,
                            beam_size=acc["beam_size"],
                            best_of=acc["best_of"],
                            temperature=acc["temperature"],
                            condition_on_previous_text=False,
                            enable_vad=False if is_short else use_vad,
                            vad_params=vad_params,
                            hotwords=hotwords,
                            initial_prompt=initial_prompt,
                            use_segment_multi=use_segment_multi,
                            word_timestamps=bool(getattr(self.spec, "word_timestamps", False)),
                        )
                    cand_eval = evaluate_transcript_quality(cand_res, diag, requested_language=req_lang)
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
                        "score": _candidate_score(cand_res, cand_eval, req_lang),
                        "reason": f"fallback_{alt_model}_accurate",
                    })
                    if attempts[-1]["score"] > attempts[0]["score"]:
                        selected_res, selected_eval = cand_res, cand_eval
                except Exception as e:
                    log.warning("STT retry candidate (%s) failed: %s", alt_model, e)

            # Candidate 2: if Malayalam or mixed-with-Malayalam detected/requested and specialized Malayalam provider exists locally
            if retry_count < max_retries and (
                req_lang in ("ml", "mixed") or primary_res.get("detected_language") == "ml"
            ):
                for sp in (self.malayalam_provider, self.indic_conformer):
                    if retry_count >= max_retries or not sp.is_available():
                        continue
                    retry_count += 1
                    try:
                        cand_res = sp.transcribe(proc_audio, ASROptions(language="ml", quality="accurate", cpu_threads=self.spec.cpu_threads))
                        cand_eval = evaluate_transcript_quality(cand_res, diag, requested_language=req_lang)
                        cand_score = _candidate_score(cand_res, cand_eval, req_lang)
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
                            "reason": f"fallback_{sp.name}",
                        })
                        # For mixed Malayalam-English, keep multilingual result if specialist dropped English technical tokens
                        if req_lang == "mixed" and _script_counts(selected_res["text"])["latin"] > 0 and _script_counts(cand_res["text"])["latin"] == 0:
                            continue
                        best_so_far = max(a["score"] for a in attempts[:-1])
                        if cand_score > best_so_far:
                            selected_res, selected_eval = cand_res, cand_eval
                    except Exception as e:
                        log.warning("Specialized Malayalam fallback %s failed: %s", sp.name, e)

        return {
            **selected_res,
            "original_transcript": selected_res["text"],
            "initial_pass_transcript": primary_res["text"],
            "task": task,
            "audio_id": audio_id,
            "seconds": round(time.perf_counter() - t_total, 2),
            "recognition_quality": selected_eval["quality_tier"],
            "quality_flags": selected_eval["flags"],
            "retry_triggered": retry_triggered,
            "retry_reasons": primary_eval["retry_reasons"],
            "attempts": attempts,
            "audio_diagnostics": diag.to_dict(),
        }
