"""System health + first-run setup state. Read-only probes: nothing is downloaded or changed here.

Every check returns {id, label, status: ok|warn|fail, detail, action} where `action` is the exact manual step for the owner.
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import time
from pathlib import Path

import numpy as np

from .config import RESOURCE_ROOT, ROOT, Settings


def _c(id: str, label: str, status: str, detail: str = "", action: str = "") -> dict:
    return {"id": id, "label": label, "status": status, "detail": detail, "action": action}


def piper_voice_urls(voice: str) -> list[str]:
    """en_GB-alan-medium -> the two official rhasspy/piper-voices files (manual download)."""
    try:
        lang_region, name, quality = voice.split("-", 2)
        lang = lang_region.split("_")[0]
    except ValueError:
        return []
    base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{lang_region}/{name}/{quality}/{voice}"
    return [f"{base}.onnx", f"{base}.onnx.json"]


def instruction_ollama_model(model: str) -> str:
    return f"Run in a terminal (needs internet, JARVIS will not do it for you): ollama pull {model}"


def instruction_whisper(model: str) -> str:
    return (
        "Run once, with internet, from the JARVIS folder:\n"
        f".venv\\Scripts\\python.exe -c \"from faster_whisper import download_model; download_model('{model}', cache_dir=r'models\\whisper')\""
    )


def instruction_piper(voice: str) -> str:
    urls = piper_voice_urls(voice)
    return "Download both files into models\\piper\\ :\n" + "\n".join(urls) if urls else f"Place {voice}.onnx and {voice}.onnx.json in models\\piper\\"


def instruction_wake(model: str) -> str:
    name = model.rsplit("_v", 1)[0]
    return (
        "Run once, with internet:\n"
        f".venv\\Scripts\\python.exe -c \"import openwakeword.utils as u; u.download_models(['{name}'], target_directory=r'models\\wake')\""
    )


def disk_check(root: Path, min_free_gb: float) -> dict:
    free = shutil.disk_usage(root).free / 1e9
    if free < min_free_gb:
        return _c("disk", "Disk space", "fail", f"{free:.1f} GB free (< {min_free_gb:g} GB)", "Free up disk space on the drive that holds JARVIS.")
    return _c("disk", "Disk space", "ok", f"{free:.1f} GB free")


def audio_devices() -> tuple[dict | None, dict | None, str]:
    """(default_input, default_output, error)."""
    try:
        import sounddevice as sd

        def one(kind):
            try:
                return dict(sd.query_devices(kind=kind))
            except Exception:  # noqa: BLE001
                return None

        return one("input"), one("output"), ""
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)[:160]


async def collect(a, *, now=time.time) -> dict:
    """Run every probe against the live Assistant `a` (no network beyond the local Ollama host)."""
    s: Settings = a.settings
    checks: list[dict] = []

    # Ollama + models (settings must still load when Ollama is down: failures here become check results, never exceptions)
    try:
        h = await asyncio.wait_for(a.brain.health(), 6)
    except Exception as e:  # noqa: BLE001
        h = {"up": False, "error": str(e)[:120], "models": []}
    if h.get("up"):
        checks.append(_c("ollama", "Ollama", "ok", f"reachable (version {h.get('version', '?')})"))
    else:
        checks.append(_c("ollama", "Ollama", "fail", "not reachable on the configured local host", "Start Ollama (`ollama serve`), then re-check."))
    installed = list(h.get("models") or [])
    want = a.brain.spec.model
    prov = getattr(a.brain, "provider", "ollama")
    if prov == "ollama":
        if want in installed or any(m.split(":")[0] == want for m in installed):
            checks.append(_c("llm_model", "Active LLM", "ok", f"{want} installed"))
        else:
            checks.append(_c("llm_model", "Active LLM", "fail" if h.get("up") else "warn", f"{want} not installed" if h.get("up") else "cannot verify while Ollama is down", instruction_ollama_model(want)))
    else:
        checks.append(_c("llm_model", "Active LLM", "ok", f"cloud provider '{prov}' (not probed: offline-first)"))

    from .stt import list_local_models

    stt_models = list_local_models()
    if s.stt.model in stt_models:
        checks.append(_c("stt_model", "Speech-to-text model", "ok", f"{s.stt.model} present"))
    else:
        checks.append(_c("stt_model", "Speech-to-text model", "fail", f"{s.stt.model} not found in models\\whisper (found: {', '.join(stt_models) or 'none'})", instruction_whisper(s.stt.model)))
    if a.tts.ready:
        checks.append(_c("tts_voice", "Piper voice", "ok", f"{s.tts.voice} present"))
    else:
        checks.append(_c("tts_voice", "Piper voice", "fail", f"{s.tts.voice} missing in models\\piper", instruction_piper(s.tts.voice)))
    if not s.wake.enabled:
        checks.append(_c("wake", "Wake word", "warn", "disabled in settings"))
    elif a.wake.ready:
        checks.append(_c("wake", "Wake word", "ok", f"{s.wake.model} model present"))
    else:
        checks.append(_c("wake", "Wake word", "fail", f"{s.wake.model}.onnx missing in models\\wake", instruction_wake(s.wake.model)))

    din, dout, err = await asyncio.to_thread(audio_devices)
    if din:
        diag = mic_diagnosis(await asyncio.to_thread(probe_input_devices), s.audio.input_device)
        checks.append(_c("microphone", "Microphone", diag["status"], diag["detail"], diag["action"]))
    else:
        checks.append(_c("microphone", "Microphone", "fail", err or "no input device found", "Connect a microphone and allow microphone access for desktop apps in Windows Privacy settings."))
    if dout:
        checks.append(_c("speaker", "Speaker", "ok", dout["name"]))
    else:
        checks.append(_c("speaker", "Speaker", "warn", err or "no output device found", "Connect speakers or headphones."))
    active = bool(a.voice.mic_active)
    checks.append(_c("voice_loop", "Voice loop", "ok" if active else "warn", "listening" if active else "idle", "" if active else "Turn on the microphone in the voice dock for hands-free use."))

    checks.append(disk_check(ROOT, float(s.monitoring.disk_free_alert_gb)))

    try:
        mig = await asyncio.to_thread(migration_state, a.orchestrator.db)
        checks.append(_c("database", "Database migrations", "ok" if mig["at_head"] else "fail", f"{mig['current']} (head {mig['head']})", "" if mig["at_head"] else "Back up data\\, then run: .venv\\Scripts\\alembic.exe -c alembic.ini upgrade head"))
    except Exception as e:  # noqa: BLE001
        checks.append(_c("database", "Database migrations", "fail", str(e)[:160]))
    checks.append(_c("offline", "Network mode", "ok", "offline enforced" if s.offline_enforced else "online assisted (network explicitly enabled)"))
    checks.append(_c("providers", "Providers", "ok", f"active={s.providers.active_provider}; cloud {'allowed' if s.providers.allow_cloud else 'blocked'}"))
    mcp = a.mcp.status()
    bad = [m["name"] for m in mcp if m["enabled"] and m["state"] != "ready"]
    checks.append(_c("mcp", "MCP servers", "warn" if bad else "ok", f"{len(mcp)} configured; not ready: {', '.join(bad)}" if bad else f"{len(mcp)} configured"))

    worst = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "ok")
    return {"status": worst, "checked_at": round(now(), 3), "checks": checks}


def migration_state(db) -> dict:
    """Read-only: compare the DB's alembic revision with the code's head (never stamps or upgrades)."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(RESOURCE_ROOT / "jarvis" / "db" / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with db.sync_engine.connect() as conn:
        cur = MigrationContext.configure(conn).get_current_revision()
    return {"current": cur or "none", "head": head, "at_head": cur == head}


