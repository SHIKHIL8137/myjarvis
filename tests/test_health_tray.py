"""Health probes, first-run state, wizard hardware-test helpers, tray actions (no desktop session needed)."""
from __future__ import annotations

import inspect

import numpy as np
import pytest
from fastapi.testclient import TestClient

from jarvis import health
from jarvis.api import create_app
from jarvis.tray import MenuItem, TrayActions

H = {"X-Jarvis": "1"}


@pytest.fixture
def client(settings, assistant, monkeypatch):
    monkeypatch.setattr("jarvis.api.save_overrides", lambda patch, *a: None)
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda patch, *a: None)
    healthy = [{"id": 1, "name": "Test Mic", "default": True, "ok": True, "frames": 100, "rms_db": -40.0, "error": ""}]
    monkeypatch.setattr(health, "probe_input_devices", lambda *a, **k: healthy)  # no real audio hardware in unit tests
    with TestClient(create_app(settings, assistant)) as c:
        c.a = assistant
        yield c


def _by_id(rep):
    return {c["id"]: c for c in rep["checks"]}


def test_health_reports_real_state_and_exact_instructions(client, monkeypatch):
    monkeypatch.setattr("jarvis.stt.list_local_models", lambda: [])
    rep = client.get("/api/v2/setup/health").json()
    c = _by_id(rep)
    assert {"ollama", "llm_model", "stt_model", "tts_voice", "wake", "microphone", "speaker", "disk", "database", "offline", "mcp"} <= set(c)
    assert c["ollama"]["status"] == "ok"  # FakeBrain says up
    assert c["stt_model"]["status"] == "fail" and "download_model('medium.en'" in c["stt_model"]["action"]
    assert c["database"]["status"] in ("ok", "fail") and "head" in c["database"]["detail"]
    assert c["offline"]["detail"] == "offline enforced"
    assert rep["status"] == "fail"


def test_health_survives_ollama_offline_and_gives_pull_command(client):
    async def boom():
        raise ConnectionError("down")

    client.a.brain.health = boom
    rep = client.get("/api/v2/setup/health").json()  # health must still load with Ollama down
    c = _by_id(rep)
    assert c["ollama"]["status"] == "fail" and c["llm_model"]["status"] == "warn"
    assert "ollama pull" in c["llm_model"]["action"]
    assert client.get("/api/v2/settings").status_code == 200  # settings page also loads


def test_missing_model_instructions_are_exact():
    assert health.instruction_ollama_model("qwen3:4b-instruct").endswith("ollama pull qwen3:4b-instruct")
    assert health.piper_voice_urls("en_GB-alan-medium") == [
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json",
    ]
    wake = health.instruction_wake("hey_jarvis_v0.1")
    assert "hey_jarvis" in wake and "download_models" in wake


def test_disk_check_thresholds(tmp_path):
    assert health.disk_check(tmp_path, 0.0001)["status"] == "ok"
    d = health.disk_check(tmp_path, 10**9)
    assert d["status"] == "fail" and "Free up" in d["action"]


def test_setup_state_persists_and_health_module_never_downloads(client):
    assert client.get("/api/v2/setup/state").json() == {"completed": False}
    assert client.post("/api/v2/setup/complete", headers=H).json()["completed"] is True
    assert client.get("/api/v2/setup/state").json()["completed"] is True
    src = inspect.getsource(health)
    assert "urlopen" not in src and "requests." not in src and "httpx" not in src  # probes are local; downloads are manual


def test_wizard_saves_through_existing_settings_api(client):
    r = client.put("/api/v2/settings", json={"wake": {"threshold": 0.6}, "tts": {"enabled": True}}, headers=H)
    assert r.status_code == 200, r.text
    assert client.get("/api/v2/settings").json()["wake"]["threshold"] == 0.6
    assert client.a.settings.wake.threshold == 0.6  # consumed by the live backend


def test_mic_level_math():
    quiet = health.level_from_samples(np.zeros(1600, dtype=np.float32))
    loud = health.level_from_samples(np.ones(1600, dtype=np.float32) * 0.5)
    assert quiet["level"] == 0.0 and loud["level"] > 0.8 and loud["peak_db"] == pytest.approx(-6.0, abs=0.1)
    assert health.level_from_samples(np.empty(0))["level"] == 0.0


