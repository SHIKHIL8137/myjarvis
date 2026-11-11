"""Speech-to-text: faster-whisper on CPU (int8), local_files_only.
Supports multilingual models, auto language detection, quality presets, vocabulary hints,
anti-aliased polyphase resampling, short-utterance recovery, and confidence metrics."""
import collections
import io
import logging
import math
import threading
import time
import uuid
import wave

import numpy as np
from scipy.signal import resample_poly

from .config import ROOT, STTSpec

log = logging.getLogger("jarvis.stt")
SR = 16000

QUALITY_PRESETS = {
    "fast": {"beam_size": 1, "best_of": 1, "temperature": 0.0, "condition_on_previous_text": False},
    "balanced": {"beam_size": 3, "best_of": 3, "temperature": [0.0, 0.2], "condition_on_previous_text": False},
    "accurate": {"beam_size": 5, "best_of": 5, "temperature": [0.0, 0.2, 0.4], "condition_on_previous_text": True},
}

SUPPORTED_LANGUAGES = {
    "auto": "Auto-detect",
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
    "zh": "Chinese",
}


def list_local_models() -> list[str]:
    """Return Whisper model names available locally under models/whisper/."""
    base = ROOT / "models" / "whisper"
    if not base.exists():
        return []
    found = []
    for p in sorted(base.glob("models--*--faster-whisper-*")):
        name = p.name.split("faster-whisper-", 1)[-1]
        if any((p / "snapshots").glob("*/*")):
            found.append(name)
    return found


def resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int = SR, remove_dc: bool = True) -> np.ndarray:
    """Bandlimited polyphase resampling -> mono float32 in [-1, 1]."""
    if len(audio) == 0:
        return np.zeros(0, dtype=np.float32)
    a = np.asarray(audio, dtype=np.float32)
    if src_sr != dst_sr:
        g = math.gcd(int(dst_sr), int(src_sr))
        up, down = int(dst_sr) // g, int(src_sr) // g
        a = resample_poly(a, up, down).astype(np.float32)
    if remove_dc and len(a) > 16:
        a = a - float(np.mean(a))
    return np.clip(a, -1.0, 1.0).astype(np.float32)