# ---- first-run state -----------------------------------------------------------------------------
def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "setup_state.json"


def setup_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"completed": False}


def complete_setup(data_dir: Path) -> dict:
    p = _state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    st = {"completed": True, "completed_at": round(time.time(), 3)}
    p.write_text(json.dumps(st), encoding="utf-8")
    return st


# ---- hardware tests used by the wizard --------------------------------------------------------------
def level_from_samples(x: np.ndarray) -> dict:
    if x.size == 0:
        return {"rms_db": -120.0, "peak_db": -120.0, "level": 0.0}
    rms = float(np.sqrt(np.mean(np.square(x))))
    peak = float(np.max(np.abs(x)))

    def db(v: float) -> float:
        return round(20 * math.log10(max(v, 1e-6)), 1)

    return {"rms_db": db(rms), "peak_db": db(peak), "level": round(min(1.0, max(0.0, (db(rms) + 60) / 60)), 3)}


def probe_input_devices(seconds: float = 0.4, sd=None) -> list[dict]:
    """Open every MME input device briefly and count the frames it really delivers.

    A device can be listed, openable and still silent (typical: a Bluetooth headset that is not in headset/HFP mode). This is the only
    reliable way to tell. Returns [{id, name, default, ok, frames, rms_db, error}].
    """
    import time

    if sd is None:
        import sounddevice

        sd = sounddevice
    try:
        default_in = sd.default.device[0]
        hostapis = sd.query_hostapis()
        devices = list(sd.query_devices())
    except Exception as e:  # noqa: BLE001
        return [{"id": -1, "name": "", "default": False, "ok": False, "frames": 0, "rms_db": -120.0, "error": f"audio subsystem unavailable: {e}"[:160]}]
    out: list[dict] = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0 or hostapis[d["hostapi"]]["name"] != "MME":
            continue
        chunks: list[np.ndarray] = []
        err = ""
        try:
            with sd.InputStream(device=i, samplerate=int(d["default_samplerate"]), channels=1, dtype="float32", callback=lambda x, n, t, st: chunks.append(x.copy())):
                time.sleep(seconds)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:100]
        frames = int(sum(len(c) for c in chunks))
        rms = level_from_samples(np.concatenate(chunks).reshape(-1))["rms_db"] if chunks else -120.0
        out.append({"id": i, "name": d["name"], "default": i == default_in, "ok": frames > 0 and not err, "frames": frames, "rms_db": rms, "error": err})
    return out


