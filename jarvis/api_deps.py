"""FastAPI dependency injection providers and shared validated settings mutation helpers."""
from fastapi import Request

from . import sysinfo
from .config import (
    PROVIDER_ENV_VARS,
    ROOT,
    BrainSpec,
    Settings,
    has_provider_api_key,
    save_overrides,
)
from .stt import SUPPORTED_LANGUAGES, list_local_models

_orig_save_overrides = save_overrides


def _persist_overrides(patch: dict) -> None:
    """Respect monkeypatches on either jarvis.api.save_overrides or jarvis.api_deps.save_overrides."""
    import sys

    api_mod = sys.modules.get("jarvis.api")
    api_fn = getattr(api_mod, "save_overrides", None) if api_mod is not None else None
    if api_fn is not None and api_fn is not _orig_save_overrides and save_overrides is _orig_save_overrides:
        api_fn(patch)
    else:
        save_overrides(patch)


def get_assistant(request: Request):
    """Resolve the active Assistant / service container dynamically from app.state."""
    return request.app.state.a


def make_assistant_dep(fallback=None):
    """Return a FastAPI Depends callable resolving request.app.state.a with optional test fallback."""

    def _resolve(request: Request):
        return getattr(request.app.state, "a", None) or fallback

    return _resolve


def build_consolidated_settings(a) -> dict:
    """Return full, secret-safe settings snapshot used by both /api/config and /api/v2/settings."""
    s = a.settings
    provider_keys = {p: has_provider_api_key(p) for p in PROVIDER_ENV_VARS}
    return {
        "tts": s.tts.model_dump(),
        "wake": s.wake.model_dump(),
        "audio": s.audio.model_dump(),
        "stt": s.stt.model_dump(),
        "translation": s.translation.model_dump(),
        "internet": s.internet.model_dump(exclude={"allowed_search_hosts"}),
        "memory": s.memory.model_dump(),
        "filesystem": s.filesystem.model_dump(),
        "actions": s.actions.model_dump(),
        "monitoring": s.monitoring.model_dump(),
        "proactive": s.proactive.model_dump(),
        "providers": s.providers.model_dump(),
        "provider_keys": provider_keys,
        "gateway": s.gateway.model_dump(),
        "learning": s.learning.model_dump(),
        "preferences": s.preferences.model_dump(),
        "history_turns": s.history_turns,
        "boot_brain": s.boot_brain,
        "offline_enforced": s.offline_enforced,
        "mode": s.mode,
        "num_ctx": a.brain.spec.num_ctx,
        "voices": sorted(p.stem for p in (ROOT / "models" / "piper").glob("*.onnx")),
        "stt_models": list_local_models(),
        "stt_languages": SUPPORTED_LANGUAGES,
        "api_keys_configured": provider_keys,
    }


