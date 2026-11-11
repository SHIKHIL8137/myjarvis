"""RAM / GPU snapshot. GPU via nvidia-smi (cached 3 s so UI refreshes never spawn processes in a loop)."""
import ctypes
import subprocess
import time

_cache = {"t": 0.0, "gpu": None}
_cpu = {"idle": 0, "total": 0, "last": None}


def cpu() -> float | None:
    """System-wide CPU % since the previous call (GetSystemTimes delta). None on the first call: no interval yet."""
    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
    idle, kernel, user = FT(), FT(), FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    val = lambda f: (f.hi << 32) | f.lo  # noqa: E731
    i, t = val(idle), val(kernel) + val(user)  # kernel time includes idle time
    di, dt, first = i - _cpu["idle"], t - _cpu["total"], _cpu["total"] == 0
    _cpu.update(idle=i, total=t)
    if first:
        return None
    if dt <= 0:
        return _cpu["last"] if _cpu["last"] is not None else 0.0
    _cpu["last"] = round(max(0.0, min(100.0, 100 * (1 - di / dt))), 1)
    return _cpu["last"]


def audio() -> dict:
    """Names of the default input/output devices, or None when there is none."""
    import sounddevice as sd
    out = {}
    for kind in ("input", "output"):
        try:
            out[kind] = sd.query_devices(kind=kind)["name"]
        except Exception:
            out[kind] = None
    return out


def ram() -> dict:
    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("ae", ctypes.c_ulonglong)]
    m = MS()
    m.l = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"total_gb": round(m.tp / 2**30, 1), "free_gb": round(m.ap / 2**30, 1)}


def gpu() -> dict | None:
    if time.time() - _cache["t"] < 3:
        return _cache["gpu"]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, check=True).stdout.strip().splitlines()[0]
        n, tot, used, util = [p.strip() for p in out.split(",")]
        g = {"name": n, "total_gb": round(int(tot) / 1024, 1), "used_gb": round(int(used) / 1024, 2), "util_pct": int(util)}
    except Exception:
        g = None
    _cache.update(t=time.time(), gpu=g)
    return g
