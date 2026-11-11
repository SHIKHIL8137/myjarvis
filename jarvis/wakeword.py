"""Wake word: openWakeWord 'hey_jarvis' on CPU via ONNX. Frames are 1280 samples (80 ms) of 16 kHz int16 mono.
Custom 'Jarvis'-only training is not needed: the pretrained 'hey_jarvis' model is the supported wake phrase."""
import logging

import numpy as np

from .config import ROOT, WakeSpec

log = logging.getLogger("jarvis.wake")
FRAME = 1280


class WakeWord:
    def __init__(self, spec: WakeSpec):
        self.spec, self._m = spec, None

    @property
    def ready(self) -> bool:
        return (ROOT / "models" / "wake" / f"{self.spec.model}.onnx").exists()

    def load(self):
        if self._m is None:
            import openwakeword
            d = ROOT / "models" / "wake"
            self._m = openwakeword.Model(
                wakeword_models=[str(d / f"{self.spec.model}.onnx")], inference_framework="onnx",
                melspec_model_path=str(d / "melspectrogram.onnx"), embedding_model_path=str(d / "embedding_model.onnx"))
        return self._m

    def score(self, frame: np.ndarray) -> float:
        """frame: int16, len FRAME -> highest wake score for this frame (0..1)."""
        out = self.load().predict(frame)
        return max(out.values()) if out else 0.0

    def reset(self):
        if self._m is not None:
            self._m.reset()
