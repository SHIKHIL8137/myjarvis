"""Offline drill: proves a conversation turn never leaves this machine. Needs Jarvis running (start-jarvis.ps1).
Three independent witnesses: (1) the server's in-process connection log, (2) OS-level netstat samples of the server
process during the turn, (3) a static scan of the viewer for external URLs. Passes only if every remote list is empty.
Also self-tests the watcher (it must catch and block a connection to api.anthropic.com) and prints THE HONEST LIST."""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
BASE = f"http://127.0.0.1:{CFG['port']}"
HDR = {"X-Jarvis": "1", "Content-Type": "application/json"}
LOCAL = ("127.0.0.1", "::1", "0.0.0.0", "[::1]", "[::]", "::")


def call(path, body=None, raw=None, timeout=300):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, headers=HDR, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout).read()


def server_pids() -> set[int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetTCPConnection -LocalPort {CFG['port']} -State Listen -EA 0).OwningProcess"], capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


class NetSampler(threading.Thread):
    """Samples `netstat -ano` every 0.15 s and records remote endpoints owned by the server process."""
    def __init__(self, pids):
        super().__init__(daemon=True)
        self.pids, self.seen, self.stop_ev = {str(p) for p in pids}, set(), threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
            for ln in out.splitlines():
                p = ln.split()
                if len(p) >= 5 and p[0] == "TCP" and p[-1] in self.pids and p[1].split(":")[0] not in ("0.0.0.0", "[::]"):
                    self.seen.add(p[2].rsplit(":", 1)[0])
            time.sleep(0.15)


def honest_list() -> list[str]:
    """Features that need the internet, read from the code (URLs that are not loopback) plus the setup-time downloads."""
    found = []
    for f in [ROOT / "server.py", *sorted((ROOT / "jarvis").glob("*.py"))]:
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for u in re.findall(r"https?://[^\s\"')]+", ln):
                if not re.match(r"https?://(localhost|127\.0\.0\.1|testserver)", u) and not ln.strip().startswith(("#", '"""')):
                    found.append(f"runtime URL in code: {f.name}:{i} {u}")
    return found + [
        "NONE at runtime: chat, speech-to-text, text-to-speech, wake word, memory and the galaxy are all local.",
        "Setup only: `pip install` (PyPI), Whisper small.en (Hugging Face), Piper voice (Hugging Face), openWakeWord models (GitHub),",
        "            `ollama pull` for models, and the 3d-force-graph file (npm) vendored once into viewer/vendor/.",
        "Not implemented, so unavailable offline AND online: live web research. Jarvis says he is offline instead of pretending.",
        "No cloud brain exists in this build; there is no cloud fallback to switch to by accident.",
    ]


def run() -> tuple[bool, list[str]]:
    out, ok = [], True

    def say(s=""):
        out.append(s)
        print(s)

    say("== THE DRILL ==")
    ext = []
    for f in ("index.html", "style.css", *[f"js/{p.name}" for p in sorted((ROOT / "viewer" / "js").glob("*.js"))]):
        t = (ROOT / "viewer" / f).read_text(encoding="utf-8")
        ext += [f"{f}: {u}" for u in re.findall(r"""(?:src|href)=["'](https?://[^"']+)|url\((https?://[^)]+)\)""", t) for u in u if u]
    say(f"static scan of viewer/: {len(ext)} external references {ext or ''}")
    ok &= not ext

    pids = server_pids()
    if not pids:
        say("FAIL: Jarvis is not running. Start it with .\\start-jarvis.ps1")
        return False, out
    t0 = time.time()
    sampler = NetSampler(pids)
    sampler.start()
    t = time.time()
    chat = json.loads(call("/api/chat", {"message": "How much VRAM do I need to run a local language model?"}))
    say(f"1. /api/chat   {time.time() - t:.1f}s  model={chat.get('model')}  answer: {chat['answer'][:90]}...")
    t = time.time()
    try:
        wav = call("/api/tts", {"text": chat["answer"][:200]})
        say(f"2. /api/tts    {time.time() - t:.1f}s  {len(wav)} bytes of WAV")
        t = time.time()
        heard = json.loads(call("/api/voice/hear", raw=wav))
        say(f"3. /api/hear   {time.time() - t:.1f}s  heard: {heard['text'][:90]}...")
    except Exception as e:
        say(f"2/3. speech chain skipped: {e}")
    time.sleep(0.5)
    sampler.stop_ev.set()
    sampler.join(2)

    conns = json.loads(call(f"/api/drill/connections?since={t0}"))
    hosts = sorted({e["host"] for e in conns["all"]})
    say(f"server-side log: {len(conns['all'])} connection events to {hosts}")
    say(f"OS-level netstat samples of server pid(s) {sorted(pids)}: remote endpoints {sorted(sampler.seen)}")
    remote_app = sorted({e["host"] for e in conns["remote"]})
    remote_os = sorted(h for h in sampler.seen if h not in LOCAL and not h.startswith("127."))
    say(f"NON-LOCAL HOSTS (server log): {remote_app}")
    say(f"NON-LOCAL HOSTS (OS level):   {remote_os}")
    ok &= not remote_app and not remote_os

    # watcher self-test: it must see AND block a cloud call (offline enforcement)
    import socket
    sys.path.insert(0, str(ROOT))
    from jarvis import audit
    audit.install(enforce=True)
    m = audit.mark()
    try:
        socket.getaddrinfo("api.anthropic.com", 443)
        say("WATCHER SELF-TEST: FAIL - a connection to api.anthropic.com was NOT blocked")
        ok = False
    except ConnectionError:
        named = [e["host"] for e in audit.since(m, only_remote=True)]
        say(f"WATCHER SELF-TEST: pass - caught and blocked {named}")
        ok &= "api.anthropic.com" in named

    say("\nTHE HONEST LIST (features that need the internet, read from the code):")
    for h in honest_list():
        say("  - " + h)
    say(f"\nDRILL {'PASSED: no host outside localhost was contacted' if ok else 'FAILED'}")
    return ok, out


if __name__ == "__main__":
    sys.exit(0 if run()[0] else 1)
