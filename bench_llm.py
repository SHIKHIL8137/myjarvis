"""Benchmark Ollama models on this machine. stdlib only.
Run: .venv\\Scripts\\python bench_llm.py qwen3:4b-instruct qwen3:4b
Per model: unload -> load (empty prompt) -> greeting, 3-turn chat, coding question (streamed, native API)."""
import json
import re
import subprocess
import sys
import time
import urllib.request

HOST = "http://localhost:11434"
NUM_CTX = 4096
LEAK = re.compile(r"^\s*(okay|hmm|alright|let me|first,? i|the user|user (wants|asked|said)|<think>)", re.I)


def post(path, body, timeout=300, stream=False):
    req = urllib.request.Request(HOST + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=timeout)
    return (json.loads(ln) for ln in r if ln.strip()) if stream else json.loads(r.read())


def avail_ram_gb():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"], capture_output=True, text=True).stdout
    return int(out.strip()) / 2**20


def ollama_ws_gb():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-Process ollama* -ea 0 | Measure-Object WorkingSet64 -Sum).Sum"], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 2**30


def chat(model, messages, num_predict):
    t0 = time.time()
    first_any = first_content = None
    content, thinking, last = "", "", {}
    for c in post("/api/chat", {"model": model, "messages": messages, "stream": True, "think": False,
                                "options": {"num_ctx": NUM_CTX, "num_predict": num_predict, "seed": 1, "temperature": 0.3}}, stream=True):
        m = c.get("message", {})
        if m.get("thinking") and first_any is None:
            first_any = time.time() - t0
        if m.get("content"):
            first_any = first_any or time.time() - t0
            first_content = first_content or time.time() - t0
        content += m.get("content", "")
        thinking += m.get("thinking", "")
        if c.get("done"):
            last = c
    tps = last["eval_count"] / (last["eval_duration"] / 1e9)
    return dict(content=content.strip(), ttf_any=first_any, ttf_content=first_content, tps=tps,
                tokens=last["eval_count"], total=time.time() - t0, thinking_chars=len(thinking),
                leak=bool(thinking) or bool(LEAK.match(content)), truncated=last.get("done_reason") == "length")


def bench(model):
    print(f"\n===== {model} =====")
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(3)
    ram0, ws0 = avail_ram_gb(), ollama_ws_gb()
    t = time.time()
    ld = post("/api/generate", {"model": model, "prompt": "", "stream": False, "options": {"num_ctx": NUM_CTX}})
    load_wall = time.time() - t
    ps = json.loads(urllib.request.urlopen(HOST + "/api/ps").read())["models"]
    mp = next(m for m in ps if m["name"] == model)
    ram1, ws1 = avail_ram_gb(), ollama_ws_gb()
    res = {"load_s": ld.get("load_duration", 0) / 1e9, "load_wall_s": load_wall, "size_gb": mp["size"] / 2**30,
           "vram_gb": mp["size_vram"] / 2**30, "ctx": mp.get("context_length"),
           "ram_free_drop_gb": ram0 - ram1, "ollama_ws_gb": ws1, "ollama_ws_before_gb": ws0}
    res["offload_pct"] = 100 * mp["size_vram"] / mp["size"]
    print({k: round(v, 2) if isinstance(v, float) else v for k, v in res.items()})

    tests = {}
    tests["greeting"] = chat(model, [{"role": "user", "content": "Hello Jarvis, how are you today?"}], 300)
    h = [{"role": "user", "content": "My name is Shikhil and I am building a voice assistant."}]
    h.append({"role": "assistant", "content": chat(model, h, 300)["content"]})
    h.append({"role": "user", "content": "What is the best wake word length, in syllables?"})
    h.append({"role": "assistant", "content": chat(model, h, 300)["content"]})
    h.append({"role": "user", "content": "What was my name, and what am I building?"})
    tests["multi_turn"] = chat(model, h, 300)
    tests["coding"] = chat(model, [{"role": "user", "content": "Write a short Python function that checks whether a string is a palindrome, ignoring case and punctuation."}], 400)
    for name, r in tests.items():
        print(f"-- {name}: ttf_any={r['ttf_any']:.1f}s ttf_content={(r['ttf_content'] or -1):.1f}s {r['tps']:.1f} tok/s "
              f"{r['tokens']} tok total={r['total']:.1f}s leak={r['leak']} thinking_chars={r['thinking_chars']} truncated={r['truncated']}")
        print("   " + r["content"][:300].replace("\n", "\n   "))
    res["tests"] = tests
    subprocess.run(["ollama", "stop", model], capture_output=True)
    return res


if __name__ == "__main__":
    out = {m: bench(m) for m in sys.argv[1:]}
    if len(sys.argv) > 2:
        json.dump(out, open(sys.argv[0].replace("bench_llm.py", "bench_results.json"), "w"), indent=1)
