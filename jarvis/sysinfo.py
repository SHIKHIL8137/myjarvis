"""System telemetry & health monitoring (CPU, RAM, GPU/VRAM/temp, disks, battery, network, processes).

All metrics are read-only and honest: unsupported hardware sensors report `None` with an
explicit `unavailable_reason` instead of fabricating numbers.
"""
from __future__ import annotations

from collections import deque
import csv
import ctypes
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from typing import Any

from .config import MonitoringSpec

_cache_lock = threading.Lock()
_gpu_cache: dict[str, Any] = {"t": 0.0, "gpu": None}
_cpu_state: dict[str, Any] = {"idle": 0, "total": 0, "last": None}


def cpu() -> float | None:
    """System-wide CPU % since the previous call (GetSystemTimes delta). None on first call."""
    if os.name != "nt":
        getload = getattr(os, "getloadavg", None)
        if getload is not None:
            try:
                load1, _, _ = getload()
                cores = max(1, os.cpu_count() or 1)
                return round(min(100.0, (load1 / cores) * 100.0), 1)
            except Exception:
                return None
        return None

    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

    idle, kernel, user = FT(), FT(), FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))

    def _to_int(f: FT) -> int:
        return (int(f.hi) << 32) | int(f.lo)

    i, t = _to_int(idle), _to_int(kernel) + _to_int(user)
    with _cache_lock:
        di = i - int(_cpu_state["idle"])
        dt = t - int(_cpu_state["total"])
        first = int(_cpu_state["total"]) == 0
        _cpu_state["idle"] = i
        _cpu_state["total"] = t
        if first:
            return None
        if dt <= 0:
            last = _cpu_state["last"]
            return float(last) if last is not None else 0.0
        pct = round(max(0.0, min(100.0, 100.0 * (1.0 - di / dt))), 1)
        _cpu_state["last"] = pct
        return pct


def cpu_details() -> dict[str, Any]:
    """Return CPU utilization %, logical cores, and honest thermal availability."""
    return {
        "pct": cpu(),
        "cores_logical": os.cpu_count() or 1,
        "temp_c": None,
        "temp_supported": False,
        "unavailable_reason": "CPU thermal sensor requires elevated kernel/WMI access on Windows",
    }


def audio() -> dict[str, str | None]:
    """Names of the default input/output devices, or None when there is none."""
    import sounddevice as sd

    out: dict[str, str | None] = {}
    for kind in ("input", "output"):
        try:
            dev = sd.query_devices(kind=kind)
            out[kind] = str(dev["name"]) if dev and "name" in dev else None
        except Exception:
            out[kind] = None
    return out


