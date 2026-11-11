"""Validated settings. config.json (committed defaults) + data/settings.json (machine-local overrides, gitignored)."""
import json
import logging
import logging.handlers
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class BrainSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://localhost:11434/v1"
    model: str
    api_key: str = "ollama"
    num_ctx: int = Field(4096, ge=512, le=32768)
    thinking: bool = False  # True: model reasons; we ask for it separately and discard it
    max_tokens: int = Field(512, ge=32, le=8192)
    keep_alive: str = "5m"


class TTSSpec(BaseModel):
    enabled: bool = True
    voice: str = "en_GB-alan-medium"
    speed: float = Field(1.0, ge=0.5, le=2.0)  # >1 faster
    chunk_chars: int = Field(170, ge=60, le=400)
    output_device: int | str | None = None


class STTSpec(BaseModel):
    model: str = "small.en"
    compute_type: str = "int8"
    cpu_threads: int = Field(6, ge=1, le=32)


class WakeSpec(BaseModel):
    enabled: bool = True
    model: str = "hey_jarvis_v0.1"
    threshold: float = Field(0.5, ge=0.05, le=0.99)


class AudioSpec(BaseModel):
    input_device: int | str | None = None
    silence_ms: int = Field(800, ge=200, le=3000)
    no_speech_timeout_s: float = Field(6.0, ge=1, le=30)
    max_utterance_s: float = Field(20.0, ge=3, le=60)


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str = "127.0.0.1"
    port: int = Field(4700, ge=1024, le=65535)
    boot_brain: str = "local"
    notes_dir: str = "notes"
    offline_enforced: bool = True  # block any non-localhost connection inside the server process
    history_turns: int = Field(6, ge=0, le=30)
    context_notes: int = Field(3, ge=1, le=8)  # notes fed to the model per turn (measured: 3x450 chars halves time-to-first-token vs 4x700)
    context_chars: int = Field(450, ge=100, le=1500)
    brains: dict[str, BrainSpec]
    tts: TTSSpec = TTSSpec()
    stt: STTSpec = STTSpec()
    wake: WakeSpec = WakeSpec()
    audio: AudioSpec = AudioSpec()

    @property
    def notes_path(self) -> Path:
        p = Path(self.notes_dir)
        return p if p.is_absolute() else ROOT / p

    def model_post_init(self, _):
        if self.boot_brain not in self.brains:
            raise ValueError(f"boot_brain {self.boot_brain!r} not in brains {list(self.brains)}")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("host must be a loopback address; Jarvis is single-user and local-only")


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(config_path: Path | None = None, overrides_path: Path | None = None) -> Settings:
    config_path = config_path or ROOT / "config.json"
    overrides_path = overrides_path or DATA / "settings.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if overrides_path.exists():
        raw = _deep_merge(raw, json.loads(overrides_path.read_text(encoding="utf-8")))
    return Settings.model_validate(raw)


def save_overrides(patch: dict, overrides_path: Path | None = None) -> None:
    p = overrides_path or DATA / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_deep_merge(cur, patch), indent=2), encoding="utf-8")
    os.replace(tmp, p)


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(ROOT / "logs" / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(x, logging.handlers.RotatingFileHandler) for x in root.handlers):
        root.addHandler(h)
        root.setLevel(logging.INFO)
        for noisy in ("httpx", "httpx2", "httpcore", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
