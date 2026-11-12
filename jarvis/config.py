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
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "huggingface": "HF_TOKEN",
    "custom": "JARVIS_CUSTOM_LLM_API_KEY",
}
_LANG_RE = re.compile(r"^(auto|none|mixed|[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$")


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
    language: str = "auto"  # "auto" | "en" | "ml" | "mixed" | "hi" | "ta" | ...
    quality: str = "balanced"  # "fast" | "balanced" | "accurate"
    preprocessing_profile: str = "light"  # "off" | "light" | "standard"
    beam_size: int | None = Field(None, ge=1, le=10)
    best_of: int | None = Field(None, ge=1, le=10)
    vad_filter: bool = True
    vad_threshold: float = Field(0.45, ge=0.1, le=0.95)
    min_silence_duration_ms: int = Field(450, ge=100, le=3000)
    min_speech_duration_ms: int = Field(120, ge=50, le=2000)
    speech_pad_ms: int = Field(300, ge=50, le=1000)
    short_utterance_pad_ms: int = Field(160, ge=0, le=500)
    vocab_hints: str = ""
    vocab_hints_file: str = "config/vocab_hints.txt"
    preserve_punctuation: bool = True
    word_timestamps: bool = False
    malayalam_model: str = ""  # optional local specialized provider/dir e.g. "malayalam_whisper" or "indic_conformer"
    hindi_model: str = ""
    tamil_model: str = ""
    max_retry_attempts: int = Field(2, ge=0, le=3)
    retry_on_low_quality: bool = True
    max_cached_models: int = Field(2, ge=1, le=3)
    preserve_original_audio: bool = True
    audio_store_dir: str = "data/audio"

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

    @field_validator("preprocessing_profile")
    @classmethod
    def _valid_profile(cls, v: str) -> str:
        v = (v or "light").strip().lower()
        if v not in ("off", "light", "standard"):
            raise ValueError("preprocessing_profile must be 'off', 'light', or 'standard'")
        return v


class TranslationSpec(BaseModel):
    mode: str = "transcribe_only"  # "transcribe_only" | "translate"
    target_language: str = "none"  # "none" | "en" | "ml" | "hi" | ...
    auto_correct: bool = False
    prefer_whisper_for_english: bool = False
    verify_translations: bool = True
    preserve_mixed_code_switching: bool = True
    eval_export_dir: str = "data/eval_consented"

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
    prefer_native_sample_rate: bool = True
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
    enable_user_preferences: bool = True
    enable_project_memory: bool = True
    enable_command_memory: bool = True
    enable_conversation_memory: bool = True
    duplicate_threshold: float = Field(0.85, ge=0.5, le=1.0)
    confirm_inferred: bool = True
    max_temporary_items: int = Field(30, ge=5, le=200)
    backup_dir: str = "data/memory/backups"
    ranking_similarity_weight: float = Field(0.55, ge=0.0, le=2.0)
    ranking_recency_weight: float = Field(0.15, ge=0.0, le=2.0)
    ranking_confidence_weight: float = Field(0.15, ge=0.0, le=2.0)
    ranking_relevance_weight: float = Field(0.10, ge=0.0, le=2.0)
    ranking_project_boost: float = Field(0.10, ge=0.0, le=2.0)
    ranking_frequency_weight: float = Field(0.12, ge=0.0, le=2.0)


