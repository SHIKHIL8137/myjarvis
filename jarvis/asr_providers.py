"""Provider-independent ASR abstraction and local adapters for Faster-Whisper,
Malayalam-specialized Whisper (sajilck/whisper-small-malayalam-v2), and IndicConformer."""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
import importlib.util
import logging
from pathlib import Path
import threading
import time

import numpy as np

from .config import ROOT

log = logging.getLogger("jarvis.asr")
SR = 16000


@dataclass
class ASROptions:
    language: str = "auto"
    quality: str = "balanced"
    task: str = "transcribe"
    beam_size: int | None = None
    best_of: int | None = None
    temperature: float | list[float] | None = None
    condition_on_previous_text: bool | None = None
    vad_filter: bool = True
    vad_parameters: dict | None = None
    hotwords: str | None = None
    initial_prompt: str | None = None
    word_timestamps: bool = False
    multilingual: bool | None = None
    model_name: str | None = None
    compute_type: str = "int8"
    cpu_threads: int = 6
    extra: dict = field(default_factory=dict)


class ASRProvider:
    """Provider-independent local Automatic Speech Recognition interface."""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    def metadata(self) -> dict:
        return {"provider": self.name, "available": self.is_available()}

    def unload(self, model_name: str | None = None) -> None:
        return None

    def transcribe(self, audio: np.ndarray, options: ASROptions | dict | None = None) -> dict:
        raise NotImplementedError


def _coerce_options(options: ASROptions | dict | None) -> ASROptions:
    if isinstance(options, ASROptions):
        return options
    if isinstance(options, dict):
        known = {f for f in ASROptions.__dataclass_fields__ if f != "extra"}
        base = {k: v for k, v in options.items() if k in known}
        extra = {k: v for k, v in options.items() if k not in known}
        return ASROptions(**base, extra=extra)
    return ASROptions()


def _segments_metrics(segments: list) -> tuple[float, float | None, float | None, float | None]:
    if not segments:
        return 0.0, None, None, None
    weights = [max(1, len(getattr(s, "tokens", []) or [1])) for s in segments]
    total_w = sum(weights)
    avg_logprob = sum(float(getattr(s, "avg_logprob", -1.0)) * w for s, w in zip(segments, weights)) / total_w
    no_speech = max(float(getattr(s, "no_speech_prob", 0.0)) for s in segments)
    comp_ratio = max(float(getattr(s, "compression_ratio", 1.0)) for s in segments)
    import math
    prob = math.exp(max(-5.0, min(0.0, avg_logprob)))
    conf = max(0.0, min(1.0, prob * (1.0 - 0.5 * no_speech)))
    return round(conf, 3), round(avg_logprob, 3), round(no_speech, 3), round(comp_ratio, 2)


