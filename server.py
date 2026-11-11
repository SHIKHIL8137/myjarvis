"""Entry point: python server.py  ->  http://127.0.0.1:4700 (loopback only)."""
import os

# Models are already on disk; never let a library try to reach the hub at runtime.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

import uvicorn  # noqa: E402

from jarvis.api import create_app  # noqa: E402
from jarvis.config import load_settings  # noqa: E402

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning")
