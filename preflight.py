"""Preflight: runs every LIVE chain end to end against the RUNNING system and prints a verdict (not unit tests, not mocks).
Run: .\\start-jarvis.ps1 ; .venv\\Scripts\\python preflight.py   Exit 1 on any FAIL. Missing voice/ears models are WARN."""
import ctypes
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
BASE = f"http://127.0.0.1:{CFG['port']}"
results = []
VIEWER_FILES = ("index.html", "style.css", "js/api.js", "js/md.js", "js/galaxy.js", "js/panels.js", "js/learning.js", "js/main.js")


def check(status, name, detail=""):
    results.append(status)
    print(f"[{status:4}] {name}: {detail}")


def req(path, body=None, raw=None, timeout=300, method=None, base=BASE):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    r = urllib.request.Request(base + path, data=data, headers={"X-Jarvis": "1", "Content-Type": "application/json"},
                               method=method or ("POST" if data is not None else "GET"))
    return urllib.request.urlopen(r, timeout=timeout).read()


def jreq(*a, **k):
    return json.loads(req(*a, **k))


def free_ram_gb():
    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("ae", ctypes.c_ulonglong)]
    m = MS()
    m.l = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ap / 2**30, m.tp / 2**30


def parse_vram(csv_line):
    """'GTX 1650, 4096, 521' -> (name, total_gb, used_gb)"""
    name, total, used = [p.strip() for p in csv_line.split(",")]
    return name, int(total) / 1024, int(used) / 1024


def words(s):
    return set(re.sub(r"[^a-z ]", "", s.lower()).split())


