"""Outbound-connection watcher, scoped permission gate, and offline enforcer (via sys.addaudithook).

In Local Mode (internet disabled, default): ALL non-loopback socket.connect / socket.getaddrinfo targets raise ConnectionError.
In Online Assisted Mode: enforcement stays ON globally; only explicitly scoped HTTPS/allowlisted connections created inside
netsec's validated client are allowed, while all other outbound traffic remains blocked and every attempt is audited."""
from contextlib import contextmanager
import contextvars
import ipaddress
import json
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from .config import ROOT

_LOCAL = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "0.0.0.0", "", "::"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_BENCH = ipaddress.ip_network("198.18.0.0/15")
_6TO4 = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")

_lock = threading.Lock()
_events: list[dict] = []
_external_log: list[dict] = []
_state = {"installed": False, "enforce": False, "online_enabled": False}
_outbound_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_outbound_scope", default=None)


def is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    return host is None or str(host).lower() in _LOCAL


def is_public_ip(ip_str: str) -> bool:
    """True iff ip_str is a global, publicly routable unicast IP (blocks loopback, private, link-local, CGNAT, reserved)."""
    if not ip_str or "%" in ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and (ip in _CGNAT or ip in _BENCH):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (ip in _6TO4 or ip in _TEREDO):
        return False
    return True


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


def _scope_allows(event: str, host: str, port) -> bool:
    if not _state["online_enabled"]:
        return False
    scope = _outbound_scope.get()
    if not scope:
        return False
    norm_host = host.rstrip(".").lower()
    ports = scope.get("ports") or {443}
    if event == "socket.getaddrinfo":
        if norm_host not in scope.get("hosts", set()):
            return False
        return port in (None, 0, "0", 443, "443") or port in ports
    if event == "socket.connect":
        if port not in ports:
            return False
        if _is_ip_literal(norm_host):
            return norm_host in scope.get("ips", set()) and is_public_ip(norm_host)
        return norm_host in scope.get("hosts", set())
    return False


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
    host_s = str(host)
    local = is_local(host_s)
    allowed = True if local else (_scope_allows(event, host_s, port) or not _state["enforce"])
    with _lock:
        _events.append({
            "t": time.time(),
            "event": event.split(".")[1],
            "host": host_s,
            "port": port,
            "local": local,
            "allowed": allowed,
            "thread": threading.current_thread().name,
        })
        del _events[:-2000]
    if not local and _state["enforce"] and not allowed:
        mode_msg = "offline mode" if not _state["online_enabled"] else "offline guard"
        raise ConnectionError(f"{mode_msg}: blocked outbound connection to {host_s}:{port}")


def install(enforce: bool = True, online_enabled: bool = False) -> None:
    _state["enforce"] = enforce
    _state["online_enabled"] = online_enabled
    if not _state["installed"]:
        sys.addaudithook(_hook)  # cannot be removed; enforcement stays active
        _state["installed"] = True


def set_enforce(v: bool) -> None:
    _state["enforce"] = bool(v)


def set_online_enabled(v: bool) -> None:
    _state["online_enabled"] = bool(v)


def is_online_enabled() -> bool:
    return bool(_state["online_enabled"])


@contextmanager
def allow_outbound(hosts: Iterable[str] = (), ips: Iterable[str] = (), ports: Iterable[int] = (443,), purpose: str = "web"):
    """Temporarily authorize a strictly scoped outbound DNS/HTTPS target inside Online Assisted Mode."""
    if not _state["online_enabled"]:
        raise ConnectionError("offline mode: internet access is disabled")
    prev = _outbound_scope.get()
    merged_hosts = ({h.rstrip(".").lower() for h in hosts} | (prev["hosts"] if prev else set()))
    merged_ips = ({i.lower() for i in ips if is_public_ip(i)} | (prev["ips"] if prev else set()))
    merged_ports = set(ports) | (prev["ports"] if prev else set())
    token = _outbound_scope.set({"hosts": merged_hosts, "ips": merged_ips, "ports": merged_ports, "purpose": purpose})
    try:
        yield
    finally:
        _outbound_scope.reset(token)


def record_external(action: str, target: str, host: str, allowed: bool, reason: str = "", status: int | None = None,
                    bytes_read: int = 0, audit_file: Path | None = None) -> dict:
    """Append an external request audit entry in memory and to logs/external_audit.jsonl (never logging secrets)."""
    entry = {
        "t": round(time.time(), 3),
        "action": action,
        "host": host,
        "target": target,
        "allowed": allowed,
        "reason": reason,
        "status": status,
        "bytes": bytes_read,
    }
    with _lock:
        _external_log.append(entry)
        del _external_log[:-500]
    try:
        path = audit_file or (ROOT / "logs" / "external_audit.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


def external_log(limit: int = 50) -> list[dict]:
    with _lock:
        return list(_external_log[-max(1, min(limit, 500)):])


def mark() -> float:
    return time.time()


def since(t: float, only_remote: bool = False) -> list[dict]:
    with _lock:
        return [e for e in _events if e.get("t", 0.0) >= t and not (only_remote and e.get("local", True))]