class FasterWhisperProvider(ASRProvider):
    """Local CPU int8 Faster-Whisper provider with bounded LRU model cache and lazy loading."""

    name = "faster_whisper"

    def __init__(self, download_root: Path | None = None, max_cached: int = 2):
        self.download_root = Path(download_root or (ROOT / "models" / "whisper"))
        self.max_cached = max(1, int(max_cached))
        self._cache: collections.OrderedDict[tuple[str, str, int], object] = collections.OrderedDict()
        self._lock = threading.Lock()

    def discover_local_models(self) -> list[str]:
        found: list[str] = []
        if self.download_root.exists():
            for p in sorted(self.download_root.glob("models--*--faster-whisper-*")):
                name = p.name.split("faster-whisper-", 1)[-1]
                if any((p / "snapshots").glob("*/*")):
                    found.append(name)
            # Also support direct CTranslate2 directories placed under models/whisper/ or models/asr/
            for p in sorted(self.download_root.iterdir()):
                if p.is_dir() and (p / "model.bin").exists() and p.name not in found:
                    found.append(p.name)
        return found

    def _resolve_model_target(self, model_name: str) -> str:
        # Allow direct CT2 directories inside ROOT/models/asr or ROOT/models/whisper
        for root_dir in (ROOT / "models" / "asr", self.download_root):
            cand = root_dir / model_name
            if cand.is_dir() and (cand / "model.bin").exists():
                return str(cand)
        p = Path(model_name)
        if p.is_dir() and (p / "model.bin").exists():
            return str(p)
        return model_name

    def is_available(self) -> bool:
        return bool(self.discover_local_models()) and importlib.util.find_spec("faster_whisper") is not None

    def load(self, model_name: str, *, compute_type: str = "int8", cpu_threads: int = 6):
        key = (model_name, compute_type, int(cpu_threads))
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            from faster_whisper import WhisperModel
            target = self._resolve_model_target(model_name)
            t0 = time.perf_counter()
            try:
                m = WhisperModel(
                    target,
                    device="cpu",
                    compute_type=compute_type,
                    cpu_threads=int(cpu_threads),
                    download_root=str(self.download_root),
                    local_files_only=True,
                )
            except Exception as e:
                avail = self.discover_local_models()
                raise RuntimeError(
                    f"Whisper model {model_name!r} is not available locally. "
                    f"Installed: {avail or ['none']}. ({e})"
                ) from e
            # Warm up once cleanly for consistent interactive latency
            m.transcribe(np.zeros(SR, dtype=np.float32))
            self._cache[key] = m
            while len(self._cache) > self.max_cached:
                evicted_key, _ = self._cache.popitem(last=False)
                log.info("unloaded idle faster-whisper model %s to respect RAM cap", evicted_key[0])
            log.info("loaded faster-whisper %s in %.2fs", model_name, time.perf_counter() - t0)
            return m

    def unload(self, model_name: str | None = None) -> None:
        with self._lock:
            if model_name is None:
                self._cache.clear()
                return
            for k in [k for k in self._cache if k[0] == model_name]:
                del self._cache[k]

    def metadata(self) -> dict:
        return {
            "provider": self.name,
            "available": self.is_available(),
            "models": self.discover_local_models(),
            "device": "cpu",
            "compute_type": "int8",
            "license": "MIT",
        }

    def transcribe(self, audio: np.ndarray, options: ASROptions | dict | None = None) -> dict:
        opt = _coerce_options(options)
        model_name = opt.model_name or "base"
        m = self.load(model_name, compute_type=opt.compute_type, cpu_threads=opt.cpu_threads)
        is_multi = bool(getattr(getattr(m, "model", None), "is_multilingual", not model_name.endswith(".en")))
        req_lang = (opt.language or "auto").strip().lower()

        whisper_lang: str | None = None
        if not is_multi:
            if req_lang not in ("auto", "en", ""):
                raise ValueError(
                    f"Whisper model {model_name!r} only supports English; switch STT model to 'small' or 'base' for {req_lang}."
                )
            if opt.task == "translate":
                raise ValueError(f"Whisper model {model_name!r} is English-only and cannot translate non-English speech.")
            whisper_lang = "en"
        else:
            whisper_lang = None if req_lang in ("auto", "") else req_lang

        kwargs: dict = {
            "language": whisper_lang,
            "task": opt.task,
            "beam_size": opt.beam_size if opt.beam_size is not None else 3,
            "best_of": opt.best_of if opt.best_of is not None else 3,
            "temperature": opt.temperature if opt.temperature is not None else [0.0, 0.2],
            "condition_on_previous_text": bool(opt.condition_on_previous_text) if opt.condition_on_previous_text is not None else False,
            "vad_filter": bool(opt.vad_filter),
            "vad_parameters": opt.vad_parameters if opt.vad_filter else None,
            "hotwords": opt.hotwords,
            "initial_prompt": opt.initial_prompt,
            "word_timestamps": bool(opt.word_timestamps),
            "no_speech_threshold": float(opt.extra.get("no_speech_threshold", 0.65)),
            "compression_ratio_threshold": float(opt.extra.get("compression_ratio_threshold", 2.4)),
            "log_prob_threshold": float(opt.extra.get("log_prob_threshold", -1.0)),
        }
        use_segment_multi = bool(opt.multilingual) if opt.multilingual is not None else (is_multi and whisper_lang is None)
        if use_segment_multi:
            kwargs["multilingual"] = True

        t0 = time.perf_counter()
        segs_it, info = m.transcribe(audio, **kwargs)
        segs = list(segs_it)
        elapsed = round(time.perf_counter() - t0, 3)

        text = " ".join(s.text.strip() for s in segs if getattr(s, "text", "").strip()).strip()
        conf, avg_lp, no_sp, comp_r = _segments_metrics(segs)
        detected_lang = getattr(info, "language", None) or (whisper_lang or "en")
        lang_prob = round(float(getattr(info, "language_probability", 0.0) or 0.0), 3)
        dur = round(float(getattr(info, "duration", len(audio) / SR) or 0.0), 3)
        dur_vad = round(float(getattr(info, "duration_after_vad", dur) or 0.0), 3)

        words = []
        if opt.word_timestamps:
            for s in segs:
                for w in (getattr(s, "words", None) or []):
                    words.append({
                        "word": getattr(w, "word", ""),
                        "start": round(float(getattr(w, "start", 0.0)), 3),
                        "end": round(float(getattr(w, "end", 0.0)), 3),
                        "probability": round(float(getattr(w, "probability", 0.0)), 3),
                    })

        return {
            "provider": self.name,
            "model": model_name,
            "text": text,
            "detected_language": detected_lang,
            "language_probability": lang_prob,
            "confidence": conf,
            "avg_logprob": avg_lp,
            "no_speech_prob": no_sp,
            "compression_ratio": comp_r,
            "duration_s": dur,
            "duration_after_vad_s": dur_vad,
            "segments": [
                {
                    "start": round(float(getattr(s, "start", 0.0)), 3),
                    "end": round(float(getattr(s, "end", 0.0)), 3),
                    "text": getattr(s, "text", "").strip(),
                    "avg_logprob": round(float(getattr(s, "avg_logprob", -1.0)), 3),
                    "no_speech_prob": round(float(getattr(s, "no_speech_prob", 0.0)), 3),
                }
                for s in segs
            ],
            "words": words,
            "seconds": round(elapsed, 2),
        }


