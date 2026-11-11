"""Hardened HTTPS outbound client for Online Assisted Mode.

Security invariants:
1. Refuses any request unless InternetSpec.enabled AND audit.is_online_enabled() are True.
2. HTTPS only, port 443 only, no URL credentials, no localhost / private / link-local / CGNAT / reserved IPs.
3. Resolves DNS once inside a scoped audit context, validates ALL returned IPs, and pins the TCP connection directly
   to a validated IP (anti-SSRF and anti-DNS-rebinding), while preserving TLS SNI + certificate verification for the host.
4. Disables automatic redirects (`follow_redirects=False`) and re-validates every redirect hop manually.
5. Disables proxy environment variables (`trust_env=False`), enforces connect/read timeouts and max download bytes.
6. Never logs API keys or sensitive query parameters in the external audit log."""
from dataclasses import dataclass
import ipaddress
import socket
import ssl
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import anyio
import httpcore
import httpx

from . import audit
from .config import InternetSpec

_SENSITIVE_PARAMS = {"api_key", "apikey", "key", "token", "secret", "subscription-key", "auth"}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".arpa", ".home.arpa")
_BLOCKED_EXACT_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


class NetworkSecurityError(PermissionError):
    """Raised when an outbound URL, IP, redirect, or offline policy check blocks a request."""


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    sanitized_url: str
    host: str
    port: int
    ips: tuple[str, ...]
    pinned_ip: str


