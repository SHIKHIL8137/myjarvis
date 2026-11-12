"""Stage E tests: System Telemetry, Threshold Alerts, /api/sysinfo & Non-Executing Proactive Suggestions."""
from __future__ import annotations

from starlette.testclient import TestClient

from jarvis import sysinfo
from jarvis.api import create_app
from jarvis.config import MonitoringSpec

H = {"X-Jarvis": "1"}


def test_sysinfo_honest_telemetry_and_alerts():
    # Trigger baseline + delta CPU measurement & real snapshot
    _ = sysinfo.cpu()
    snap = sysinfo.snapshot(MonitoringSpec(), include_processes=True)
    assert "cpu" in snap
    assert isinstance(snap["cpu"]["cores_logical"], int) and snap["cpu"]["cores_logical"] >= 1
    assert snap["cpu"]["temp_c"] is None and snap["cpu"]["temp_supported"] is False
    assert snap["cpu"]["unavailable_reason"]
    assert snap["ram"]["total_gb"] > 0
    assert isinstance(snap["disks"], list) and len(snap["disks"]) >= 1
    assert "battery" in snap
    assert "hostname" in snap["network"]
    assert isinstance(snap["processes"], list)

    hist = sysinfo.monitor_history(limit=5)
    assert len(hist) >= 1

    # Threshold breach evaluation
    fake_snap = {
        "cpu_pct": 95.2,
        "ram": {"used_pct": 93.0, "free_gb": 1.1},
        "gpu": {"used_pct": 96.0, "used_gb": 3.84, "total_gb": 4.0},
        "disks": [{"mount": "C:\\", "free_gb": 2.0}],
        "battery": {"present": True, "ac_online": False, "charge_pct": 12},
    }
    alerts = sysinfo.evaluate_alerts(
        fake_snap,
        MonitoringSpec(cpu_alert_pct=90, ram_alert_pct=90, gpu_vram_alert_pct=90, disk_free_alert_gb=5.0, battery_low_pct=20),
    )
    ids = {a["id"] for a in alerts}
    assert {"high_cpu", "high_ram", "high_vram", "low_disk_C:\\", "battery_low"} <= ids


def test_sysinfo_api_nl_and_non_executing_proactive_suggestions(assistant, settings, monkeypatch):
    monkeypatch.setattr("jarvis.api_deps.save_overrides", lambda *a, **kw: None)
    client = TestClient(create_app(settings, assistant=assistant))

    # 1. /api/sysinfo and /api/v2/system/health return full telemetry + Jarvis backend state
    r_sys = client.get("/api/sysinfo")
    assert r_sys.status_code == 200
    body = r_sys.json()
    assert "ram" in body and "disks" in body and "jarvis" in body
    assert body["jarvis"]["active_brain"] == settings.boot_brain

    r_h = client.get("/api/v2/system/health")
    assert r_h.status_code == 200
    assert "history" in r_h.json()

    # 2. NL system status works offline & opens system drawer
    nl = client.post("/api/chat", json={"message": "check system status"}, headers=H)
    assert nl.status_code == 200
    assert nl.json()["command"] == "system_health"
    assert "RAM:" in nl.json()["answer"]

    # 3. Proactive suggestions: non-executing cards from failed task + repeated command + simulated alert
    monkeypatch.setattr(
        "jarvis.sysinfo.snapshot",
        lambda *a, **kw: {
            "timestamp": 1.0,
            "cpu_pct": 94.0,
            "cpu": {"pct": 94.0, "cores_logical": 12, "temp_c": None, "temp_supported": False},
            "ram": {"total_gb": 16.0, "used_gb": 14.8, "free_gb": 1.2, "used_pct": 92.5},
            "gpu": None,
            "disks": [{"mount": "C:\\", "total_gb": 200.0, "free_gb": 50.0, "used_pct": 75.0, "health": "healthy"}],
            "battery": {"present": False},
            "network": {"hostname": "test-host", "addresses": [], "has_local_interface": False},
            "audio": {"input": None, "output": None},
            "processes": [],
            "alerts": [
                {"id": "high_cpu", "severity": "warning", "metric": "cpu_pct", "value": 94.0, "threshold": 90.0, "message": "CPU high"}
            ],
        },
    )

    r_sugs = client.get("/api/v2/system/suggestions")
    assert r_sugs.status_code == 200
    sugs = r_sugs.json()["suggestions"]
    assert len(sugs) >= 1
    assert all(s["auto_execute"] is False and s["requires_user_action"] is True for s in sugs)

    # Dismissing suppresses that suggestion during cooldown
    sid = sugs[0]["id"]
    dis = client.post(f"/api/v2/system/suggestions/{sid}/dismiss", headers=H)
    assert dis.status_code == 200
    after = client.get("/api/v2/system/suggestions").json()["suggestions"]
    assert all(s["id"] != sid for s in after)

    # Disabling proactive via consolidated settings disables suggestions cleanly
    put = client.put("/api/v2/settings", json={"proactive": {"enabled": False}}, headers=H)
    assert put.status_code == 200
    assert client.get("/api/v2/system/suggestions").json()["suggestions"] == []