class MalayalamASRProvider(ASRProvider):
    """Adapter for specialized local Malayalam Whisper (sajilck/whisper-small-malayalam-v2).
    Supports both CTranslate2 INT8 directories (preferred on CPU, no PyTorch needed) and local HF Transformers pipelines.
    """

    name = "malayalam_whisper"
    REPO_ID = "sajilck/whisper-small-malayalam-v2"
    LICENSE = "Apache-2.0"

    def __init__(self, model_dir: Path | None = None, ct2_dir: Path | None = None):
        self.ct2_dir = Path(ct2_dir or (ROOT / "models" / "asr" / "whisper-small-malayalam-v2-ct2"))
        self.hf_dir = Path(model_dir or (ROOT / "models" / "asr" / "whisper-small-malayalam-v2"))
        self._ct2_model = None
        self._hf_pipe = None
        self._lock = threading.Lock()

    def _has_ct2(self) -> bool:
        return self.ct2_dir.is_dir() and (self.ct2_dir / "model.bin").exists()

    def _has_hf(self) -> bool:
        has_weights = self.hf_dir.is_dir() and any(
            (self.hf_dir / fname).exists() for fname in ("model.safetensors", "pytorch_model.bin")
        )
        return (
            has_weights
            and importlib.util.find_spec("transformers") is not None
            and importlib.util.find_spec("torch") is not None
        )

    def is_available(self) -> bool:
        return self._has_ct2() or self._has_hf()

    def metadata(self) -> dict:
        backend = "ctranslate2_int8" if self._has_ct2() else ("transformers" if self._has_hf() else "not_installed")
        return {
            "provider": self.name,
            "model_id": self.REPO_ID,
            "architecture": "Whisper Small (244M encoder-decoder)",
            "license": self.LICENSE,
            "languages": ["ml"],
            "cpu_compatible": True,
            "backend": backend,
            "available": self.is_available(),
            "ct2_path": str(self.ct2_dir),
            "hf_path": str(self.hf_dir),
            "source_url": f"https://huggingface.co/{self.REPO_ID}",
        }

    def unload(self, model_name: str | None = None) -> None:
        with self._lock:
            self._ct2_model = None
            self._hf_pipe = None

    def transcribe(self, audio: np.ndarray, options: ASROptions | dict | None = None) -> dict:
        opt = _coerce_options(options)
        if not self.is_available():
            raise RuntimeError(
                f"MalayalamASRProvider ({self.REPO_ID}) is not installed locally under {self.ct2_dir} or {self.hf_dir}."
            )
        t0 = time.perf_counter()
        if self._has_ct2():
            with self._lock:
                if self._ct2_model is None:
                    from faster_whisper import WhisperModel
                    self._ct2_model = WhisperModel(
                        str(self.ct2_dir),
                        device="cpu",
                        compute_type=opt.compute_type,
                        cpu_threads=opt.cpu_threads,
                        local_files_only=True,
                    )
            assert self._ct2_model is not None
            segs_it, info = self._ct2_model.transcribe(
                audio,
                language="ml",
                task="transcribe",
                beam_size=opt.beam_size or 5,
                best_of=opt.best_of or 5,
                vad_filter=bool(opt.vad_filter),
                vad_parameters=opt.vad_parameters if opt.vad_filter else None,
            )
            segs = list(segs_it)
            text = " ".join(s.text.strip() for s in segs if s.text.strip()).strip()
            conf, avg_lp, no_sp, comp_r = _segments_metrics(segs)
            dur = round(float(getattr(info, "duration", len(audio) / SR) or 0.0), 3)
            dur_vad = round(float(getattr(info, "duration_after_vad", dur) or 0.0), 3)
            return {
                "provider": self.name,
                "model": "whisper-small-malayalam-v2",
                "text": text,
                "detected_language": "ml",
                "language_probability": 1.0,
                "confidence": conf,
                "avg_logprob": avg_lp,
                "no_speech_prob": no_sp,
                "compression_ratio": comp_r,
                "duration_s": dur,
                "duration_after_vad_s": dur_vad,
                "segments": [],
                "words": [],
                "seconds": round(time.perf_counter() - t0, 2),
            }

        with self._lock:
            if self._hf_pipe is None:
                from transformers import pipeline
                self._hf_pipe = pipeline(
                    "automatic-speech-recognition",
                    model=str(self.hf_dir),
                    device="cpu",
                )
        assert self._hf_pipe is not None
        out = self._hf_pipe(
            {"raw": np.asarray(audio, dtype=np.float32), "sampling_rate": SR},
            generate_kwargs={"language": "malayalam", "task": "transcribe", "num_beams": opt.beam_size or 3},
        )
        text = str((out or {}).get("text") or "").strip()
        dur = round(len(audio) / float(SR), 3)
        return {
            "provider": self.name,
            "model": "whisper-small-malayalam-v2",
            "text": text,
            "detected_language": "ml",
            "language_probability": 0.95,
            "confidence": 0.72 if text else 0.0,
            "avg_logprob": None,
            "no_speech_prob": None,
            "compression_ratio": None,
            "duration_s": dur,
            "duration_after_vad_s": dur,
            "segments": [],
            "words": [],
            "seconds": round(time.perf_counter() - t0, 2),
        }