def main():
    v = sys.version_info
    check("PASS" if v >= (3, 11) and sys.prefix != sys.base_prefix else "FAIL", "python+venv", f"{sys.version.split()[0]} in {sys.prefix}")
    for mod in ("fastapi", "uvicorn", "httpx", "faster_whisper", "piper", "openwakeword", "sounddevice"):
        try:
            __import__(mod)
            check("PASS", f"import {mod}")
        except ImportError as e:
            check("FAIL", f"import {mod}", str(e))
    free, total = free_ram_gb()
    check("PASS" if free >= CFG["min_free_ram_gb"] else "WARN", "ram", f"{free:.1f} GB free of {total:.1f} GB")
    check("PASS" if shutil.disk_usage(ROOT).free > 5 * 2**30 else "WARN", "disk", f"{shutil.disk_usage(ROOT).free / 2**30:.1f} GB free")
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, check=True).stdout.strip().splitlines()[0]
        n, tot, used = parse_vram(o)
        # used VRAM is mostly the loaded model itself, so only the card's size is judged here
        check("PASS" if tot >= CFG["min_vram_gb"] else "WARN", "gpu", f"{n}: {used:.1f}/{tot:.1f} GB VRAM used")
    except Exception as e:
        check("WARN", "gpu", f"nvidia-smi unavailable ({e})")

    # --- the server and the viewer
    try:
        h = jreq("/api/health")
        check("PASS", "server up", f"{BASE}  status={h['status']}")
    except Exception as e:
        check("FAIL", "server up", f"{e}. Start it: .\\start-jarvis.ps1")
        return summary(None)
    served_ok = True
    for f in VIEWER_FILES:
        served_ok &= hashlib.sha256(req("/" + f)).digest() == hashlib.sha256((ROOT / "viewer" / f).read_bytes()).digest()
    check("PASS" if served_ok else "FAIL", "served files match disk", ", ".join(VIEWER_FILES))
    leaks = []
    for p in ("/config.json", "/preflight.py", "/server.py", "/data/history.jsonl", "/.env", "/notes/learning/Active%20Recall.md"):
        try:
            req(p)
            leaks.append(p)
        except urllib.error.HTTPError:
            pass
    check("FAIL" if leaks else "PASS", "private files not reachable from browser", f"leaked: {leaks}" if leaks else "config.json, source, data, notes all 404")
    g = jreq("/api/graph")
    check("PASS" if g["nodes"] else "FAIL", "graph data", f"{len(g['nodes'])} nodes, {len(g['links'])} links")
    bad = [f for f in VIEWER_FILES if re.search(r"sk-[A-Za-z0-9]{16,}|PUT-YOUR-KEY|api[_-]?key\s*[:=]\s*[\"'][^\"']{8,}", (ROOT / "viewer" / f).read_text(encoding="utf-8"))]
    check("FAIL" if bad else "PASS", "no cloud key under viewer/", str(bad) if bad else "clean")

    # --- Ollama
    spec = CFG["brains"][CFG["boot_brain"]]
    host = spec["base_url"].rsplit("/v1", 1)[0]
    try:
        tags = [m["name"] for m in json.loads(req("/api/tags", base=host))["models"]]
        if spec["model"] in tags:
            check("PASS", "ollama + configured brain", f"{spec['model']} in {tags}")
        else:
            check("FAIL", "ollama + configured brain", f"missing. Run: ollama pull {spec['model']}")
    except Exception as e:
        check("FAIL", "ollama", str(e))
    lst = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-NetTCPConnection -LocalPort 11434 -State Listen -EA 0 | Select -Expand LocalAddress) -join ','"],
                         capture_output=True, text=True).stdout.strip()
    exposed = lst and not all(a in ("127.0.0.1", "::1") for a in lst.split(","))
    check("WARN" if exposed else "PASS", "ollama bind address", f"listening on {lst} - reachable from your LAN/Tailscale without auth; set OLLAMA_HOST=127.0.0.1:11434"
          if exposed else f"loopback only ({lst})")
    sl = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-NetTCPConnection -LocalPort {CFG['port']} -State Listen -EA 0 | Select -Expand LocalAddress) -join ','"],
                        capture_output=True, text=True).stdout.strip()
    check("PASS" if sl and all(a in ("127.0.0.1", "::1") for a in sl.split(",")) else "FAIL", "jarvis bind address", f"{sl}")

    # --- chat (think)
    t = time.time()
    c = jreq("/api/chat", {"message": "How much VRAM do I need to run a local language model?"})
    think = time.time() - t
    ok = c.get("answer") and isinstance(c.get("nodes"), list) and c["nodes"]
    check("PASS" if ok else "FAIL", "/chat answer from notes", f"{think:.1f}s ttft={c.get('ttft')} {c.get('tps')} tok/s nodes={c.get('nodes')} -> {c.get('answer', '')[:80]!r}")
    leak = re.match(r"\s*(okay|hmm|<think|the user)", c.get("answer", ""), re.I)
    check("FAIL" if leak else "PASS", "no reasoning leak in answer")

    # --- voice (speak) / ears (hear) / THE LOOP
    speak_s = hear_s = None
    sentence = "Good afternoon sir the local voice is live"
    try:
        t = time.time()
        wav = req("/api/tts", {"text": f"{sentence} at {int(time.time()) % 100000}."})  # unique text: bypass the TTS cache, measure real synthesis
        speak_s = time.time() - t
        check("PASS" if wav[:4] == b"RIFF" else "FAIL", "/tts returns WAV", f"{len(wav)} bytes in {speak_s:.2f}s")
        h = jreq("/api/voice/hear", raw=wav)
        hear_s = h["seconds"]
        got = words(h["text"]) & words(sentence)
        # one misheard word is tolerated (small.en on a synthetic voice); two or more means ears and voice disagree
        check("PASS" if len(got) >= 7 else "FAIL", "THE LOOP tts -> hear", f"{h['text']!r} ({len(got)}/8 words, hear {hear_s}s)")
    except urllib.error.HTTPError as e:
        check("WARN", "voice/ears unavailable", f"{e.code} {e.read()[:100]!r}")

    # --- memory
    try:
        r = jreq("/api/memory", {"text": "preflight probe zebrafruit quokka", "title": "Preflight probe zebrafruit"})
        found = jreq("/api/memory/search?q=zebrafruit")["results"]
        on_disk = (ROOT / "notes" / r["path"]).exists()
        check("PASS" if on_disk and found and found[0]["path"] == r["path"] else "FAIL", "/memory writes file and is searchable immediately", r["path"])
        trash = jreq(f"/api/notes/{[n for n in jreq('/api/graph')['nodes'] if n['path'] == r['path']][0]['id']}", method="DELETE")["moved_to"]
        (ROOT / "notes" / trash).unlink()  # remove only the probe's own file
    except Exception as e:
        check("FAIL", "/memory", str(e))

    # --- microphone (real device, opened and released)
    try:
        m = jreq("/api/voice/mic", {"enabled": True})
        time.sleep(1.5)
        st = jreq("/api/status")["voice"]["state"]
        jreq("/api/voice/mic", {"enabled": False})
        check("PASS" if m.get("error") is None and st == "listening" else "WARN", "microphone opens and releases", f"state while on={st}, error={m.get('error')}")
    except Exception as e:
        check("WARN", "microphone", str(e))

    # --- Phase 18 personal assistant, workspace, permissions, and learning pipeline checks
    try:
        mem = jreq("/api/memory/items")
        check("PASS" if "items" in mem else "FAIL", "personal memory store", f"{len(mem.get('items', []))} items")
        fs = jreq("/api/files/search?q=")
        check("PASS" if "matches" in fs else "FAIL", "safe filesystem search", f"{len(fs.get('matches', []))} files in allowed roots")
        # Permission enforcement check (outside root must return 403/400)
        try:
            jreq("/api/files/read?path=C:/Windows/System32/drivers/etc/hosts")
            check("FAIL", "filesystem traversal guard", "outside root was allowed")
        except urllib.error.HTTPError as he:
            check("PASS" if he.code in (400, 403, 404) else "FAIL", "filesystem traversal guard", f"blocked with HTTP {he.code}")

        v = jreq("/api/voices")
        check("PASS" if v.get("voices") else "WARN", "Piper voice discovery", f"{len(v.get('voices', []))} voices (active={v.get('active')})")

        ov = jreq("/api/learning/overview")
        hw = ov.get("hardware") or {}
        caps = hw.get("capabilities") or {}
        check("PASS", "learning overview & knowledge index", f"chunks={ov.get('knowledge', {}).get('kb_chunks', 0)}, sources={ov.get('knowledge_sources_count', 0)}")
        check(
            "PASS" if caps.get("ready_for_peft_lora") else "WARN",
            "optional LoRA training runtime",
            "ready" if caps.get("ready_for_peft_lora") else f"missing optional pkgs: {', '.join(caps.get('missing_required', []))}; RAG + Feedback active",
        )
        reg = ov.get("registry") or {}
        check("PASS" if reg.get("active_version_id") else "FAIL", "model registry & baseline pointer", f"active={reg.get('active_version_id')}")
    except Exception as e:
        check("FAIL", "workspace & learning subsystem", str(e))

    # --- network drill
    import drill
    ok, _ = drill.run()
    check("PASS" if ok else "FAIL", "offline drill (no non-local host)")
    return summary((think, speak_s, hear_s))


def summary(b):
    p, f, w = (results.count(x) for x in ("PASS", "FAIL", "WARN"))
    if b and b[1] is not None:
        print(f"\nLATENCY BUDGET: hear {b[2]:.1f}s (5s of speech, CPU Whisper {CFG['stt']['model']}), think {b[0]:.1f}s (full /chat turn incl. retrieval), "
              f"speak {b[1]:.1f}s (Piper synth, one sentence) -> total {b[0] + b[1] + b[2]:.1f}s")
    else:
        print("\nLATENCY BUDGET: not measured")
    print(f"SUMMARY: {p} pass, {f} fail, {w} warn")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