def test_wizard_hardware_endpoints_report_failures_cleanly(client, monkeypatch):
    def nomic(*a, **k):
        raise RuntimeError("no input device")

    monkeypatch.setattr(health, "mic_level", nomic)
    monkeypatch.setattr(health, "play_test_tone", nomic)
    assert client.get("/api/v2/setup/mic-level").json() == {"ok": False, "error": "no input device"}
    assert client.post("/api/v2/setup/speaker-test", headers=H).json()["ok"] is False
    monkeypatch.setattr(type(client.a.wake), "ready", property(lambda self: False))
    assert client.post("/api/v2/setup/wake-test", headers=H).json()["ok"] is False


def test_wake_listen_detects_with_fake_stream(monkeypatch):
    class FakeStream:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return np.zeros((n, 1), dtype=np.int16), False

    class FakeWake:
        spec = type("S", (), {"threshold": 0.5})()
        scores = iter([0.1, 0.2, 0.9])

        def reset(self):
            pass

        def score(self, frame):
            return next(self.scores)

    import sounddevice

    monkeypatch.setattr(sounddevice, "InputStream", FakeStream)
    r = health.wake_listen(FakeWake(), seconds=3)
    assert r["detected"] is True and r["best_score"] == 0.9


# ---- tray ----
class FakeApi:
    def __init__(self):
        self.calls, self.dictation = [], False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/v2/dictation/status":
            return {"enabled": self.dictation}
        if method == "PUT" and path == "/api/v2/settings":
            self.dictation = body["dictation"]["enabled"]
        return {}


def test_tray_actions_drive_the_real_endpoints():
    api, opened, quit_calls = FakeApi(), [], []
    t = TrayActions("http://127.0.0.1:4700", "C:/logs", api=api, open_url=opened.append, open_path=opened.append, on_quit=lambda: quit_calls.append(1))
    t.start()
    t.stop()
    assert t.toggle_mute() is True and t.toggle_mute() is False
    assert t.toggle_dictation() is True and api.dictation is True
    t.open_dashboard()
    t.open_logs()
    t.quit()
    paths = [(m, p) for m, p, _ in api.calls]
    assert ("POST", "/api/voice/mic") in paths and ("POST", "/api/voice/session") in paths and ("PUT", "/api/v2/settings") in paths
    assert api.calls[0][2] == {"enabled": True} and api.calls[1][2] == {"enabled": False}
    assert [c[2]["action"] for c in api.calls if c[1] == "/api/voice/session"] == ["pause", "resume"]
    assert opened == ["http://127.0.0.1:4700/", "C:/logs"] and quit_calls == [1]


def test_tray_menu_contains_required_items_and_survives_backend_down():
    def down(*a, **k):
        raise ConnectionError("down")

    t = TrayActions("http://127.0.0.1:4700", "C:/logs", api=down)
    items = t.menu()
    labels = [i.label for i in items if not i.separator]
    for need in ("Start JARVIS", "Stop JARVIS", "Mute microphone", "Open dashboard", "Open logs", "Dictation", "Quit"):
        assert any(need in label for label in labels), need
    assert all(isinstance(i, MenuItem) for i in items)


def test_tray_quit_stops_the_real_uvicorn_server_object():
    import server

    srv = type("S", (), {"should_exit": False})()
    t = TrayActions("http://x", "l", api=lambda *a, **k: {}, on_quit=lambda: setattr(srv, "should_exit", True))
    t.quit()
    assert srv.should_exit is True and callable(server.build_server)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows only")
def test_tray_creates_real_win32_window_and_dispatches_menu_commands():
    """Live Win32: hidden window + notification icon are created (64-bit prototypes) and WM_COMMAND reaches the action callback."""
    import ctypes
    import time

    from jarvis import tray as T

    t = T.JarvisTray(lambda: [])
    if t._headless:
        pytest.skip("no interactive desktop session")
    hit: list[str] = []
    t.start()
    try:
        for _ in range(30):
            if t.running:
                break
            time.sleep(0.1)
        assert t.running and t._hwnd and t._nid is not None
        t._callbacks[T.IDM_BASE + 1] = lambda: hit.append("ok")
        ctypes.windll.user32.PostMessageW(t._hwnd, T.WM_COMMAND, T.IDM_BASE + 1, 0)
        for _ in range(30):
            if hit:
                break
            time.sleep(0.1)
    finally:
        t.stop()
    assert hit == ["ok"] and not t.running