def apply_settings_patch(a, patch: dict, *, allow_gateway_and_prefs: bool = True) -> dict:
    """Validate, persist, and live-apply a partial settings update safely."""
    allowed = {
        "tts",
        "wake",
        "audio",
        "stt",
        "translation",
        "internet",
        "history_turns",
        "boot_brain",
        "num_ctx",
        "memory",
        "filesystem",
        "actions",
        "monitoring",
        "proactive",
        "providers",
        "learning",
    }
    if allow_gateway_and_prefs:
        allowed |= {"gateway", "preferences", "mode", "offline_enforced"}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError(f"Unsupported settings keys: {sorted(set(patch or {}) - allowed)}")

    # Reject raw secrets ever written into config overrides JSON
    for sec_val in patch.values():
        if isinstance(sec_val, dict) and any("api_key" in str(k).lower() or "secret" in str(k).lower() for k in sec_val):
            raise ValueError("API keys and secrets must be configured via the encrypted DPAPI key endpoint, never in settings")

    patch_copy = dict(patch)
    if "num_ctx" in patch_copy:
        b, n = a.brain, patch_copy.pop("num_ctx")
        if not isinstance(n, int) or isinstance(n, bool) or not 1024 <= n <= 8192:
            raise ValueError("num_ctx must be an integer between 1024 and 8192 (4 GB VRAM)")
        spec = BrainSpec.model_validate({**b.spec.model_dump(), "num_ctx": n})
        if b.current in b.settings.brains:
            b.settings.brains[b.current] = spec
            _persist_overrides({"brains": {b.current: {"num_ctx": n}}})
        else:
            b.adhoc[b.current] = spec

    if "voice" in patch_copy.get("tts", {}) and not (ROOT / "models" / "piper" / f"{patch_copy['tts']['voice']}.onnx").exists():
        raise ValueError("that Piper voice is not installed in models/piper")
    if "model" in patch_copy.get("stt", {}):
        local_models = list_local_models()
        if local_models and patch_copy["stt"]["model"] not in local_models:
            raise ValueError(f"STT model {patch_copy['stt']['model']!r} is not downloaded locally ({', '.join(local_models)})")

    if "workspace_roots" in patch_copy.get("filesystem", {}):
        from .filesystem_access import validate_workspace_roots

        patch_copy["filesystem"] = dict(patch_copy["filesystem"])
        patch_copy["filesystem"]["workspace_roots"] = validate_workspace_roots(
            patch_copy["filesystem"].get("workspace_roots") or []
        )

    raw = a.settings.model_dump()
    raw.pop("mode", None)
    if "mode" in patch_copy:
        req_mode = str(patch_copy.pop("mode") or "").strip().lower()
        if req_mode in ("online", "online_assisted"):
            patch_copy.setdefault("offline_enforced", False)
            patch_copy["internet"] = {**raw.get("internet", {}), **patch_copy.get("internet", {}), "enabled": True}
        elif req_mode in ("local", "offline"):
            patch_copy.setdefault("offline_enforced", True)
            patch_copy["internet"] = {**raw.get("internet", {}), **patch_copy.get("internet", {}), "enabled": False}
    for k, v in patch_copy.items():
        if k in raw and isinstance(raw[k], dict) and isinstance(v, dict):
            raw[k] = {**raw[k], **v}
        else:
            raw[k] = v
    raw.pop("mode", None)
    new = Settings.model_validate(raw)
    if patch_copy:
        _persist_overrides(patch_copy)
    a.apply_settings(new)
    a.bus.publish({"type": "internet", **a.search.status()})
    a.bus.publish({"type": "config_updated", "keys": sorted(patch.keys())})
    return build_consolidated_settings(a)


def _safe_audio_inventory() -> dict:
    """Return host audio input/output devices plus default names without raising when disconnected."""
    defaults: dict[str, str | None] = {"input": None, "output": None}
    err: str | None = None
    try:
        defaults = sysinfo.audio()
    except Exception as e:
        err = str(e)

    inputs: list[dict] = []
    outputs: list[dict] = []
    try:
        import sounddevice as sd

        raw_devs = list(sd.query_devices() or [])
        mme_devs = []
        for idx, dev in enumerate(raw_devs):
            try:
                host_name = str(sd.query_hostapis(dev["hostapi"])["name"])
            except Exception:
                host_name = ""
            if host_name == "MME":
                mme_devs.append((idx, dev))
        selected = mme_devs if mme_devs else list(enumerate(raw_devs))
        for idx, dev in selected:
            name = str(dev.get("name") or f"Device {idx}")
            if int(dev.get("max_input_channels") or 0) > 0:
                inputs.append({"index": int(idx), "name": name})
            if int(dev.get("max_output_channels") or 0) > 0:
                outputs.append({"index": int(idx), "name": name})
    except Exception as e:
        if not err:
            err = str(e)

    return {
        "available": bool(inputs or outputs or defaults.get("input") or defaults.get("output")),
        "input": defaults.get("input"),
        "output": defaults.get("output"),
        "inputs": inputs,
        "outputs": outputs,
        "error": err,
    }


def list_settings_devices(a) -> dict:
    """Return safe desktop audio/voice/model inventory without probing network."""
    return {
        "voices": sorted(p.stem for p in (ROOT / "models" / "piper").glob("*.onnx")),
        "stt_models": list_local_models(),
        "stt_languages": SUPPORTED_LANGUAGES,
        "audio": _safe_audio_inventory(),
    }
