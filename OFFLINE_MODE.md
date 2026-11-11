# Offline & Controlled Network Mode

Jarvis runs **local-first** and defaults to **Local Mode** (`offline_enforced: true`, `internet.enabled: false`).

## Two Operating Modes

| Mode | LLM / Voice / Notes | Outbound Network | How to Select |
|---|---|---|---|
| **Local Mode** (default) | 100% local (`qwen3:4b-instruct`, Whisper, Piper, openWakeWord) | **Blocked**: `jarvis/audit.py` raises `ConnectionError` on any non-loopback `socket.connect` or `socket.getaddrinfo` | Default at install; HUD `INTERNET` = `off` |
| **Online Assisted Mode** (opt-in) | Still 100% local (`qwen3:4b-instruct`, local speech, local notes) | **Scoped & guarded**: only `jarvis/netsec.py` may call allowlisted HTTPS search providers or `robots.txt`-checked public `https://` pages; arbitrary sockets stay blocked | Toggle HUD `INTERNET` = `on`, or enable in **System → Internet & Web Search** |

## What enforces security in both modes

1. **Process-wide audit hook (`jarvis/audit.py`)**: `sys.addaudithook` cannot be removed at runtime. Even when Online Assisted Mode is turned on, `offline_enforced` stays active: any code or third-party library trying to connect outside `netsec.safe_request` is still blocked and logged.
2. **Browser CSP (`jarvis/api.py`)**: `connect-src 'self'` guarantees the browser never talks to external hosts. All web searches are executed by the backend and returned as attributed links.
3. **SSRF & DNS-rebinding protection (`jarvis/netsec.py`)**:
   - Only `https://` on port `443` (no `http://`, `file://`, URL credentials, or private ports).
   - Host allowlist enforced for search providers.
   - Blocks loopback, RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local/metadata (`169.254/16`), CGNAT (`100.64/10`), and IPv6 private/mapped equivalents.
   - Resolves DNS once, validates every IP, and pins TCP connection directly to the validated IP (preserving TLS SNI & certificate checks) so DNS rebinding cannot occur.
   - Disables automatic redirects (`follow_redirects=False`) and re-validates every hop.
   - Enforces `trust_env=False` (ignores proxy env vars), timeouts, and download size caps.
4. **API keys outside config**: Keys are stored only in env vars or gitignored `data/secrets.json`, redacted in logs, and never returned to the browser.
5. **External audit log**: Every allowed and blocked outbound attempt is logged to `logs/external_audit.jsonl` (with secrets stripped).

## Returning to strict Offline Mode

Click **INTERNET** on the HUD (`on` → `off`) or uncheck **Online Assisted Mode** in System settings and click **Save**. Outbound scopes are immediately disabled, and `drill.py` can verify zero remote hosts are contacted.

## Run the offline test

1. Turn Wi-Fi / Ethernet / Tailscale off.
2. Run:
   ```powershell
   cd D:\projects\myjarvis
   .\offline-test.ps1
   ```
   Output is saved to `logs\offline-test.txt`.

## Separate from Jarvis: Ollama bind address

`OLLAMA_HOST` is configured as `127.0.0.1:11434` (User environment) so Ollama listens exclusively on loopback (`127.0.0.1:11434`, verified via `Get-NetTCPConnection` and `netstat -ano`). If you ever need to re-apply or check it:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', '127.0.0.1:11434', 'User')
# then quit Ollama from the system tray and start it again
```