def mic_diagnosis(devices: list[dict], configured) -> dict:
    """Pick the device that will be used and say, in plain words, whether it works and what to do if not."""
    def pick():
        if configured not in (None, ""):
            for d in devices:
                if d["id"] == configured or (isinstance(configured, str) and configured.lower() in d["name"].lower()):
                    return d
            return None
        return next((d for d in devices if d["default"]), None)

    used, working = pick(), [d for d in devices if d["ok"]]
    if used is None:
        return {"status": "fail", "detail": "the configured input device was not found" if configured not in (None, "") else "no default input device", "used": None,
                "working": working, "action": "Settings -> Audio -> Input device: choose one of: " + ", ".join(f"{d['name']} (id {d['id']})" for d in working) if working else "Connect a microphone."}
    if used["ok"]:
        return {"status": "ok", "detail": f"{used['name']} delivers audio", "used": used, "working": working, "action": ""}
    alt = [d for d in working if d["id"] != used["id"]]
    hint = (" Likely a Bluetooth headset that is not in headset (hands-free) mode, or the microphone is muted/blocked by Windows privacy settings." if "headset" in used["name"].lower() or "bluetooth" in used["name"].lower() else
            " Check Windows Settings -> Privacy & security -> Microphone and that the device is not muted.")
    return {"status": "fail", "detail": f"{used['name']} is selected but delivered no audio ({used['error'] or '0 frames'}).{hint}", "used": used, "working": alt,
            "action": ("Settings -> Audio -> Input device: choose " + ", ".join(f"{d['name']} (id {d['id']})" for d in alt)) if alt else "No input device delivered audio; connect or unmute a microphone."}


def mic_level(device=None, seconds: float = 0.3) -> dict:
    """One short capture -> rms/peak in dBFS and 0..1 meter value (the UI polls this for a live meter)."""
    import sounddevice as sd

    n = int(16000 * max(0.1, min(seconds, 1.0)))
    data = sd.rec(n, samplerate=16000, channels=1, dtype="float32", device=device)
    sd.wait()
    return level_from_samples(np.asarray(data).reshape(-1))


def play_test_tone(device=None, seconds: float = 0.6, freq: float = 660.0) -> None:
    import sounddevice as sd

    sr = 22050
    t = np.arange(int(sr * seconds)) / sr
    tone = (0.2 * np.sin(2 * np.pi * freq * t) * np.minimum(1, np.minimum(t, seconds - t) * 20)).astype(np.float32)
    sd.play(tone, sr, device=device)
    sd.wait()


def wake_listen(wake, device=None, seconds: float = 5.0) -> dict:
    """Listen for the wake word for a few seconds; returns best score and whether it fired at the configured threshold."""
    import sounddevice as sd

    best, t0 = 0.0, time.monotonic()
    frame = 1280  # 80 ms @ 16 kHz (openwakeword frame)
    wake.reset()
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=frame, device=device) as st:
        while time.monotonic() - t0 < seconds:
            buf, _ = st.read(frame)
            best = max(best, float(wake.score(buf.reshape(-1))))
            if best >= wake.spec.threshold:
                break
    return {"detected": best >= wake.spec.threshold, "best_score": round(best, 3), "threshold": wake.spec.threshold}