class IndicConformerProvider(ASRProvider):
    """Adapter for AI4Bharat IndicConformer Malayalam local ONNX / sherpa-onnx checkpoints (MIT)."""

    name = "indic_conformer"
    REPO_ID = "ai4bharat/indicconformer_stt_ml_hybrid_ctc_rnnt_large"
    LICENSE = "MIT"

    def __init__(self, model_dir: Path | None = None):
        self.model_dir = Path(model_dir or (ROOT / "models" / "asr" / "indicconformer-ml"))
        self._recognizer = None
        self._lock = threading.Lock()

    def _model_files(self) -> tuple[Path | None, Path | None]:
        if not self.model_dir.is_dir():
            return None, None
        onnx_files = sorted(self.model_dir.glob("*.onnx"))
        tokens = self.model_dir / "tokens.txt"
        if not tokens.exists():
            alt = sorted(self.model_dir.glob("*tokens*.txt"))
            tokens = alt[0] if alt else tokens
        return (onnx_files[0] if onnx_files else None), (tokens if tokens.exists() else None)

    def is_available(self) -> bool:
        m, tok = self._model_files()
        return m is not None and tok is not None and importlib.util.find_spec("sherpa_onnx") is not None

    def metadata(self) -> dict:
        m, tok = self._model_files()
        return {
            "provider": self.name,
            "model_id": self.REPO_ID,
            "architecture": "Conformer-Large Hybrid CTC/RNN-T (~120M params)",
            "license": self.LICENSE,
            "languages": ["ml"],
            "cpu_compatible": True,
            "runtime": "sherpa_onnx" if importlib.util.find_spec("sherpa_onnx") else "sherpa_onnx_missing",
            "available": self.is_available(),
            "model_file": str(m) if m else None,
            "tokens_file": str(tok) if tok else None,
            "source_url": f"https://huggingface.co/{self.REPO_ID}",
        }

    def unload(self, model_name: str | None = None) -> None:
        with self._lock:
            self._recognizer = None

    def transcribe(self, audio: np.ndarray, options: ASROptions | dict | None = None) -> dict:
        opt = _coerce_options(options)
        if not self.is_available():
            raise RuntimeError(
                f"IndicConformerProvider requires local ONNX+tokens under {self.model_dir} and `sherpa-onnx`."
            )
        m, tok = self._model_files()
        with self._lock:
            if self._recognizer is None:
                import sherpa_onnx
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                    model=str(m),
                    tokens=str(tok),
                    num_threads=max(1, min(4, int(opt.cpu_threads))),
                    provider="cpu",
                )
        assert self._recognizer is not None
        t0 = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SR, np.asarray(audio, dtype=np.float32))
        self._recognizer.decode_stream(stream)
        text = str(getattr(stream.result, "text", "") or "").strip()
        dur = round(len(audio) / float(SR), 3)
        return {
            "provider": self.name,
            "model": "indicconformer-ml",
            "text": text,
            "detected_language": "ml",
            "language_probability": 1.0,
            "confidence": 0.78 if text else 0.0,
            "avg_logprob": None,
            "no_speech_prob": None,
            "compression_ratio": None,
            "duration_s": dur,
            "duration_after_vad_s": dur,
            "segments": [],
            "words": [],
            "seconds": round(time.perf_counter() - t0, 2),
        }

