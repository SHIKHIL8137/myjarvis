"""Acoustic Echo Cancellation (AEC) discovery, far-end TTS reference ring, and NLMS echo verifier.

Design note (Windows + Bluetooth A2DP):
- On Windows Python 3.14, native WebRTC AudioProcessing / SpeexDSP wheels are unavailable.
- This module checks for optional native bindings first, then provides a pure-NumPy delay-estimating
  NLMS echo canceller fed with exact 16 kHz resampled TTS far-end reference audio.
- Full-duplex barge-in MUST remain gated by `verified == True` (measured ERLE >= 8.0 dB), otherwise
  VoiceLoop safely enforces half-duplex suspension to prevent Bluetooth speaker self-transcription.
"""
from dataclasses import dataclass
import logging
import threading
import numpy as np

from .stt import SR, resample_audio

log = logging.getLogger("jarvis.aec")


def discover_aec_backends() -> dict:
    """Inspect available AEC libraries on this Python/OS environment without claiming unverified capability."""
    webrtc_ok = False
    speex_ok = False
    try:
        import webrtc_audio_processing  # type: ignore # noqa: F401
        webrtc_ok = True
    except Exception:
        pass
    try:
        import speexdsp  # type: ignore # noqa: F401
        speex_ok = True
    except Exception:
        pass
    return {
        "webrtc_audio_processing": webrtc_ok,
        "speexdsp": speex_ok,
        "nlms_builtin": True,
    }


class EchoReferenceBuffer:
    """Thread-safe 16 kHz ring buffer holding recent far-end TTS playback samples."""

    def __init__(self, seconds: float = 2.0):
        self.cap = max(SR // 2, int(SR * seconds))
        self._buf = np.zeros(self.cap, dtype=np.float32)
        self._total_written = 0
        self._active_until = 0.0
        self._lock = threading.Lock()

    def push_playback(self, audio_i16: np.ndarray, src_sr: int, lead_latency_s: float = 0.0):
        if audio_i16 is None or len(audio_i16) == 0:
            return
        f32 = np.asarray(audio_i16, dtype=np.float32) / 32768.0
        ref16 = resample_audio(f32, src_sr, SR, remove_dc=False) if src_sr != SR else f32
        n = len(ref16)
        if n == 0:
            return
        dur = n / float(SR)
        import time
        now = time.monotonic()
        with self._lock:
            if n >= self.cap:
                self._buf[:] = ref16[-self.cap:]
            else:
                self._buf[:-n] = self._buf[n:]
                self._buf[-n:] = ref16
            self._total_written += n
            self._active_until = max(self._active_until, now + lead_latency_s + dur)

    def clear(self):
        with self._lock:
            self._buf.fill(0.0)
            self._active_until = 0.0

    def is_playback_recent(self, tail_s: float = 0.35) -> bool:
        import time
        with self._lock:
            return time.monotonic() <= (self._active_until + tail_s)

    def snapshot(self, samples: int | None = None) -> np.ndarray:
        with self._lock:
            if samples is None or samples >= self.cap:
                return self._buf.copy()
            return self._buf[-samples:].copy()


@dataclass
class AECStats:
    backend: str = "nlms"
    verified: bool = False
    erle_db: float = 0.0
    estimated_delay_ms: float = 0.0
    fallback_reason: str = "Full-duplex AEC unverified for current speaker/mic pair; using safe half-duplex."


class NLMSEchoCanceller:
    """Delay-compensated normalized LMS adaptive filter with ERLE health estimation."""

    def __init__(self, filter_taps: int = 256, mu: float = 0.35, min_erle_db: float = 8.0):
        self.L = filter_taps
        self.mu = mu
        self.min_erle_db = min_erle_db
        self.w = np.zeros(filter_taps, dtype=np.float32)
        self.delay_samples = 0
        self.verified = False
        self.last_erle_db = 0.0
        self._x_hist = np.zeros(filter_taps, dtype=np.float32)

    def reset(self):
        self.w.fill(0.0)
        self._x_hist.fill(0.0)
        self.last_erle_db = 0.0

    def estimate_delay(self, near_end: np.ndarray, far_end: np.ndarray, max_delay_ms: int = 450) -> int:
        """Estimate sample lag between far-end TTS signal and near-end mic capture via normalized cross-correlation."""
        if len(near_end) < 320 or len(far_end) < 320:
            return 0
        max_lag = min(int(SR * max_delay_ms / 1000), max(1, len(far_end) - len(near_end)))
        if max_lag <= 0:
            return 0
        x = np.asarray(near_end, dtype=np.float32)
        y = np.asarray(far_end, dtype=np.float32)
        x = x - float(np.mean(x))
        y = y - float(np.mean(y))
        n_fft = 1 << int(np.ceil(np.log2(len(x) + len(y))))
        corr = np.fft.irfft(np.fft.rfft(x, n_fft) * np.conj(np.fft.rfft(y, n_fft)), n_fft)
        # Positive lags where near_end comes after far_end segment
        best_lag = int(np.argmax(np.abs(corr[:max_lag])))
        self.delay_samples = best_lag
        return best_lag

    def process_block(self, near_pcm: np.ndarray, far_block: np.ndarray) -> tuple[np.ndarray, float]:
        """Cancel aligned far-end echo from near_pcm; returns (cleaned_float32, erle_db)."""
        d = np.asarray(near_pcm, dtype=np.float32)
        if d.dtype == np.int16 or np.max(np.abs(d)) > 1.5:
            d = d / 32768.0
        x = np.asarray(far_block, dtype=np.float32)
        if len(x) < len(d):
            x = np.pad(x, (len(d) - len(x), 0))
        elif len(x) > len(d):
            x = x[-len(d):]

        e = np.empty_like(d)
        eps = 1e-4
        w = self.w
        hist = self._x_hist
        L = self.L
        mu = self.mu

        for i in range(len(d)):
            hist[1:] = hist[:-1]
            hist[0] = x[i]
            y_hat = float(np.dot(w, hist))
            err = float(d[i]) - y_hat
            e[i] = err
            norm = float(np.dot(hist, hist)) + eps
            w += (mu * err / norm) * hist

        in_pow = float(np.mean(d ** 2)) + 1e-9
        err_pow = float(np.mean(e ** 2)) + 1e-9
        erle_db = float(10.0 * np.log10(in_pow / err_pow))
        self.last_erle_db = erle_db
        return np.clip(e, -1.0, 1.0).astype(np.float32), erle_db

    def calibrate(self, near_pcm: np.ndarray, far_pcm: np.ndarray) -> float:
        """Calibrate delay & NLMS filter on pure echo reference (no double-talk) and set `verified`."""
        self.reset()
        self.estimate_delay(near_pcm, far_pcm)
        f32 = np.asarray(far_pcm, dtype=np.float32)
        if np.max(np.abs(f32)) > 1.5:
            f32 = f32 / 32768.0
        n32 = np.asarray(near_pcm, dtype=np.float32)
        if np.max(np.abs(n32)) > 1.5:
            n32 = n32 / 32768.0
        step = 320
        erles = []
        for i in range(0, min(len(n32), len(f32)), step):
            _, db = self.process_block(n32[i : i + step], f32[i : i + step])
            erles.append(db)
        final_db = float(erles[-1]) if erles else 0.0
        self.last_erle_db = final_db
        self.verified = final_db >= self.min_erle_db
        return final_db

