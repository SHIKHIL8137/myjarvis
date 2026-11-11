"""Scoped short-lived sandbox model gateway tokens & restricted egress proxy (never exposes host API keys)."""
from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from .. import audit, netsec

log = logging.getLogger("jarvis.gateway.sandbox")


@dataclass
class ScopedGatewayToken:
    token: str
    run_id: str
    allowed_models: tuple[str, ...]
    allow_ollama: bool
    expires_at: float
    revoked: bool = False
    created_at: float = field(default_factory=time.time)


class ScopedGatewayTokenManager:
    """Issues short-lived, single-run tokens so sandboxed agents access ONLY approved models without host API keys."""

    def __init__(self, max_logs: int = 200):
        self._lock = threading.Lock()
        self._tokens: dict[str, ScopedGatewayToken] = {}
        self._audit_logs: list[dict] = []
        self._max_logs = max_logs

    def issue(
        self,
        *,
        run_id: str,
        allowed_models: list[str] | None = None,
        allow_ollama: bool = False,
        ttl_s: float = 300.0,
    ) -> ScopedGatewayToken:
        tok_str = f"jgw_{secrets.token_urlsafe(24)}"
        models = tuple(m.strip() for m in (allowed_models or []) if m.strip())
        now = time.time()
        rec = ScopedGatewayToken(
            token=tok_str,
            run_id=run_id or f"run-{int(now)}",
            allowed_models=models,
            allow_ollama=bool(allow_ollama),
            expires_at=now + max(10.0, float(ttl_s)),
            created_at=now,
        )
        with self._lock:
            self._tokens[tok_str] = rec
            # prune expired
            expired = [k for k, v in self._tokens.items() if now > v.expires_at + 60.0]
            for k in expired:
                self._tokens.pop(k, None)
        return rec

    def validate(self, bearer_token: str, requested_model: str | None = None) -> ScopedGatewayToken:
        raw = (bearer_token or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            raise PermissionError("Missing scoped gateway bearer token.")

        with self._lock:
            matched: ScopedGatewayToken | None = None
            for k, v in self._tokens.items():
                if hmac.compare_digest(k, raw):
                    matched = v
                    break
        if matched is None:
            raise PermissionError("Invalid sandbox gateway token.")
        if matched.revoked:
            raise PermissionError(f"Sandbox gateway token for run {matched.run_id!r} has been revoked.")
        if time.time() > matched.expires_at:
            raise PermissionError(f"Sandbox gateway token for run {matched.run_id!r} expired.")
        if requested_model and matched.allowed_models and requested_model not in matched.allowed_models:
            raise PermissionError(f"Model {requested_model!r} is not permitted for sandbox run {matched.run_id!r}.")
        return matched

    def revoke_run(self, run_id: str) -> int:
        cnt = 0
        with self._lock:
            for v in self._tokens.values():
                if v.run_id == run_id and not v.revoked:
                    v.revoked = True
                    cnt += 1
        return cnt

    def record_request_metadata(self, *, run_id: str, model: str, status: str, latency_ms: float = 0.0, token_count: int = 0) -> None:
        """Log gateway telemetry ONLY — never stores prompts, completions, or secret headers."""
        entry = {
            "ts": round(time.time(), 3),
            "run_id": run_id,
            "model": model,
            "status": status,
            "latency_ms": round(latency_ms, 1),
            "tokens": int(token_count),
        }
        with self._lock:
            self._audit_logs.append(entry)
            if len(self._audit_logs) > self._max_logs:
                self._audit_logs = self._audit_logs[-self._max_logs:]

    def recent_logs(self, run_id: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._audit_logs)
        if run_id:
            items = [it for it in items if it["run_id"] == run_id]
        return items


class RestrictedEgressProxy:
    """Fail-closed domain-allowlist HTTP/CONNECT proxy enforcing `allowed_domains` and SSRF IP blocks."""

    def __init__(
        self,
        *,
        allowed_domains: list[str] | tuple[str, ...],
        allow_ollama: bool = False,
        ollama_host: str = "127.0.0.1",
        ollama_port: int = 11434,
        bind_host: str = "127.0.0.1",
    ):
        self.allowed_domains = {d.strip().lower() for d in allowed_domains if d.strip()}
        self.allow_ollama = bool(allow_ollama)
        self.ollama_host = ollama_host.lower()
        self.ollama_port = int(ollama_port)
        self.bind_host = bind_host
        self.logs: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def is_target_allowed(self, host: str, port: int) -> tuple[bool, str]:
        h = (host or "").strip().lower().strip("[]")
        if not h:
            return False, "empty_host"
        if h in ("127.0.0.1", "localhost", "::1", self.ollama_host) and int(port) == self.ollama_port:
            if self.allow_ollama:
                return True, "ollama_explicitly_allowed"
            return False, "ollama_access_disabled_for_sandbox"
        if not any(h == d or h.endswith("." + d) for d in self.allowed_domains):
            return False, f"domain_not_in_allowlist:{h}"
        try:
            if int(port) == 443:
                netsec.resolve_and_validate(f"https://{h}/")
            else:
                infos = socket.getaddrinfo(h, int(port), type=socket.SOCK_STREAM)
                ips = {str(info[4][0]) for info in infos}
                if not ips or not all(audit.is_public_ip(ip) for ip in ips):
                    return False, "ssrf_non_public_ip"
        except Exception as e:
            return False, f"ssrf_blocked:{e}"
        return True, "allowed_domain"

    def _log(self, host: str, port: int, allowed: bool, reason: str) -> None:
        with self._lock:
            self.logs.append({"ts": round(time.time(), 3), "host": host, "port": int(port), "allowed": allowed, "reason": reason})

    def start(self) -> str:
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_CONNECT(self):
                host, _, p_str = self.path.partition(":")
                port = int(p_str or 443)
                ok, reason = proxy.is_target_allowed(host, port)
                proxy._log(host, port, ok, reason)
                if not ok:
                    self.send_error(403, f"Blocked by Jarvis sandbox egress policy ({reason})")
                    return
                try:
                    remote = socket.create_connection((host, port), timeout=10)
                except OSError as e:
                    self.send_error(502, f"Upstream connect error: {e}")
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                conns = [self.connection, remote]
                try:
                    while True:
                        rlist, _, _ = select.select(conns, [], [], 30.0)
                        if not rlist:
                            break
                        for sock in rlist:
                            data = sock.recv(8192)
                            if not data:
                                return
                            target = remote if sock is self.connection else self.connection
                            target.sendall(data)
                finally:
                    remote.close()

            def _handle_http(self):
                parts = urlsplit(self.path)
                host = parts.hostname or ""
                port = parts.port or (443 if parts.scheme == "https" else 80)
                ok, reason = proxy.is_target_allowed(host, port)
                proxy._log(host, port, ok, reason)
                if not ok:
                    body = json.dumps({"error": f"Blocked by Jarvis sandbox egress policy ({reason})"}).encode("utf-8")
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(501, "Plain HTTP forwarding outside CONNECT disabled for sandbox safety")

            do_GET = _handle_http
            do_POST = _handle_http
            do_PUT = _handle_http
            do_DELETE = _handle_http

        self._server = ThreadingHTTPServer((self.bind_host, 0), _Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.bind_host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

