"""Standalone isolated Laya FastAPI internal service & runtime wrapper (runs inside `data/laya_iso_venv`).

Exposes:
  - GET  /health
  - POST /internal/decision/classify

Never mounts in the main API server cleanly when heavy ML deps are absent; communicates ONLY over loopback JSON.
"""
import argparse
import ctypes
import ctypes.wintypes
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .rules import detect_text_language
from .schemas import JARVIS_LAYA_QUESTIONS, validate_classifier_payload

# Keep TensorFlow disabled inside transformers so torch-only Laya loads cleanly
os.environ.setdefault("USE_TF", "0")


class LayaRuntimeManager:
    """Manages resident Laya/Vega checkpoint count, thread limits, calibration overlay, and RSS metrics."""

    def __init__(self, *, max_resident_models: int = 1, default_checkpoint: str = "multilingual", device: str = "cpu"):
        self.max_resident_models = max(1, int(max_resident_models))
        self.default_checkpoint = default_checkpoint
        self.device = device
        self._agents: dict[str, Any] = {}
        self._order: list[str] = []
        self._calibration: dict | None = None
        self._startup_ms: float = 0.0

    def apply_calibration(self, payload: dict | None) -> None:
        self._calibration = dict(payload) if payload else None

    def _evict_if_needed(self) -> None:
        while len(self._order) >= self.max_resident_models:
            victim = self._order.pop(0)
            ag = self._agents.pop(victim, None)
            if ag is not None:
                try:
                    unload = getattr(ag, "unload", None)
                    if callable(unload):
                        unload()
                except Exception:
                    pass

    def _get_agent(self, checkpoint: str, device: str):
        key = f"{checkpoint}:{device}"
        if key in self._agents:
            self._order.remove(key)
            self._order.append(key)
            return self._agents[key]
        self._evict_if_needed()
        t0 = time.perf_counter()
        import torch
        import laya

        if device == "cpu":
            torch.set_num_threads(min(8, max(1, os.cpu_count() or 4)))
        if checkpoint == "vegaml":
            ag = laya.Agent(vegaml=True, device=None if device == "auto" else device)
        else:
            repo_map = {
                "english": "convaiinnovations/laya",
                "multilingual": "convaiinnovations/laya-multilingual",
                "typed-decisions": "convaiinnovations/laya-typed-decisions",
            }
            ag = laya.Agent(repo_map.get(checkpoint, checkpoint), device=None if device == "auto" else device)
        self._startup_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        self._agents[key] = ag
        self._order.append(key)
        return ag

    @staticmethod
    def _process_rss_mb() -> float:
        try:
            import psutil

            return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass
        if os.name == "nt":
            try:
                class _PMC(ctypes.Structure):
                    _fields_ = [
                        ("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                pmc = _PMC()
                pmc.cb = ctypes.sizeof(pmc)
                k32 = ctypes.windll.kernel32
                k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
                psapi = ctypes.windll.psapi
                psapi.GetProcessMemoryInfo.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_PMC), ctypes.wintypes.DWORD]
                psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
                if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                    return round(pmc.WorkingSetSize / (1024 * 1024), 1)
            except Exception:
                pass
        return 0.0

    def status(self) -> dict:
        rss_mb = self._process_rss_mb()
        cuda_avail = False
        laya_ver = "unavailable"
        try:
            import laya
            import torch

            laya_ver = getattr(laya, "__version__", "unknown")
            cuda_avail = bool(torch.cuda.is_available())
        except Exception:
            pass
        return {
            "ok": True,
            "laya_version": laya_ver,
            "cuda_available": cuda_avail,
            "loaded_models": list(self._order),
            "max_resident_models": self.max_resident_models,
            "startup_ms": self._startup_ms,
            "rss_mb": rss_mb,
            "calibration_active": bool(self._calibration),
        }

    def predict(
        self,
        *,
        text: str,
        checkpoint: str | None = None,
        device: str | None = None,
        abstain_enabled: bool = True,
        confidence_threshold: float = 0.70,
        questions: dict | None = None,
    ) -> dict:
        t0 = time.perf_counter()
        ckpt = checkpoint or self.default_checkpoint
        dev = device or self.device
        ag = self._get_agent(ckpt, dev)
        qmap = questions or JARVIS_LAYA_QUESTIONS
        out = ag.predict(text, qmap)
        ans = out.get("answers") or {}

        dept_a = ans.get("department") or {}
        intent_a = ans.get("intent") or {}
        comp_a = ans.get("complexity") or {}
        risk_a = ans.get("risk_tier") or {}
        web_a = ans.get("needs_live_web") or {}
        proj_a = ans.get("needs_project_context") or {}

        dept = str(dept_a.get("choice") or "chat").lower()
        intent = str(intent_a.get("choice") or "general_chat").lower()
        comp = str(comp_a.get("choice") or "standard").lower()
        risk = str(risk_a.get("choice") or "READ").upper()
        needs_web = bool(float(web_a.get("noul") or 0.0) >= 0.5)
        needs_proj = bool(float(proj_a.get("noul") or 0.0) >= 0.5)

        confs = [
            float(dept_a.get("answer_confidence") or dept_a.get("confidence") or max((dept_a.get("probabilities") or {dept: 0.5}).values())),
            float(intent_a.get("answer_confidence") or intent_a.get("confidence") or max((intent_a.get("probabilities") or {intent: 0.5}).values())),
        ]
        conf = max(0.0, min(1.0, sum(confs) / len(confs)))
        if self._calibration and isinstance(self._calibration.get("confidence_threshold"), (int, float)):
            confidence_threshold = float(self._calibration["confidence_threshold"])

        abstained = bool(dept_a.get("abstain") or intent_a.get("abstain") or (abstain_enabled and conf < confidence_threshold))
        unbound = bool(dept_a.get("unbound") or intent_a.get("unbound"))
        route = str(intent_a.get("route") or dept_a.get("route") or ("review" if abstained else "auto"))

        res = validate_classifier_payload(
            {
                "department": dept,
                "intent": intent,
                "complexity": comp,
                "risk_tier": risk,
                "needs_live_web": needs_web,
                "needs_project_context": needs_proj,
                "confidence": conf,
                "abstain": abstained,
                "unbound": unbound,
                "route": route if route in ("auto", "review") else "review",
                "clarifying_question": "Could you clarify what action or target you would like?" if abstained else None,
            }
        )
        res["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        res["language"] = detect_text_language(text)
        res["model_version"] = f"laya:{ckpt}"
        return res


class ClassifyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=16000)
    checkpoint: str = "multilingual"
    device: str = "cpu"
    abstain_enabled: bool = True
    confidence_threshold: float = 0.70
    questions: dict | None = None


def create_laya_app(manager: LayaRuntimeManager | None = None) -> FastAPI:
    mgr = manager or LayaRuntimeManager()
    app = FastAPI(title="Jarvis Isolated Laya Decision Service", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return mgr.status()

    @app.post("/internal/decision/classify")
    async def classify(body: ClassifyIn):
        try:
            return mgr.predict(
                text=body.text,
                checkpoint=body.checkpoint,
                device=body.device,
                abstain_enabled=body.abstain_enabled,
                confidence_threshold=body.confidence_threshold,
                questions=body.questions,
            )
        except Exception as exc:
            raise HTTPException(500, f"Laya inference failed: {exc}") from exc

    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Run isolated Jarvis Laya decision service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4710)
    ap.add_argument("--checkpoint", default="multilingual")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--max-resident-models", type=int, default=1)
    ap.add_argument("--preload", action="store_true", help="Load the configured checkpoint at startup before serving requests")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Laya service must bind strictly to loopback.")
    mgr = LayaRuntimeManager(
        max_resident_models=args.max_resident_models,
        default_checkpoint=args.checkpoint,
        device=args.device,
    )
    if args.preload:
        mgr._get_agent(args.checkpoint, args.device)
    uvicorn.run(create_laya_app(mgr), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