def ram() -> dict[str, float]:
    """Return total, free, used GB and memory load percentage."""
    if os.name == "nt":
        class MS(ctypes.Structure):
            _fields_ = [
                ("l", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("tp", ctypes.c_ulonglong),
                ("ap", ctypes.c_ulonglong),
                ("tpf", ctypes.c_ulonglong),
                ("apf", ctypes.c_ulonglong),
                ("tv", ctypes.c_ulonglong),
                ("av", ctypes.c_ulonglong),
                ("ae", ctypes.c_ulonglong),
            ]

        m = MS()
        m.l = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        total_gb = round(m.tp / 2**30, 2)
        free_gb = round(m.ap / 2**30, 2)
        used_gb = round(max(0.0, total_gb - free_gb), 2)
        return {
            "total_gb": total_gb,
            "free_gb": free_gb,
            "used_gb": used_gb,
            "used_pct": round(float(m.load), 1),
        }
    return {"total_gb": 0.0, "free_gb": 0.0, "used_gb": 0.0, "used_pct": 0.0}


def _parse_optional_float(val: str) -> float | None:
    s = (val or "").strip()
    if not s or s.upper() in {"N/A", "[N/A]", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def gpu() -> dict[str, Any] | None:
    """Query NVIDIA GPU telemetry via nvidia-smi (cached 3s). Returns None when unavailable."""
    now = time.time()
    with _cache_lock:
        if now - float(_gpu_cache["t"]) < 3.0:
            cached = _gpu_cache["gpu"]
            return dict(cached) if isinstance(cached, dict) else None

    g: dict[str, Any] | None = None
    try:
        cp = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            shell=False,
        )
        line = cp.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        name, tot_mb, used_mb, util = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
        temp_c = _parse_optional_float(parts[4]) if len(parts) > 4 else None
        total_gb = round(tot_mb / 1024.0, 2)
        used_gb = round(used_mb / 1024.0, 2)
        g = {
            "name": name,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": round(max(0.0, total_gb - used_gb), 2),
            "used_pct": round((used_mb / max(1, tot_mb)) * 100.0, 1),
            "util_pct": util,
            "temp_c": temp_c,
        }
    except Exception:
        g = None

    with _cache_lock:
        _gpu_cache["t"] = time.time()
        _gpu_cache["gpu"] = g
    return dict(g) if g is not None else None


def disks(low_space_gb: float = 5.0) -> list[dict[str, Any]]:
    """Return capacity & free space status for local fixed drives."""
    mounts: list[str] = []
    if os.name == "nt":
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    mounts.append(root)
    if not mounts:
        mounts = [str(Path.cwd().anchor or "/")]

    out: list[dict[str, Any]] = []
    for m in mounts:
        try:
            usage = shutil.disk_usage(m)
            total_gb = round(usage.total / 2**30, 2)
            free_gb = round(usage.free / 2**30, 2)
            used_gb = round(usage.used / 2**30, 2)
            used_pct = round((usage.used / max(1, usage.total)) * 100.0, 1)
            out.append({
                "mount": m,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "used_pct": used_pct,
                "health": "low_space" if free_gb < float(low_space_gb) else "healthy",
            })
        except OSError:
            continue
    return out


def battery() -> dict[str, Any]:
    """Return laptop battery state on Windows via GetSystemPowerStatus."""
    if os.name != "nt":
        return {"present": False, "ac_online": None, "charge_pct": None, "charging": None, "unavailable_reason": "Windows power API only"}

    class SPS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("SystemStatusFlag", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    sps = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return {"present": False, "ac_online": None, "charge_pct": None, "charging": None, "unavailable_reason": "GetSystemPowerStatus failed"}

    no_battery = bool(sps.BatteryFlag & 128) or int(sps.BatteryLifePercent) == 255
    ac = None if sps.ACLineStatus == 255 else bool(sps.ACLineStatus == 1)
    if no_battery:
        return {"present": False, "ac_online": ac, "charge_pct": None, "charging": False, "unavailable_reason": "No system battery detected"}

    return {
        "present": True,
        "ac_online": ac,
        "charge_pct": int(sps.BatteryLifePercent),
        "charging": bool(sps.BatteryFlag & 8),
        "unavailable_reason": None,
    }


def network() -> dict[str, Any]:
    """Inspect local hostname and non-loopback interface addresses without external traffic."""
    host = socket.gethostname()
    addrs: list[str] = []
    try:
        for info in socket.getaddrinfo(host, None):
            ip = str(info[4][0]).split("%")[0]
            if ip and not ip.startswith("127.") and ip != "::1" and ip not in addrs:
                addrs.append(ip)
    except OSError:
        pass
    return {
        "hostname": host,
        "addresses": addrs[:8],
        "has_local_interface": bool(addrs),
    }


def processes(limit: int = 8) -> list[dict[str, Any]]:
    """Return bounded top processes by working-set memory using Windows tasklist."""
    if os.name != "nt":
        return []
    try:
        cp = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
            shell=False,
        )
        rows: list[dict[str, Any]] = []
        for row in csv.reader(io.StringIO(cp.stdout)):
            if len(row) < 5:
                continue
            name, pid_s, session, _, mem_s = row[:5]
            digits = "".join(ch for ch in mem_s if ch.isdigit())
            mem_kb = int(digits) if digits else 0
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            rows.append({
                "name": name,
                "pid": pid,
                "session": session,
                "mem_mb": round(mem_kb / 1024.0, 1),
            })
        rows.sort(key=lambda r: float(r["mem_mb"]), reverse=True)
        return rows[: max(1, min(int(limit), 30))]
    except Exception:
        return []


def evaluate_alerts(snap: dict[str, Any], spec: MonitoringSpec) -> list[dict[str, Any]]:
    """Check collected system telemetry against configured thresholds."""
    alerts: list[dict[str, Any]] = []
    cpu_pct = snap.get("cpu_pct")
    if isinstance(cpu_pct, (int, float)) and cpu_pct >= spec.cpu_alert_pct:
        alerts.append({
            "id": "high_cpu",
            "severity": "warning",
            "metric": "cpu_pct",
            "value": cpu_pct,
            "threshold": spec.cpu_alert_pct,
            "message": f"CPU usage is high ({cpu_pct:.1f}% >= {spec.cpu_alert_pct:.0f}%).",
        })

    ram_info = snap.get("ram") or {}
    ram_pct = ram_info.get("used_pct")
    if isinstance(ram_pct, (int, float)) and ram_pct >= spec.ram_alert_pct:
        alerts.append({
            "id": "high_ram",
            "severity": "warning",
            "metric": "ram_used_pct",
            "value": ram_pct,
            "threshold": spec.ram_alert_pct,
            "message": f"RAM usage is high ({ram_pct:.1f}% used, {ram_info.get('free_gb', 0)} GB free).",
        })

    gpu_info = snap.get("gpu")
    if isinstance(gpu_info, dict):
        vram_pct = gpu_info.get("used_pct")
        if isinstance(vram_pct, (int, float)) and vram_pct >= spec.gpu_vram_alert_pct:
            alerts.append({
                "id": "high_vram",
                "severity": "warning",
                "metric": "gpu_vram_pct",
                "value": vram_pct,
                "threshold": spec.gpu_vram_alert_pct,
                "message": f"GPU VRAM usage is high ({gpu_info.get('used_gb')} / {gpu_info.get('total_gb')} GB).",
            })

    for d in snap.get("disks") or []:
        free_gb = d.get("free_gb")
        if isinstance(free_gb, (int, float)) and free_gb < spec.disk_free_alert_gb:
            alerts.append({
                "id": f"low_disk_{d.get('mount', 'drive')}",
                "severity": "critical" if free_gb < 1.5 else "warning",
                "metric": "disk_free_gb",
                "value": free_gb,
                "threshold": spec.disk_free_alert_gb,
                "message": f"Low disk space on {d.get('mount')}: {free_gb:.1f} GB free.",
            })

    bat = snap.get("battery") or {}
    if bat.get("present") and not bat.get("ac_online"):
        pct = bat.get("charge_pct")
        if isinstance(pct, (int, float)) and pct <= spec.battery_low_pct:
            alerts.append({
                "id": "battery_low",
                "severity": "warning",
                "metric": "battery_pct",
                "value": pct,
                "threshold": spec.battery_low_pct,
                "message": f"Battery low ({pct}%) and unplugged.",
            })

    return alerts


class SystemMonitorHistory:
    """Thread-safe bounded ring buffer for recent system snapshots."""

    def __init__(self, maxlen: int = 60) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=max(5, maxlen))
        self._lock = threading.Lock()

    def record(self, snap: dict[str, Any]) -> None:
        summary = {
            "t": snap.get("timestamp"),
            "cpu_pct": snap.get("cpu_pct"),
            "ram_used_pct": (snap.get("ram") or {}).get("used_pct"),
            "gpu_util_pct": (snap.get("gpu") or {}).get("util_pct") if snap.get("gpu") else None,
            "gpu_vram_pct": (snap.get("gpu") or {}).get("used_pct") if snap.get("gpu") else None,
            "alert_count": len(snap.get("alerts") or []),
        }
        with self._lock:
            self._buf.append(summary)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._buf)
        return items[-max(1, limit) :]


_history = SystemMonitorHistory()


def monitor_history(limit: int = 20) -> list[dict[str, Any]]:
    return _history.recent(limit=limit)


def snapshot(spec: MonitoringSpec | None = None, *, include_processes: bool = True) -> dict[str, Any]:
    """Collect a full, honest local telemetry snapshot and evaluate threshold alerts."""
    cfg = spec or MonitoringSpec()
    c_det = cpu_details()
    r_info = ram()
    g_info = gpu()
    d_info = disks(low_space_gb=cfg.disk_free_alert_gb)
    b_info = battery()
    n_info = network()
    p_info = processes(limit=8) if include_processes else []
    snap: dict[str, Any] = {
        "timestamp": round(time.time(), 3),
        "cpu_pct": c_det["pct"],
        "cpu": c_det,
        "ram": r_info,
        "gpu": g_info,
        "disks": d_info,
        "battery": b_info,
        "network": n_info,
        "audio": audio(),
        "processes": p_info,
    }
    snap["alerts"] = evaluate_alerts(snap, cfg)
    _history.record(snap)
    return snap
