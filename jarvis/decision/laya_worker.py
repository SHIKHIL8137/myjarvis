"""Isolated stdio JSON-line worker entrypoint for Laya subprocess execution mode."""
import json
import sys

from .laya_service import LayaRuntimeManager


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("Empty request payload on stdin")
    req = json.loads(raw)
    mgr = LayaRuntimeManager(
        default_checkpoint=str(req.get("checkpoint") or "multilingual"),
        device=str(req.get("device") or "cpu"),
    )
    res = mgr.predict(
        text=str(req["text"]),
        checkpoint=req.get("checkpoint"),
        device=req.get("device"),
        abstain_enabled=bool(req.get("abstain_enabled", True)),
        confidence_threshold=float(req.get("confidence_threshold", 0.70)),
        questions=req.get("questions"),
    )
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