def wav_to_float(data: bytes) -> np.ndarray:
    """Parse a PCM16 WAV (any rate/channels) -> mono float32 at 16 kHz. Raises ValueError on bad input."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a valid WAV file: {e}") from e
    if sw != 2:
        raise ValueError("WAV must be 16-bit PCM")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return resample_audio(a, sr, SR)


def float_to_wav(audio: np.ndarray, sr: int = SR) -> bytes:
    """Encode float32 [-1, 1] mono audio as PCM16 WAV bytes (for preserving original utterance audio)."""
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class AudioBufferStore:
    """Bounded in-memory ring store preserving recent original utterance audio for re-transcription/correction."""

    def __init__(self, maxlen: int = 24):
        self._items: collections.OrderedDict[str, np.ndarray] = collections.OrderedDict()
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def put(self, audio: np.ndarray) -> str:
        aid = uuid.uuid4().hex[:12]
        copy = np.asarray(audio, dtype=np.float32).copy()
        with self._lock:
            self._items[aid] = copy
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)
        return aid

    def get(self, audio_id: str) -> np.ndarray | None:
        with self._lock:
            a = self._items.get(audio_id)
            return a.copy() if a is not None else None


def _compute_confidence(segments: list) -> tuple[float, float | None, float | None, float | None]:
    """Compute bounded confidence [0..1] + raw Whisper decoder quality metrics."""
    if not segments:
        return 0.0, None, None, None
    weights = [max(1, len(s.tokens)) for s in segments]
    total_w = sum(weights)
    avg_logprob = sum(float(s.avg_logprob) * w for s, w in zip(segments, weights)) / total_w
    no_speech = max(float(s.no_speech_prob) for s in segments)
    comp_ratio = max(float(s.compression_ratio) for s in segments)
    prob = math.exp(max(-5.0, min(0.0, avg_logprob)))
    conf = max(0.0, min(1.0, prob * (1.0 - 0.5 * no_speech)))
    return round(conf, 3), round(avg_logprob, 3), round(no_speech, 3), round(comp_ratio, 2)


class Whisper:
    def __init__(self, spec: STTSpec):
        self.spec = spec
        self._m = None
        self._loaded_key: tuple[str, str, int] | None = None
        self._lock = threading.Lock()
        self.audio_store = AudioBufferStore()

    @property
    def ready(self) -> bool:
        return any((ROOT / "models" / "whisper").glob(f"*faster-whisper-{self.spec.model}")) or any(
            (ROOT / "models" / "whisper").glob(f"*{self.spec.model}*")
        )

    def load(self, model_name: str | None = None):
        target_model = model_name or self.spec.model
        key = (target_model, self.spec.compute_type, self.spec.cpu_threads)
        with self._lock:
            if self._m is None or self._loaded_key != key:
                from faster_whisper import WhisperModel
                t = time.time()
                try:
                    self._m = WhisperModel(
                        target_model,
                        device="cpu",
                        compute_type=self.spec.compute_type,
                        cpu_threads=self.spec.cpu_threads,
                        download_root=str(ROOT / "models" / "whisper"),
                        local_files_only=True,
                    )
                except Exception as e:
                    avail = list_local_models()
                    raise RuntimeError(
                        f"Whisper model {target_model!r} is not available offline. "
                        f"Locally installed: {avail or ['none']}. ({e})"
                    ) from e
                self._m.transcribe(np.zeros(SR, dtype=np.float32))  # warm-up so the first real call is not cold
                self._loaded_key = key
                log.info("whisper %s loaded+warmed in %.1fs", target_model, time.time() - t)
        return self._m

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        quality: str | None = None,
        task: str = "transcribe",
        vocab_hints: str | None = None,
        vad_filter: bool | None = None,
        multilingual: bool | None = None,
        save_audio: bool = True,
    ) -> dict:
        """Transcribe (or Whisper-translate to English) mono float32 16 kHz audio.
        Returns text plus original_transcript, detected_language, language_probability, confidence, and quality stats."""
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if len(audio) > 16:
            audio = audio - float(np.mean(audio))
        audio_id = self.audio_store.put(audio) if save_audio and len(audio) > 0 else None

        m = self.load()
        req_lang = (language if language is not None else self.spec.language or "auto").strip().lower()
        is_multi = bool(getattr(getattr(m, "model", None), "is_multilingual", not self.spec.model.endswith(".en")))
        if not is_multi:
            if req_lang not in ("auto", "en", ""):
                raise ValueError(
                    f"Whisper model {self.spec.model!r} only supports English; switch STT model to 'small' or 'base' for {req_lang}."
                )
            if task == "translate":
                raise ValueError(f"Whisper model {self.spec.model!r} is English-only and cannot translate non-English speech.")
            whisper_lang = "en"
        else:
            whisper_lang = None if req_lang in ("auto", "") else req_lang

        preset = QUALITY_PRESETS.get((quality or self.spec.quality or "balanced").lower(), QUALITY_PRESETS["balanced"])
        beam_size = self.spec.beam_size if self.spec.beam_size is not None else preset["beam_size"]
        best_of = self.spec.best_of if self.spec.best_of is not None else preset["best_of"]
        use_vad = self.spec.vad_filter if vad_filter is None else vad_filter
        hints = (vocab_hints if vocab_hints is not None else self.spec.vocab_hints or "").strip()

        # Only apply English/Latin vocabulary hints when decoding English or auto-detecting without forcing Latin over Indic scripts.
        ascii_hints = bool(hints) and all(ord(ch) < 128 for ch in hints)
        hotwords = hints if (hints and is_multi and not (ascii_hints and whisper_lang not in (None, "en"))) else None
        initial_prompt = None
        if hints and (whisper_lang == "en" or not is_multi or (whisper_lang is not None and not ascii_hints)):
            initial_prompt = hints

        # When auto-detecting on multilingual models without fixed language, segment-level multilingual detection helps mixed speech
        use_segment_multi = bool(multilingual) if multilingual is not None else (is_multi and whisper_lang is None)

        vad_params = {
            "threshold": self.spec.vad_threshold,
            "min_speech_duration_ms": self.spec.min_speech_duration_ms,
            "min_silence_duration_ms": self.spec.min_silence_duration_ms,
            "speech_pad_ms": self.spec.speech_pad_ms,
        }

        def _run(enable_vad: bool):
            kwargs = dict(
                language=whisper_lang,
                task=task,
                beam_size=beam_size,
                best_of=best_of,
                temperature=preset["temperature"],
                condition_on_previous_text=preset["condition_on_previous_text"],
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
            segs_it, info = m.transcribe(audio, **kwargs)
            return list(segs_it), info

        t0 = time.time()
        rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
        with self._lock:
            segs, info = _run(use_vad)
            # Short-utterance recovery: if VAD clipped a very brief command that still has real energy, retry once without VAD
            if use_vad and not segs and len(audio) >= int(0.15 * SR) and rms > 0.008:
                segs, info = _run(False)

        text = " ".join(s.text.strip() for s in segs if s.text.strip()).strip()
        if text and not self.spec.preserve_punctuation:
            text = text.rstrip(".!?")

        conf, avg_lp, no_sp, comp_r = _compute_confidence(segs)
        detected_lang = getattr(info, "language", None) or (whisper_lang or "en")
        lang_prob = round(float(getattr(info, "language_probability", 0.0) or 0.0), 3)
        dur = round(float(getattr(info, "duration", len(audio) / SR) or 0.0), 2)
        dur_vad = round(float(getattr(info, "duration_after_vad", dur) or 0.0), 2)

        return {
            "text": text,
            "original_transcript": text,
            "detected_language": detected_lang,
            "language_probability": lang_prob,
            "confidence": conf,
            "avg_logprob": avg_lp,
            "no_speech_prob": no_sp,
            "compression_ratio": comp_r,
            "duration_s": dur,
            "duration_after_vad_s": dur_vad,
            "model": self.spec.model,
            "task": task,
            "audio_id": audio_id,
            "seconds": round(time.time() - t0, 2),
        }
