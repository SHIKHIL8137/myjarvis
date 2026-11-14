"""ISOLATED QA server for the model selector: real app + real UI, cloud providers replaced by an in-process mock transport.
Never talks to a real provider, uses fake keys passed via environment, a temp JARVIS_HOME copy of config.json, and a temp overrides file.
Mode is read per request from QA_MODE_FILE (ok | auth | ratelimit | timeout | down), so failure modes can be flipped without a restart.
  python scripts/qa_model_server.py <port> <home_dir>
The mocked chat completion echoes the model id it was routed to, which is the evidence that the selected model received the request.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

port, home = int(sys.argv[1]), Path(sys.argv[2])
home.mkdir(parents=True, exist_ok=True)
(home / "data").mkdir(exist_ok=True)
src = Path(__file__).resolve().parent.parent
(home / "config.json").write_text((src / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
(home / "notes").mkdir(exist_ok=True)
os.environ.update({"JARVIS_HOME": str(home), "JARVIS_PORT": str(port), "JARVIS_OVERRIDES_FILE": str(home / "o.json"),
                   "OPENAI_API_KEY": "sk-FAKEQAKEY0000000000000000", "OPENROUTER_API_KEY": "sk-or-FAKEQAKEY000000000000000"})
sys.path.insert(0, str(src))
MODE_FILE = Path(os.environ.get("QA_MODE_FILE", home / "mode.txt"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from jarvis import audit  # noqa: E402
from jarvis import brain as brain_mod  # noqa: E402
from jarvis import netsec  # noqa: E402
from jarvis.api import create_app  # noqa: E402
from jarvis.config import load_settings  # noqa: E402

OR = {"data": [
    {"id": "deepseek/deepseek-chat:free", "name": "DeepSeek Chat (free)", "context_length": 64000, "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, "supported_parameters": ["tools"]},
    {"id": "qwen/qwen3-coder:free", "name": "Qwen3 Coder (free)", "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"]}, "supported_parameters": ["tools"]},
    {"id": "qwen/qwen3-coder", "name": "Qwen3 Coder", "context_length": 262144, "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
     "architecture": {"input_modalities": ["text"]}, "supported_parameters": ["tools", "reasoning"]},
    {"id": "google/gemini-2.5-flash", "name": "Gemini Flash", "context_length": 1000000, "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
     "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["tools"]},
]}
OA = {"data": [{"id": "gpt-4.1", "owned_by": "system"}, {"id": "gpt-4o-mini", "owned_by": "system"}, {"id": "text-embedding-3-small"}]}


def mode() -> str:
    try:
        return MODE_FILE.read_text().strip() or "ok"
    except OSError:
        return "ok"


def handler(req: httpx.Request) -> httpx.Response:
    m = mode()
    if m == "timeout":
        raise httpx.ConnectTimeout("mock timeout")
    if m == "down":
        raise httpx.ConnectError("mock down")
    if m == "auth":
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-FAKEQAKEY0000000000000000"}})
    if m == "ratelimit":
        return httpx.Response(429, json={"error": {"message": "rate limited"}})
    url = str(req.url)
    if url.endswith("/models"):
        return httpx.Response(200, json=OR if "openrouter" in url else OA)
    if url.endswith("/chat/completions"):
        body = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": f"[mock reply routed to {body.get('model')}]"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return httpx.Response(404)


TRANSPORT = httpx.MockTransport(handler)
_orig = netsec.safe_request


async def _mock_safe_request(method, url, spec, **kw):
    kw["transport_factory"] = lambda _t: TRANSPORT
    return await _orig(method, url, spec, **kw)


netsec.safe_request = _mock_safe_request
brain_mod.safe_request = _mock_safe_request
import jarvis.model_catalog as mcat  # noqa: E402

mcat.safe_request = _mock_safe_request

s = load_settings()
s.internet.enabled = not os.environ.get("QA_OFFLINE")
s.providers.allow_cloud = True
app = create_app(s)
audit.install(enforce=s.offline_enforced, online_enabled=not os.environ.get("QA_OFFLINE"))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
