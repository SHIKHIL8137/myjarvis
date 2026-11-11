"""Speech-to-text: faster-whisper on CPU (int8). Loaded lazily, offline only (local_files_only)."""
import io
import logging
import threading
import time
import wave

import numpy as np

from .config import ROOT, STTSpec

log = logging.getLogger("jarvis.stt")
SR = 16000


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
    if sr != SR and len(a):
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * SR / sr)), np.arange(len(a)), a).astype(np.float32)
    return a


class Whisper:
    def __init__(self, spec: STTSpec):
        self.spec, self._m, self._lock = spec, None, threading.Lock()

    @property
    def ready(self) -> bool:
        return any((ROOT / "models" / "whisper").glob(f"*{self.spec.model}*"))

    def load(self):
        with self._lock:
            if self._m is None:
                from faster_whisper import WhisperModel
                t = time.time()
                self._m = WhisperModel(self.spec.model, device="cpu", compute_type=self.spec.compute_type,
                                       cpu_threads=self.spec.cpu_threads, download_root=str(ROOT / "models" / "whisper"),
                                       local_files_only=True)
                self._m.transcribe(np.zeros(SR, dtype=np.float32))  # warm-up so the first real call is not cold
                log.info("whisper %s loaded+warmed in %.1fs", self.spec.model, time.time() - t)
        return self._m

    def transcribe(self, audio: np.ndarray) -> dict:
        """audio: mono float32 16 kHz -> {"text", "seconds"}. Blocking; call from a thread."""
        m = self.load()
        t = time.time()
        segs, _ = m.transcribe(audio, language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        return {"text": text, "seconds": round(time.time() - t, 2)}