def sanitize_url(url: str) -> str:
    """Strip userinfo, fragments, and sensitive query keys before logging."""
    p = urlsplit(url)
    q = [(k, "REDACTED" if k.lower() in _SENSITIVE_PARAMS else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
    host = p.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if p.port and p.port != 443:
        netloc = f"{netloc}:{p.port}"
    return urlunsplit((p.scheme, netloc, p.path, urlencode(q), ""))


def _validate_url_syntax(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url.strip():
        raise NetworkSecurityError("URL must be a non-empty string")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise NetworkSecurityError("URL contains control or whitespace characters")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise NetworkSecurityError(f"only https:// URLs are permitted (got {parts.scheme or 'none'})")
    if parts.username is not None or parts.password is not None:
        raise NetworkSecurityError("URL credentials (user:pass@) are not permitted")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise NetworkSecurityError("URL is missing a hostname")
    if host in _BLOCKED_EXACT_HOSTS or any(host.endswith(s) for s in _BLOCKED_HOST_SUFFIXES):
        raise NetworkSecurityError(f"blocked internal hostname: {host}")
    port = parts.port or 443
    if port != 443:
        raise NetworkSecurityError(f"only port 443 is permitted for HTTPS requests (got {port})")
    # Reject IP literals that are non-public immediately before DNS resolution
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not audit.is_public_ip(host):
            raise NetworkSecurityError(f"blocked non-public IP address: {host}")
    return host, port


def resolve_and_validate(url: str, allowed_hosts: Iterable[str] | None = None) -> ValidatedTarget:
    """Validate URL syntax, host allowlist (when provided), resolve DNS once, and verify all IPs are public."""
    host, port = _validate_url_syntax(url)
    if allowed_hosts is not None:
        allow = {h.rstrip(".").lower() for h in allowed_hosts if h}
        if host not in allow:
            audit.record_external("policy_block", sanitize_url(url), host, False, "host not in allowlist")
            raise NetworkSecurityError(f"host {host!r} is not in the approved search-provider allowlist")

    # Reject literal IP URLs for arbitrary browsing/search (require domain names or public IP check)
    ips: list[str] = []
    try:
        ipaddress.ip_address(host)
        if not audit.is_public_ip(host):
            raise NetworkSecurityError(f"blocked non-public IP: {host}")
        ips = [host]
    except ValueError:
        try:
            with audit.allow_outbound(hosts={host}, ports={port}, purpose="dns"):
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except ConnectionError as e:
            audit.record_external("dns_block", sanitize_url(url), host, False, str(e))
            raise NetworkSecurityError(str(e)) from e
        except socket.gaierror as e:
            audit.record_external("dns_error", sanitize_url(url), host, False, str(e))
            raise NetworkSecurityError(f"DNS lookup failed for {host}: {e}") from e

        seen = set()
        for info in infos:
            ip = str(info[4][0])
            if not audit.is_public_ip(ip):
                audit.record_external("ssrf_block", sanitize_url(url), host, False, f"resolved to non-public IP {ip}")
                raise NetworkSecurityError(f"blocked connection: {host} resolved to non-public IP {ip}")
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
        if not ips:
            raise NetworkSecurityError(f"DNS returned no addresses for {host}")

    # Prefer IPv4 first when available for deterministic reliability on dual-stack Windows
    ipv4 = [i for i in ips if "." in i]
    pinned = ipv4[0] if ipv4 else ips[0]
    return ValidatedTarget(
        url=url.strip(),
        sanitized_url=sanitize_url(url.strip()),
        host=host,
        port=port,
        ips=tuple(ips),
        pinned_ip=pinned,
    )


class _PinnedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Connects directly to pre-validated public IP(s), eliminating DNS-rebinding TOCTOU."""

    def __init__(self, target: ValidatedTarget):
        self._target = target
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options=None) -> httpcore.AsyncNetworkStream:
        if host.rstrip(".").lower() != self._target.host or port != self._target.port:
            raise NetworkSecurityError(f"unexpected target {host}:{port} (expected {self._target.host}:{self._target.port})")
        if not audit.is_public_ip(self._target.pinned_ip):
            raise NetworkSecurityError(f"blocked non-public pinned IP {self._target.pinned_ip}")
        with audit.allow_outbound(hosts={self._target.host}, ips={self._target.pinned_ip}, ports={port}, purpose="https"):
            return await self._inner.connect_tcp(
                self._target.pinned_ip,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise NetworkSecurityError("unix sockets are forbidden for outbound requests")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx AsyncHTTPTransport backed by httpcore.AsyncConnectionPool with IP pinning and TLS verification."""

    def __init__(self, target: ValidatedTarget, ssl_context: ssl.SSLContext | None = None):
        super().__init__(verify=ssl_context or ssl.create_default_context(), trust_env=False, http1=True, http2=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            http1=True,
            http2=False,
            network_backend=_PinnedAsyncBackend(target),
        )


@dataclass
class SafeHTTPResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    body: bytes


async def safe_request(
    method: str,
    url: str,
    spec: InternetSpec,
    *,
    allowed_hosts: Iterable[str] | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    transport_factory=None,
) -> SafeHTTPResponse:
    """Execute a hardened HTTPS request with offline enforcement, SSRF/DNS-rebinding protection, redirect validation,
    timeouts, and download size limits. `transport_factory` is an optional test hook (httpx.MockTransport)."""
    if not spec.enabled or not audit.is_online_enabled():
        audit.record_external("offline_block", sanitize_url(url), urlsplit(url).hostname or "", False, "internet disabled")
        raise NetworkSecurityError("Jarvis is in Local Mode (internet access is disabled).")

    limit_bytes = max_bytes or spec.max_fetch_bytes
    timeout = httpx.Timeout(spec.timeout_s, connect=min(4.0, spec.timeout_s))
    req_headers = {"User-Agent": "JarvisLocalAssistant/1.0 (https://127.0.0.1)", **(headers or {})}

    current_url = url
    if params:
        current_url = str(httpx.URL(current_url, params=params))

    for hop in range(spec.max_redirects + 1):
        # Validate syntax + allowlist + DNS -> public IPs BEFORE any transport connects.
        # In unit tests that pass transport_factory (httpx.MockTransport), allow skipping live DNS when requested.
        if transport_factory is not None:
            host, _ = _validate_url_syntax(current_url)
            if allowed_hosts is not None:
                allow = {h.rstrip(".").lower() for h in allowed_hosts if h}
                if host not in allow:
                    raise NetworkSecurityError(f"host {host!r} is not in the approved search-provider allowlist")
            target = ValidatedTarget(current_url, sanitize_url(current_url), host, 443, ("93.184.216.34",), "93.184.216.34")
            transport = transport_factory(target)
        else:
            target = resolve_and_validate(current_url, allowed_hosts=allowed_hosts)
            transport = _PinnedTransport(target)

        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                req_kwargs: dict[str, Any] = {"headers": req_headers}
                if hop == 0 and json_body is not None and method.upper() == "POST":
                    req_kwargs["json"] = json_body
                async with client.stream(method if hop == 0 else "GET", current_url, **req_kwargs) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise NetworkSecurityError("redirect response missing Location header")
                        next_url = urljoin(current_url, loc)
                        # Validate redirect destination immediately (also catches redirects to http:// or private IP)
                        _validate_url_syntax(next_url)
                        audit.record_external("redirect", sanitize_url(next_url), urlsplit(next_url).hostname or "", True, f"hop {hop + 1}", resp.status_code)
                        current_url = next_url
                        # Never leak search API keys across redirects to different hosts
                        if urlsplit(next_url).hostname != target.host:
                            req_headers = {"User-Agent": req_headers["User-Agent"]}
                        continue

                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > limit_bytes:
                        audit.record_external("size_block", target.sanitized_url, target.host, False, f"content-length {cl} > {limit_bytes}", resp.status_code)
                        raise NetworkSecurityError(f"response exceeds max download size ({limit_bytes} bytes)")

                    chunks = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > limit_bytes:
                            audit.record_external("size_block", target.sanitized_url, target.host, False, f"stream > {limit_bytes}", resp.status_code, total)
                            raise NetworkSecurityError(f"response exceeded max download size ({limit_bytes} bytes)")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    audit.record_external(method.upper(), target.sanitized_url, target.host, True, f"ip={target.pinned_ip}", resp.status_code, len(body))
                    return SafeHTTPResponse(
                        status_code=resp.status_code,
                        url=current_url,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        body=body,
                    )
        except NetworkSecurityError:
            raise
        except httpx.HTTPError as e:
            audit.record_external("http_error", target.sanitized_url, target.host, False, f"{type(e).__name__}: {e}")
            raise

    audit.record_external("redirect_block", sanitize_url(current_url), urlsplit(current_url).hostname or "", False, "too many redirects")
    raise NetworkSecurityError(f"exceeded maximum redirects ({spec.max_redirects})")


async def safe_stream_lines(
    method: str,
    url: str,
    spec: InternetSpec,
    *,
    allowed_hosts: Iterable[str] | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    transport_factory=None,
):
    """Stream SSE/line-delimited HTTPS responses line-by-line under the exact same offline & SSRF protections."""
    if not spec.enabled or not audit.is_online_enabled():
        audit.record_external("offline_block", sanitize_url(url), urlsplit(url).hostname or "", False, "internet disabled")
        raise NetworkSecurityError("Jarvis is in Local Mode (internet access is disabled).")

    limit_bytes = max_bytes or (spec.max_fetch_bytes * 4)
    timeout = httpx.Timeout(spec.timeout_s, connect=min(4.0, spec.timeout_s))
    req_headers = {"User-Agent": "JarvisLocalAssistant/1.0 (https://127.0.0.1)", **(headers or {})}

    current_url = str(httpx.URL(url, params=params)) if params else url
    if transport_factory is not None:
        host, _ = _validate_url_syntax(current_url)
        if allowed_hosts is not None:
            allow = {h.rstrip(".").lower() for h in allowed_hosts if h}
            if host not in allow:
                raise NetworkSecurityError(f"host {host!r} is not in the approved allowlist")
        target = ValidatedTarget(current_url, sanitize_url(current_url), host, 443, ("93.184.216.34",), "93.184.216.34")
        transport = transport_factory(target)
    else:
        target = resolve_and_validate(current_url, allowed_hosts=allowed_hosts)
        transport = _PinnedTransport(target)

    total = 0
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            req_kwargs: dict[str, Any] = {"headers": req_headers}
            if json_body is not None and method.upper() == "POST":
                req_kwargs["json"] = json_body
            async with client.stream(method, current_url, **req_kwargs) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")[:320]
                    audit.record_external(method.upper(), target.sanitized_url, target.host, False, f"http_{resp.status_code}", resp.status_code)
                    raise NetworkSecurityError(f"HTTP {resp.status_code}: {err_body}")
                async for line in resp.aiter_lines():
                    total += len(line.encode("utf-8", errors="replace")) + 1
                    if total > limit_bytes:
                        audit.record_external("size_block", target.sanitized_url, target.host, False, f"sse > {limit_bytes}", resp.status_code, total)
                        raise NetworkSecurityError(f"stream exceeded max size ({limit_bytes} bytes)")
                    yield line
                audit.record_external(method.upper(), target.sanitized_url, target.host, True, f"stream ip={target.pinned_ip}", resp.status_code, total)
    except NetworkSecurityError:
        raise
    except httpx.HTTPError as e:
        audit.record_external("http_error", target.sanitized_url, target.host, False, f"{type(e).__name__}: {e}")
        raise