# ---- microphone diagnostics ----
class _FakeSD:
    """Minimal sounddevice stand-in: device 1 (default) is silent, device 2 delivers audio, device 3 cannot open."""

    class default:
        device = [1, 4]

    def __init__(self):
        self.opened = []

    def query_hostapis(self):
        return [{"name": "MME"}, {"name": "WASAPI"}]

    def query_devices(self, dev=None, kind=None):
        if dev is not None:
            return self.query_devices()[dev]
        mk = lambda n, api=0: {"name": n, "max_input_channels": 1, "hostapi": api, "default_samplerate": 16000}  # noqa: E731
        return [{"name": "spk", "max_input_channels": 0, "hostapi": 0, "default_samplerate": 16000}, mk("Headset (Buds)"), mk("Array Mic"), mk("Broken"), mk("Dup", 1)]

    def InputStream(self, device, callback, **kw):  # noqa: N802
        fake = self

        class S:
            def __enter__(s):
                fake.opened.append(device)
                if device == 3:
                    raise OSError("Unanticipated host error")
                if device == 2:
                    callback(np.full((800, 1), 0.1, dtype=np.float32), 800, None, None)
                return s

            def __exit__(s, *a):
                return False

        return S()


def test_probe_finds_silent_default_and_recommends_working_device():
    sd = _FakeSD()
    devs = health.probe_input_devices(0.0, sd=sd)
    assert [d["id"] for d in devs] == [1, 2, 3] and 4 not in sd.opened  # MME only, outputs skipped
    byid = {d["id"]: d for d in devs}
    assert byid[1]["ok"] is False and byid[2]["ok"] is True and byid[2]["frames"] == 800 and "Unanticipated" in byid[3]["error"]
    diag = health.mic_diagnosis(devs, None)
    assert diag["status"] == "fail" and "Bluetooth" in diag["detail"] and "Array Mic (id 2)" in diag["action"]
    assert health.mic_diagnosis(devs, 2)["status"] == "ok"  # explicit selection of the working device is honoured
    assert health.mic_diagnosis(devs, "array")["used"]["id"] == 2  # by name substring, like the voice loop
    assert health.mic_diagnosis(devs, 99)["status"] == "fail"  # unknown configured device is reported, not silently replaced


def test_mic_devices_endpoint_and_health_row_use_the_probe(client, monkeypatch):
    fake = [{"id": 1, "name": "Headset", "default": True, "ok": False, "frames": 0, "rms_db": -120.0, "error": ""},
            {"id": 2, "name": "Realtek Mic", "default": False, "ok": True, "frames": 100, "rms_db": -40.0, "error": ""}]
    monkeypatch.setattr(health, "probe_input_devices", lambda *a, **k: fake)
    r = client.get("/api/v2/setup/mic-devices").json()
    assert r["diagnosis"]["status"] == "fail" and r["diagnosis"]["working"][0]["id"] == 2
    row = _by_id(client.get("/api/v2/setup/health").json())["microphone"]
    assert row["status"] == "fail" and "Realtek Mic (id 2)" in row["action"]
    client.put("/api/v2/settings", json={"audio": {"input_device": 2}}, headers=H)
    assert _by_id(client.get("/api/v2/setup/health").json())["microphone"]["status"] == "ok"


def test_dictation_recorder_follows_configured_input_device(tmp_path, monkeypatch):
    from jarvis.config import DictationSpec
    from jarvis.ptt import DictationService
    import threading

    opened = {}

    class SD(_FakeSD):
        def InputStream(self, device=None, **kw):  # noqa: N802
            opened["device"] = device
            return super().InputStream(device if device is not None else 0, **kw)

    import sys
    monkeypatch.setitem(sys.modules, "sounddevice", SD())
    svc = DictationService(object(), DictationSpec(enabled=True), root=tmp_path, data_dir=tmp_path, input_device=lambda: 2)
    ev = threading.Event()
    ev.set()
    svc._record(ev)
    assert opened["device"] == 2


def test_server_shutdown_is_bounded_so_an_open_dashboard_cannot_block_quit():
    """Regression: an open SSE stream (dashboard tab) kept uvicorn waiting forever after Quit / POST /api/system/shutdown."""
    import server
    from jarvis.config import load_settings

    srv = server.build_server(load_settings())
    assert srv.config.timeout_graceful_shutdown == 5
    assert callable(srv.config.app.state.request_shutdown)
    srv.config.app.state.request_shutdown()
    assert srv.should_exit is True
