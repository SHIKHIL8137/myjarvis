"""Validated settings and secrets. config.json (committed defaults) + data/settings.json (local overrides, gitignored).
API keys are never read from or written to config.json/settings.json; they live in env vars or gitignored data/secrets.json."""
import json
import logging
import logging.handlers
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SECRETS_PATH = DATA / "secrets.json"

DEFAULT_SEARCH_HOSTS = [
    "html.duckduckgo.com",
    "lite.duckduckgo.com",
    "api.duckduckgo.com",
    "api.tavily.com",
    "api.search.brave.com",
    "google.serper.dev",
]
PROVIDER_ENV_VARS = {
    "tavily": "JARVIS_TAVILY_API_KEY",
    "brave": "JARVIS_BRAVE_API_KEY",
    "serper": "JARVIS_SERPER_API_KEY",
    "searxng": "JARVIS_SEARXNG_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_LANG_RE = re.compile(r"^(auto|none|[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$")


class BrainSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:11434/v1"
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
    model: str = "small"
    compute_type: str = "int8"
    cpu_threads: int = Field(6, ge=1, le=32)
    language: str = "auto"  # "auto" | "en" | "ml" | "hi" | ...
    quality: str = "balanced"  # "fast" | "balanced" | "accurate"
    beam_size: int | None = Field(None, ge=1, le=10)
    best_of: int | None = Field(None, ge=1, le=10)
    vad_filter: bool = True
    vad_threshold: float = Field(0.45, ge=0.1, le=0.95)
    min_silence_duration_ms: int = Field(450, ge=100, le=3000)
    min_speech_duration_ms: int = Field(120, ge=50, le=2000)
    speech_pad_ms: int = Field(300, ge=50, le=1000)
    vocab_hints: str = ""
    preserve_punctuation: bool = True

    @field_validator("language")
    @classmethod
    def _valid_lang(cls, v: str) -> str:
        v = (v or "auto").strip().lower()
        if not _LANG_RE.match(v) or v == "none":
            raise ValueError(f"invalid STT language: {v!r}")
        return v

    @field_validator("quality")
    @classmethod
    def _valid_quality(cls, v: str) -> str:
        v = (v or "balanced").strip().lower()
        if v not in ("fast", "balanced", "accurate"):
            raise ValueError("quality must be 'fast', 'balanced', or 'accurate'")
        return v


class TranslationSpec(BaseModel):
    mode: str = "transcribe_only"  # "transcribe_only" | "translate"
    target_language: str = "none"  # "none" | "en" | "ml" | "hi" | ...
    auto_correct: bool = False
    prefer_whisper_for_english: bool = True

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = (v or "transcribe_only").strip().lower()
        if v not in ("transcribe_only", "translate"):
            raise ValueError("translation mode must be 'transcribe_only' or 'translate'")
        return v

    @field_validator("target_language")
    @classmethod
    def _valid_target(cls, v: str) -> str:
        v = (v or "none").strip().lower()
        if not _LANG_RE.match(v) or v == "auto":
            raise ValueError(f"invalid translation target_language: {v!r}")
        return v


class WakeSpec(BaseModel):
    enabled: bool = True
    model: str = "hey_jarvis_v0.1"
    threshold: float = Field(0.5, ge=0.05, le=0.99)


class AudioSpec(BaseModel):
    input_device: int | str | None = None
    sample_rate: int = Field(16000, ge=8000, le=48000)
    voice_mode: str = "continuous"  # "continuous" | "ptt" | "wake_word"
    duplex_mode: str = "half_duplex"  # "half_duplex" (safe default) | "full_duplex" (requires verified AEC)
    echo_cancellation: str = "auto"  # "auto" | "nlms" | "off"
    speech_confirm_ms: int = Field(180, ge=80, le=600)
    silence_ms: int = Field(850, ge=200, le=3000)
    no_speech_timeout_s: float = Field(6.0, ge=1, le=30)
    max_utterance_s: float = Field(25.0, ge=3, le=60)
    min_utterance_ms: int = Field(200, ge=100, le=1500)
    pre_roll_ms: int = Field(240, ge=0, le=1000)
    use_silero_vad: bool = True
    vad_speech_threshold: float = Field(0.35, ge=0.1, le=0.9)
    noise_gate_rms: float = Field(220.0, ge=50.0, le=2000.0)
    barge_in_enabled: bool = True
    barge_in_sensitivity: str = "medium"  # "low" | "medium" | "high"
    suppress_mic_during_tts: bool = True  # safe default in half-duplex to prevent speaker echo feedback
    mic_resume_delay_ms: int = Field(300, ge=0, le=2000)
    post_tts_cooldown_ms: int = Field(250, ge=0, le=1500)
    wake_followup_s: float = Field(6.0, ge=0.0, le=30.0)

    @field_validator("voice_mode")
    @classmethod
    def _valid_voice_mode(cls, v: str) -> str:
        v = (v or "continuous").strip().lower()
        if v not in ("continuous", "ptt", "wake_word"):
            raise ValueError("voice_mode must be 'continuous', 'ptt', or 'wake_word'")
        return v

    @field_validator("duplex_mode")
    @classmethod
    def _valid_duplex_mode(cls, v: str) -> str:
        v = (v or "half_duplex").strip().lower()
        if v not in ("half_duplex", "full_duplex"):
            raise ValueError("duplex_mode must be 'half_duplex' or 'full_duplex'")
        return v

    @field_validator("echo_cancellation")
    @classmethod
    def _valid_aec_mode(cls, v: str) -> str:
        v = (v or "auto").strip().lower()
        if v not in ("auto", "nlms", "off"):
            raise ValueError("echo_cancellation must be 'auto', 'nlms', or 'off'")
        return v

    @field_validator("barge_in_sensitivity")
    @classmethod
    def _valid_barge_sens(cls, v: str) -> str:
        v = (v or "medium").strip().lower()
        if v not in ("low", "medium", "high"):
            raise ValueError("barge_in_sensitivity must be 'low', 'medium', or 'high'")
        return v


class InternetSpec(BaseModel):
    enabled: bool = False  # Disabled by default; explicit opt-in required
    provider: str = "duckduckgo"  # "duckduckgo" | "tavily" | "brave" | "serper" | "searxng"
    allow_page_fetch: bool = False
    searxng_url: str = ""
    max_results: int = Field(5, ge=1, le=10)
    timeout_s: float = Field(8.0, ge=1.0, le=30.0)
    rate_limit_rpm: int = Field(20, ge=1, le=120)
    cache_ttl_s: int = Field(300, ge=0, le=3600)
    max_fetch_bytes: int = Field(512_000, ge=10_000, le=2_000_000)
    max_redirects: int = Field(3, ge=0, le=5)
    respect_robots: bool = True
    tool_calling: bool = True
    allowed_search_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_HOSTS))

    @field_validator("provider")
    @classmethod
    def _valid_provider(cls, v: str) -> str:
        v = (v or "duckduckgo").strip().lower()
        if v not in ("duckduckgo", "tavily", "brave", "serper", "searxng"):
            raise ValueError(f"unsupported search provider: {v!r}")
        return v

    @field_validator("searxng_url")
    @classmethod
    def _valid_searxng_url(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("searxng_url must be a valid https:// URL")
        return v.rstrip("/")


class MemorySpec(BaseModel):
    enabled: bool = True
    duplicate_threshold: float = Field(0.85, ge=0.5, le=1.0)
    confirm_inferred: bool = True
    max_temporary_items: int = Field(30, ge=5, le=200)
    backup_dir: str = "data/memory/backups"


class FilesystemSpec(BaseModel):
    allowed_roots: list[str] = Field(default_factory=lambda: [str(ROOT / "notes")])
    excluded_dirs: list[str] = Field(default_factory=lambda: [".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".trash", "Windows", "Program Files", "System32"])
    max_file_bytes: int = Field(512_000, ge=10_000, le=5_000_000)
    max_results: int = Field(40, ge=5, le=200)
    allow_write: bool = True


class ActionsSpec(BaseModel):
    enabled: bool = True
    allowed_apps: dict[str, list[str]] = Field(default_factory=lambda: {
        "notepad": ["notepad.exe"],
        "calculator": ["calc.exe"],
        "calc": ["calc.exe"],
        "explorer": ["explorer.exe"],
        "vscode": ["code"],
        "code": ["code"],
    })
    allowed_commands: list[str] = Field(default_factory=lambda: ["pytest", "python", "git status", "ollama list", "ollama ps"])
    confirm_levels: list[str] = Field(default_factory=lambda: ["MODIFY", "DESTRUCTIVE", "SYSTEM"])


class ProvidersSpec(BaseModel):
    allow_cloud: bool = False  # Never silently switch to cloud in Local Mode
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-3-5-sonnet-latest"


class LearningSpec(BaseModel):
    enabled: bool = True
    automatic_knowledge_updates: bool = True
    automatic_feedback_collection: bool = True
    automatic_training: bool = False
    require_training_approval: bool = True
    minimum_examples: int = Field(500, ge=1, le=100_000)
    evaluation_required: bool = True
    auto_promote: bool = False
    rollback_enabled: bool = True
    require_verified_license: bool = True
    allowed_licenses: list[str] = Field(default_factory=lambda: [
        "apache-2.0", "mit", "bsd-3-clause", "bsd-2-clause", "cc-by-4.0", "cc-by-sa-4.0", "cc0-1.0", "unlicense", "isc",
    ])
    allowed_knowledge_hosts: list[str] = Field(default_factory=lambda: [
        "huggingface.co", "datasets-server.huggingface.co", "docs.python.org", "raw.githubusercontent.com",
    ])
    knowledge_sources: list[dict] = Field(default_factory=list)
    dataset_sources: list[dict] = Field(default_factory=list)
    max_download_bytes: int = Field(50_000_000, ge=100_000, le=500_000_000)
    update_interval_hours: int = Field(24, ge=1, le=720)
    embedding_model: str = ""  # Optional Ollama embedding tag e.g. "nomic-embed-text"
    training_backend: str = "peft_lora"  # "peft_lora" | "qlora" | "remote"
    remote_worker_url: str = ""


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str = "127.0.0.1"
    port: int = Field(4700, ge=1024, le=65535)
    boot_brain: str = "local"
    notes_dir: str = "notes"
    offline_enforced: bool = True  # block unauthorized non-localhost connections inside the server process
    history_turns: int = Field(6, ge=0, le=30)
    context_notes: int = Field(3, ge=1, le=8)  # notes fed to the model per turn
    context_chars: int = Field(450, ge=100, le=1500)
    brains: dict[str, BrainSpec]
    tts: TTSSpec = TTSSpec()
    stt: STTSpec = STTSpec()
    translation: TranslationSpec = TranslationSpec()
    wake: WakeSpec = WakeSpec()
    audio: AudioSpec = AudioSpec()
    internet: InternetSpec = InternetSpec()
    memory: MemorySpec = MemorySpec()
    filesystem: FilesystemSpec = FilesystemSpec()
    actions: ActionsSpec = ActionsSpec()
    providers: ProvidersSpec = ProvidersSpec()
    learning: LearningSpec = LearningSpec()

    @property
    def notes_path(self) -> Path:
        p = Path(self.notes_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def mode(self) -> str:
        return "online_assisted" if self.internet.enabled else "local"

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


# ---- secure API key storage (env vars or gitignored data/secrets.json; never committed or returned to UI) ----
def _load_secrets_file(secrets_path: Path | None = None) -> dict[str, str]:
    p = secrets_path or SECRETS_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}
    except Exception:
        return {}


def get_provider_api_key(provider: str, secrets_path: Path | None = None) -> str | None:
    p = (provider or "").strip().lower()
    env_name = PROVIDER_ENV_VARS.get(p)
    if env_name:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return _load_secrets_file(secrets_path).get(p)


def has_provider_api_key(provider: str, secrets_path: Path | None = None) -> bool:
    return bool(get_provider_api_key(provider, secrets_path))


def set_provider_api_key(provider: str, key: str, secrets_path: Path | None = None) -> None:
    p_name = (provider or "").strip().lower()
    if p_name not in PROVIDER_ENV_VARS:
        raise ValueError(f"provider {provider!r} does not use an API key")
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("API key must not be empty")
    path = secrets_path or SECRETS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = _load_secrets_file(path)
    cur[p_name] = cleaned
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def delete_provider_api_key(provider: str, secrets_path: Path | None = None) -> None:
    p_name = (provider or "").strip().lower()
    path = secrets_path or SECRETS_PATH
    if not path.exists():
        return
    cur = _load_secrets_file(path)
    if p_name in cur:
        del cur[p_name]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        os.replace(tmp, path)


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
