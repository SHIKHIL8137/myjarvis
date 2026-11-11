import shutil
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.assistant import Assistant  # noqa: E402
from jarvis.brain import Brain, BrainError  # noqa: E402
from jarvis.config import load_settings  # noqa: E402


def ollama_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:11434/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def models_present(*names) -> bool:
    return all((ROOT / "models" / n).exists() for n in names)


needs_ollama = pytest.mark.skipif(not ollama_up(), reason="Ollama is not running on localhost:11434")
needs_piper = pytest.mark.skipif(not models_present("piper/en_GB-alan-medium.onnx"), reason="Piper voice not downloaded")
needs_whisper = pytest.mark.skipif(not any((ROOT / "models" / "whisper").glob("models--*")), reason="Whisper model not downloaded")
needs_wake = pytest.mark.skipif(not models_present("wake/hey_jarvis_v0.1.onnx"), reason="wake-word model not downloaded")


def have_mic() -> bool:
    try:
        import sounddevice as sd
        return sd.query_devices(kind="input") is not None
    except Exception:
        return False


needs_mic = pytest.mark.skipif(not have_mic(), reason="no input audio device")


@pytest.fixture
def notes_dir(tmp_path):
    d = tmp_path / "notes"
    shutil.copytree(ROOT / "notes", d, ignore=shutil.ignore_patterns("captures", ".trash"))
    return d


@pytest.fixture
def settings(notes_dir):
    s = load_settings()
    s.notes_dir = str(notes_dir)
    return s


class FakeBrain(Brain):
    """Brain with scripted replies and no network."""
    def __init__(self, settings):
        super().__init__(settings)
        self.replies = ["Certainly, sir. ", "Here is your answer."]
        self.fail = False
        self.unloaded = []
        self.seen = []

    async def installed(self):
        return ["qwen3:4b-instruct", "qwen3:4b", "qwen2.5vl:3b"]

    async def health(self):
        return {"up": not self.fail, "version": "fake", "models": await self.installed(), "loaded": []}

    async def stream(self, messages):
        self.seen.append(messages)
        if self.fail:
            raise BrainError("Ollama is not reachable (fake)")
        for r in self.replies:
            yield r
        self.last_stats = {"ttft": 0.1, "tokens": 5, "tps": 50.0, "total": 0.2, "model": self.spec.model, "truncated": False}

    async def unload(self, model):
        self.unloaded.append(model)

    async def warm(self):
        return 0.0


@pytest.fixture
def fake_brain(settings):
    return FakeBrain(settings)


@pytest.fixture
def assistant(settings, fake_brain, tmp_path, monkeypatch):
    monkeypatch.setattr(Assistant, "_warm_speech", lambda self: None)  # do not load Whisper/Piper in unit tests
    return Assistant(settings, data_dir=tmp_path / "data", brain=fake_brain)
