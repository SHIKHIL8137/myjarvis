"""Outbound-connection watcher and offline enforcer (process-wide, via sys.addaudithook).
Records every socket.connect / getaddrinfo target. With enforce=True any non-loopback target raises, so the
server cannot reach the internet even by accident. Limitation: asyncio's Windows proactor connects IP literals
without a Python audit event; drill.py cross-checks with OS-level netstat samples."""
import sys
import threading
import time

_LOCAL = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "0.0.0.0", "", "::"}
_lock = threading.Lock()
_events: list[dict] = []
_state = {"installed": False, "enforce": False}


def is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    return host is None or str(host).lower() in _LOCAL


def _hook(event, args):
    if event == "socket.connect":
        addr = args[1]
        host = addr[0] if isinstance(addr, tuple) else str(addr)  # AF_UNIX path etc.
        port = addr[1] if isinstance(addr, tuple) and len(addr) > 1 else None
    elif event == "socket.getaddrinfo":
        host, port = args[0], args[1]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    local = is_local(host)
    with _lock:
        _events.append({"t": time.time(), "event": event.split(".")[1], "host": str(host), "port": port, "local": local,
                        "thread": threading.current_thread().name})
        del _events[:-2000]
    if not local and _state["enforce"]:
        raise ConnectionError(f"offline mode: blocked outbound connection to {host}:{port}")


def install(enforce: bool = True) -> None:
    _state["enforce"] = enforce
    if not _state["installed"]:
        sys.addaudithook(_hook)  # cannot be removed; enforcement is a flag
        _state["installed"] = True


def set_enforce(v: bool) -> None:
    _state["enforce"] = v


def mark() -> float:
    return time.time()


def since(t: float, only_remote: bool = False) -> list[dict]:
    with _lock:
        return [e for e in _events if e["t"] >= t and not (only_remote and e["local"])]