class FilesystemSpec(BaseModel):
    allowed_roots: list[str] = Field(default_factory=lambda: [str(ROOT / "notes")])
    workspace_roots: list[str] = Field(default_factory=list)
    excluded_dirs: list[str] = Field(default_factory=lambda: [".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".trash", "Windows", "Program Files", "System32"])
    max_file_bytes: int = Field(512_000, ge=10_000, le=5_000_000)
    max_results: int = Field(40, ge=5, le=200)
    allow_write: bool = True
    sensitive_file_policy: str = Field("block", pattern="^(always_ask|block|warn|metadata_only)$")
    cloud_data_policy: str = Field("redacted", pattern="^(local_only|redact_only|redact_and_confirm|never|redacted|approved_only)$")
    allowed_extensions: list[str] = Field(default_factory=list)
    allow_always_read_only: bool = True


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
    allowed_url_domains: list[str] = Field(default_factory=lambda: [
        "docs.python.org",
        "developer.mozilla.org",
        "github.com",
        "wikipedia.org",
        "arxiv.org",
        "pypi.org",
    ])
    allow_screenshots: bool = True
    allow_media_controls: bool = True
    allow_window_controls: bool = True
    allow_input_automation: bool = False


class ProvidersSpec(BaseModel):
    allow_cloud: bool = False  # Never silently switch to cloud in Local Mode
    allow_paid_models: bool = False  # Paid providers require explicit owner authorization
    prefer_free_cloud: bool = True
    paid_providers: list[str] = Field(default_factory=lambda: ["openai", "anthropic"])
    active_provider: str = "ollama"
    fast_model: str = "local"
    advanced_model: str = "local_thinking"
    coding_model: str = "local"
    embedding_model: str = ""
    fallback_provider: str = "ollama"
    streaming: bool = True
    custom_base_url: str = ""
    custom_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-3-5-sonnet-latest"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "qwen/qwen3-coder:free"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    huggingface_base_url: str = "https://router.huggingface.co/v1"
    huggingface_model: str = "Qwen/Qwen2.5-Coder-32B-Instruct"
    allowed_cloud_hosts: list[str] = Field(
        default_factory=lambda: [
            "api.openai.com",
            "api.anthropic.com",
            "openrouter.ai",
            "generativelanguage.googleapis.com",
            "router.huggingface.co",
            "huggingface.co",
        ]
    )


class DatabaseSpec(BaseModel):
    url: str = ""  # Empty -> local SQLite+WAL (data/jarvis.db); or postgresql+psycopg://...
    role: str = Field("auto", pattern="^(auto|local_sqlite|dev_pg|test_pg|scratch_pg|prod_pg)$")
    production_targets: list[str] = Field(default_factory=list)
    scratch_target_url: str = ""
    test_target_url: str = ""
    registered_targets: list[dict] = Field(default_factory=list)
    echo: bool = False
    embedding_provider: str = Field("auto", pattern="^(auto|ollama|huggingface|local_hash)$")
    embedding_model: str = ""  # e.g. "nomic-embed-text" for Ollama or sentence-transformers model
    embedding_dim: int = Field(256, ge=32, le=4096)
    vector_index_type: str = Field("hnsw", pattern="^(hnsw|ivfflat)$")
    vector_metric: str = Field("cosine", pattern="^(cosine|l2|ip)$")
    embedding_version: str = "hash-v1:256"


class GatewaySpec(BaseModel):
    default_rpm_limit: int = Field(60, ge=1, le=1000)
    daily_token_budget: int = Field(500_000, ge=1000, le=50_000_000)
    classifier_confidence_threshold: float = Field(0.75, ge=0.3, le=0.99)
    enable_llm_classifier: bool = True
    scrub_outbound_secrets: bool = True
    circuit_breaker_failures: int = Field(3, ge=1, le=20)
    circuit_breaker_cooldown_s: float = Field(60.0, ge=5.0, le=3600.0)
    retry_max_attempts: int = Field(3, ge=1, le=6)
    retry_base_delay_s: float = Field(0.25, ge=0.05, le=5.0)
    request_timeout_s: float = Field(30.0, ge=2.0, le=300.0)
    max_local_concurrency: int = Field(1, ge=1, le=4)
    max_cloud_concurrency: int = Field(3, ge=1, le=16)
    max_queue_size: int = Field(16, ge=2, le=100)


class LayaSpec(BaseModel):
    """Isolated optional Laya System 1 Layer 2 Decision Classifier configuration (`disabled` by default)."""

    enabled: bool = False
    mode: str = Field("shadow", pattern="^(disabled|shadow|laya_primary|laya_with_heuristic_fallback)$")
    service_mode: str = Field("http", pattern="^(http|subprocess)$")
    base_url: str = "http://127.0.0.1:4710"
    python_bin: str = "data/laya_iso_venv/Scripts/python.exe"
    checkpoint: str = Field("multilingual", pattern="^(english|multilingual|typed-decisions|vegaml)$")
    device: str = Field("cpu", pattern="^(cpu|cuda|auto)$")
    timeout_ms: int = Field(1500, ge=100, le=30000)
    max_concurrency: int = Field(1, ge=1, le=8)
    max_resident_models: int = Field(1, ge=1, le=3)
    confidence_threshold: float = Field(0.70, ge=0.1, le=0.99)
    abstain_enabled: bool = True
    circuit_breaker_failures: int = Field(3, ge=1, le=20)
    circuit_breaker_cooldown_s: float = Field(30.0, ge=2.0, le=600.0)
    cache_enabled: bool = True
    calibration_dir: str = "data/decision/calibration"

    @field_validator("base_url")
    @classmethod
    def _loopback_base_url(cls, v: str) -> str:
        s = (v or "").strip().rstrip("/")
        if not s:
            return "http://127.0.0.1:4710"
        host = (urlsplit(s).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Laya service base_url must remain loopback-only; got {v!r}")
        return s


class WorkforceSpec(BaseModel):
    enabled: bool = True
    opencode_bin: str = "opencode"
    graphify_bin: str = "graphify"
    worktree_root_name: str = ".jarvis_worktrees"
    max_agent_steps: int = Field(16, ge=2, le=60)
    max_repair_retries: int = Field(2, ge=0, le=5)
    registered_projects: list[dict] = Field(default_factory=list)
    sandbox_backend: str = Field("restricted_subprocess", pattern="^(restricted_subprocess|docker)$")
    docker_image: str = "jarvis-opencode:1.19.5"
    allow_host_fallback: bool = False
    qa_timeout_s: int = Field(120, ge=5, le=1800)
    max_memory_mb: int = Field(1024, ge=128, le=16384)
    cpus: float = Field(2.0, ge=0.25, le=16.0)
    pids_limit: int = Field(256, ge=32, le=4096)
    read_only_root_fs: bool = True
    allowed_executables: list[str] = Field(
        default_factory=lambda: ["pytest", "python", "ruff", "mypy", "node", "npm", "npx", "tsc", "git"]
    )
    denied_executables: list[str] = Field(
        default_factory=lambda: [
            "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "curl", "wget", "rm", "del", "format", "reg", "diskpart",
        ]
    )
    network_mode: str = Field("none", pattern="^(none|host)$")
    network_policy: str = Field("offline", pattern="^(offline|model_gateway|restricted_egress|unrestricted)$")
    allowed_egress_domains: list[str] = Field(default_factory=list)
    allow_ollama_in_sandbox: bool = False
    docker_allow_unrestricted_network: bool = False
    docker_unrestricted_authorized_by: str = ""


class SkillsSpec(BaseModel):
    enabled: bool = True
    skills_dir: str = "data/skills"
    max_context_chars: int = Field(3200, ge=400, le=16000)
    max_skills_per_run: int = Field(3, ge=1, le=8)
    require_install_approval: bool = True
    require_update_approval: bool = True
    allow_untrusted_scripts: bool = False
    allowed_licenses: list[str] = Field(
        default_factory=lambda: ["mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "isc", "cc-by-4.0", "cc0-1.0"]
    )


class RecoverySpec(BaseModel):
    backup_dir: str = "data/backups"
    tier3_dir: str = ""
    retain_tier1: int = Field(10, ge=2, le=100)
    retain_tier2: int = Field(14, ge=2, le=100)
    retain_tier3: int = Field(7, ge=1, le=100)
    encrypt_bundles: bool = True
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "jarvis-backups"
    s3_region: str = "us-east-1"


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


class PreferencesSpec(BaseModel):
    theme: str = Field("jarvis_cyan", pattern="^(jarvis_cyan|midnight|high_contrast|dark-hud|cyan-hud|midnight-obsidian)$")
    compact_mode: bool = False
    reduced_motion: bool = False
    graph_detail: str = Field("standard", pattern="^(minimal|compact|standard|detailed|full)$")
    stt_provider: str = "faster_whisper"
    tts_provider: str = "piper"
    personality_preset: str = Field("professional", pattern="^(professional|friendly|concise|technical|detailed|custom)$")
    custom_instructions: str = Field("", max_length=600)
    quiet_hours: dict[str, object] = Field(
        default_factory=lambda: {"enabled": False, "start": "22:00", "end": "07:00"}
    )


class MonitoringSpec(BaseModel):
    enabled: bool = True
    poll_interval_s: float = Field(5.0, ge=1.0, le=300.0)
    cpu_alert_pct: float = Field(90.0, ge=50.0, le=100.0)
    ram_alert_pct: float = Field(90.0, ge=50.0, le=100.0)
    gpu_vram_alert_pct: float = Field(90.0, ge=50.0, le=100.0)
    disk_free_alert_gb: float = Field(5.0, ge=0.5, le=500.0)
    battery_low_pct: float = Field(20.0, ge=5.0, le=80.0)
    history_size: int = Field(60, ge=5, le=360)


class ProactiveSpec(BaseModel):
    enabled: bool = True
    cooldown_s: float = Field(120.0, ge=5.0, le=3600.0)
    suggest_on_system_alerts: bool = True
    suggest_on_failed_tasks: bool = True
    suggest_on_repeated_commands: bool = True


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
    database: DatabaseSpec = DatabaseSpec()
    gateway: GatewaySpec = GatewaySpec()
    laya: LayaSpec = LayaSpec()
    workforce: WorkforceSpec = WorkforceSpec()
    recovery: RecoverySpec = RecoverySpec()
    skills: SkillsSpec = SkillsSpec()
    preferences: PreferencesSpec = PreferencesSpec()
    monitoring: MonitoringSpec = MonitoringSpec()
    proactive: ProactiveSpec = ProactiveSpec()

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


def _apply_laya_env_overrides(raw: dict) -> dict:
    """Apply explicit `JARVIS_LAYA_*` environment variables when configured."""
    laya_cfg = dict(raw.get("laya") or {})
    env_bools = {"JARVIS_LAYA_ENABLED": "enabled", "JARVIS_LAYA_ABSTAIN_ENABLED": "abstain_enabled", "JARVIS_LAYA_CACHE_ENABLED": "cache_enabled"}
    for ek, fk in env_bools.items():
        if ek in os.environ:
            laya_cfg[fk] = os.environ[ek].strip().lower() in ("1", "true", "yes", "on")
    env_strs = {
        "JARVIS_LAYA_MODE": "mode",
        "JARVIS_LAYA_SERVICE_MODE": "service_mode",
        "JARVIS_LAYA_BASE_URL": "base_url",
        "JARVIS_LAYA_PYTHON_BIN": "python_bin",
        "JARVIS_LAYA_CHECKPOINT": "checkpoint",
        "JARVIS_LAYA_DEVICE": "device",
        "JARVIS_LAYA_CALIBRATION_DIR": "calibration_dir",
    }
    for ek, fk in env_strs.items():
        if val := os.environ.get(ek, "").strip():
            laya_cfg[fk] = val
    for ek, fk in (("JARVIS_LAYA_TIMEOUT_MS", "timeout_ms"), ("JARVIS_LAYA_MAX_CONCURRENCY", "max_concurrency"), ("JARVIS_LAYA_MAX_RESIDENT_MODELS", "max_resident_models")):
        if val := os.environ.get(ek, "").strip():
            laya_cfg[fk] = int(val)
    if val := os.environ.get("JARVIS_LAYA_CONFIDENCE_THRESHOLD", "").strip():
        laya_cfg["confidence_threshold"] = float(val)
    if laya_cfg:
        raw["laya"] = laya_cfg
    return raw


def _default_overrides_path() -> Path:
    env_ov = os.environ.get("JARVIS_OVERRIDES_FILE", "").strip()
    return Path(env_ov) if env_ov else (DATA / "settings.json")


def load_settings(config_path: Path | None = None, overrides_path: Path | None = None) -> Settings:
    config_path = config_path or ROOT / "config.json"
    overrides_path = overrides_path or _default_overrides_path()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if overrides_path.exists():
        raw = _deep_merge(raw, json.loads(overrides_path.read_text(encoding="utf-8")))
    raw = _apply_laya_env_overrides(raw)
    return Settings.model_validate(raw)


def save_overrides(patch: dict, overrides_path: Path | None = None) -> None:
    p = overrides_path or _default_overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_deep_merge(cur, patch), indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---- secure API key storage (DPAPI encrypted on Windows in gitignored data/secrets.json; never returned to UI) ----
def _load_secrets_file(secrets_path: Path | None = None) -> dict[str, str]:
    from .security.secrets import load_secrets_map
    return load_secrets_map(secrets_path or SECRETS_PATH)


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
    from .security.secrets import save_secrets_map
    p_name = (provider or "").strip().lower()
    if p_name not in PROVIDER_ENV_VARS:
        raise ValueError(f"provider {provider!r} does not use an API key")
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("API key must not be empty")
    path = secrets_path or SECRETS_PATH
    cur = _load_secrets_file(path)
    cur[p_name] = cleaned
    save_secrets_map(path, cur, encrypt_dpapi=True)


def delete_provider_api_key(provider: str, secrets_path: Path | None = None) -> None:
    from .security.secrets import save_secrets_map
    p_name = (provider or "").strip().lower()
    path = secrets_path or SECRETS_PATH
    if not path.exists():
        return
    cur = _load_secrets_file(path)
    if p_name in cur:
        del cur[p_name]
        save_secrets_map(path, cur, encrypt_dpapi=True)


def setup_logging() -> None:
    from .core.logging_ctx import CorrelationAndRedactionFilter, StructuredJsonFormatter

    (ROOT / "logs").mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(ROOT / "logs" / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    use_json = os.environ.get("JARVIS_LOG_FORMAT", "").strip().lower() == "json"
    if use_json:
        h.setFormatter(StructuredJsonFormatter())
    else:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [corr=%(correlation_id)s] %(name)s: %(message)s"))
    h.addFilter(CorrelationAndRedactionFilter())
    root = logging.getLogger()
    for existing in root.handlers:
        if not any(isinstance(f, CorrelationAndRedactionFilter) for f in existing.filters):
            existing.addFilter(CorrelationAndRedactionFilter())
    if not any(isinstance(x, logging.handlers.RotatingFileHandler) for x in root.handlers):
        root.addHandler(h)
        root.setLevel(logging.INFO)
        for noisy in ("httpx", "httpx2", "httpcore", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
