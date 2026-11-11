# Offline mode

## Status

| Claim | Evidence | Verified |
|---|---|---|
| The server contacts nothing but localhost during a turn | `drill.py`: in-process connection log and OS-level `netstat` samples, both empty of remote hosts | Yes, run on 2026-11-11 |
| The browser loads nothing from outside | 19 resources, all from `127.0.0.1:4700`; no external URL in `viewer/` | Yes |
| A cloud call from the server is blocked | Watcher self-test: `api.anthropic.com` caught and refused | Yes |
| Everything works with the network physically off | `offline-test.ps1` | **Not yet. You need to run it** |

The last row is open because this build session could not switch off the Wi-Fi adapter (it needs administrator rights, and it
would have cut the session itself). Until you run it, "works with the internet disconnected" is strongly supported but not proven.

## Run the offline test

1. Turn Wi-Fi off (taskbar network icon, or Airplane mode). Disconnect Ethernet and Tailscale too.
2. Run:
   ```powershell
   cd D:\projects\myjarvis
   .\offline-test.ps1
   ```
   It refuses to run if the internet is still reachable, restarts Jarvis, and runs the full live preflight and drill.
3. Open http://127.0.0.1:4700/ and ask a question whose answer is in one note, for example "How does spaced repetition work?".
   You should see the answer stream in, hear it, and see the galaxy fly to the "Spaced Repetition" star.

Output is saved to `logs\offline-test.txt`.

## What enforces it

- `offline_enforced: true` in `config.json`: a process-wide hook (`jarvis/audit.py`) raises an error on any connection or DNS
  lookup that is not loopback. There is no cloud model in the configuration and no fallback code path.
- `server.py` sets `HF_HUB_OFFLINE=1` and telemetry opt-outs; Whisper is loaded with `local_files_only`.
- The 3D library is a local file; the page's Content-Security-Policy only allows its own origin.

Limit of the hook: on Windows, asyncio can connect to a literal IP address without raising a Python audit event. The drill covers
that gap by sampling the operating system's connection table for the server process.

## What needs the internet

Nothing at runtime. Only setup does: `pip install`, `ollama pull`, and the three speech-model downloads (see INSTALLATION.md).
Web research is not implemented; asked for something that needs the web, Jarvis is instructed to say he is offline.

## Separate from Jarvis: your Ollama is exposed to the network

Ollama on this machine listens on all interfaces because the user environment variable `OLLAMA_HOST` is `0.0.0.0:11434`. It
answered on the Wi-Fi address (192.168.1.6) and the Tailscale address (100.68.21.25) without any authentication. Anyone on those
networks can use your models. Jarvis only needs localhost. To restrict it:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', '127.0.0.1:11434', 'User')
# then quit Ollama from the system tray and start it again
```

This was left unchanged because it is your system setting and other devices of yours may rely on it.
