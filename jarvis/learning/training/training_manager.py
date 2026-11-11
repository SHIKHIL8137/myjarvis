"""Hardware-aware training manager & scheduler (Phase 5):
inspects live CPU/RAM/VRAM/Ollama usage, computes conservative resource estimates, enforces
operator approval, spawns isolated worker processes, and handles pause/resume/cancel cleanly."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time

from ... import sysinfo
from ...config import LearningSpec, ROOT
from .training_audit import TrainingAuditLog
from .training_config import TrainingJobConfig
from .training_worker import inspect_training_capabilities


def _infer_param_billions(model_name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", (model_name or "").lower())
    if m:
        return float(m.group(1))
    return 4.0


class TrainingManager:
    """Tracks training readiness, hardware budgets, pending approvals, and isolated job lifecycles."""

    def __init__(
        self,
        spec: LearningSpec | None = None,
        *,
        jobs_dir: Path | None = None,
        prepared_dir: Path | None = None,
        audit_log: TrainingAuditLog | None = None,
    ):
        from . import JOBS_DIR, PREPARED_DIR, TRAINING_DIR
        self.spec = spec or LearningSpec()
        self.jobs_dir = Path(jobs_dir) if jobs_dir else JOBS_DIR
        self.prepared_dir = Path(prepared_dir) if prepared_dir else PREPARED_DIR
        self.audit = audit_log or TrainingAuditLog(TRAINING_DIR / "audit.jsonl")
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen] = {}

    def hardware_profile(self, *, ollama_ps: list[dict] | None = None) -> dict:
        ram_info = sysinfo.ram() or {}
        gpu_info = sysinfo.gpu() or {}
        caps = inspect_training_capabilities()
        total_ram = float(ram_info.get("total_gb") or 0.0)
        free_ram = float(ram_info.get("free_gb") or 0.0)
        total_vram = float(gpu_info.get("total_gb") or 0.0)
        used_vram = float(gpu_info.get("used_gb") or 0.0)
        free_vram = round(max(0.0, total_vram - used_vram), 2)
        ollama_vram = round(sum(float(m.get("size_vram") or 0) for m in (ollama_ps or [])) / (1024 ** 3), 2)
        return {
            "cpu": platform.processor() or "CPU",
            "cpu_pct": sysinfo.cpu(),
            "total_ram_gb": round(total_ram, 2),
            "free_ram_gb": round(free_ram, 2),
            "gpu_name": gpu_info.get("name"),
            "total_vram_gb": round(total_vram, 2),
            "used_vram_gb": round(used_vram, 2),
            "free_vram_gb": free_vram,
            "ollama_loaded_vram_gb": ollama_vram,
            "capabilities": caps,
        }

    def estimate_resources(self, cfg: TrainingJobConfig, *, ollama_ps: list[dict] | None = None) -> dict:
        hw = self.hardware_profile(ollama_ps=ollama_ps)
        params_b = _infer_param_billions(cfg.hf_model_id or cfg.base_model)
        bytes_per_param = {"4bit": 0.65, "8bit": 1.15, "fp16": 2.1}.get(cfg.quantization, 1.15)
        base_weights_gb = params_b * bytes_per_param
        # Activations + optimizer + LoRA gradients scale with seq length & batch size
        overhead_gb = 1.1 + (cfg.max_seq_length / 512.0) * 0.45 * cfg.batch_size
        est_vram_gb = round(base_weights_gb + overhead_gb, 2)
        est_ram_gb = round(max(2.5, params_b * 1.6 + 1.5), 2)

        caps = hw["capabilities"]
        total_vram = hw["total_vram_gb"]
        free_ram = hw["free_ram_gb"]
        free_vram_if_unloaded = round(min(total_vram, hw["free_vram_gb"] + hw["ollama_loaded_vram_gb"]), 2)

        blockers: list[str] = []
        if not caps["ready_for_peft_lora"] and cfg.backend == "peft_lora":
            blockers.append(f"Missing required packages: {', '.join(caps['missing_required'])}")
        if cfg.quantization == "4bit" and not caps["installed"].get("bitsandbytes"):
            blockers.append("4-bit QLoRA requires optional 'bitsandbytes' package with CUDA support.")
        if total_vram > 0 and est_vram_gb > free_vram_if_unloaded:
            blockers.append(
                f"Estimated VRAM ({est_vram_gb} GB) exceeds available GPU capacity ({free_vram_if_unloaded} GB even after unloading Ollama)."
            )
        if est_ram_gb > max(free_ram, hw["total_ram_gb"] * 0.7):
            blockers.append(f"Estimated RAM ({est_ram_gb} GB) is tight for free system RAM ({free_ram} GB).")

        need_unload = hw["ollama_loaded_vram_gb"] > 0.2 and total_vram > 0 and est_vram_gb > hw["free_vram_gb"]
        feasible = (len(blockers) == 0)
        return {
            "feasible_local": feasible,
            "requires_ollama_unload": need_unload,
            "estimated_vram_gb": est_vram_gb,
            "estimated_ram_gb": est_ram_gb,
            "hardware": hw,
            "blockers": blockers,
            "recommendation": (
                "Approved resource profile within local budget (unload Ollama before launch)." if (feasible and need_unload)
                else "Resource budget feasible." if feasible
                else "Keep RAG Knowledge Pipeline + Feedback Learning active locally; run weight fine-tuning on smaller <=1.5B model or a remote/GPU workstation."
            ),
        }

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _write_json(self, path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def create_job(self, cfg: TrainingJobConfig, *, ollama_ps: list[dict] | None = None) -> dict:
        prep = self.prepared_dir / cfg.dataset_build_id
        if not (prep / "train.jsonl").exists():
            raise FileNotFoundError(f"Prepared dataset build '{cfg.dataset_build_id}' does not exist.")
        estimate = self.estimate_resources(cfg, ollama_ps=ollama_ps)
        jdir = self._job_dir(cfg.job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        cfg_data = cfg.model_dump()
        cfg_data["prepared_dir"] = str(prep)
        self._write_json(jdir / "config.json", cfg_data)
        self._write_json(jdir / "control.json", {"command": "pending"})

        initial_state = "pending_approval" if self.spec.require_training_approval else "queued"
        status = {
            "job_id": cfg.job_id,
            "state": initial_state,
            "approved": not self.spec.require_training_approval,
            "approved_by": cfg.approved_by,
            "config": cfg_data,
            "estimate": estimate,
            "created_at": cfg.created_at,
            "updated_at": round(time.time(), 3),
        }
        self._write_json(jdir / "status.json", status)
        self.audit.record("training_requested", details={"job_id": cfg.job_id, "build_id": cfg.dataset_build_id, "state": initial_state, "feasible": estimate["feasible_local"]})
        return status

    def approve_job(self, job_id: str, *, approver: str = "user") -> dict:
        jdir = self._job_dir(job_id)
        sp = jdir / "status.json"
        if not sp.exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        st = json.loads(sp.read_text(encoding="utf-8"))
        st["approved"] = True
        st["approved_by"] = approver
        if st.get("state") == "pending_approval":
            st["state"] = "approved"
        st["updated_at"] = round(time.time(), 3)
        self._write_json(sp, st)
        self.audit.record("training_approved", actor=approver, details={"job_id": job_id})
        return st

    def start_job(self, job_id: str, *, force_spawn_even_if_blocked: bool = True) -> dict:
        with self._lock:
            jdir = self._job_dir(job_id)
            sp = jdir / "status.json"
            if not sp.exists():
                raise FileNotFoundError(f"Training job '{job_id}' not found.")
            st = json.loads(sp.read_text(encoding="utf-8"))
            if self.spec.require_training_approval and not st.get("approved"):
                raise PermissionError("Training job requires explicit operator approval before starting.")
            est = st.get("estimate") or {}
            if not est.get("feasible_local") and not force_spawn_even_if_blocked:
                raise RuntimeError("; ".join(est.get("blockers") or ["Insufficient hardware/packages for training"]))

            self._write_json(jdir / "control.json", {"command": "running"})
            self._write_json(jdir / "progress.json", {"job_id": job_id, "state": "starting", "updated_at": round(time.time(), 3)})
            st["state"] = "starting"
            st["started_at"] = round(time.time(), 3)
            st["updated_at"] = st["started_at"]
            self._write_json(sp, st)

            proc = subprocess.Popen(
                [sys.executable, "-m", "jarvis.learning.training.training_worker", "--job-dir", str(jdir)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._procs[job_id] = proc
            self.audit.record("training_started", details={"job_id": job_id, "pid": proc.pid})
            return self.get_job(job_id)

    def pause_job(self, job_id: str) -> dict:
        jdir = self._job_dir(job_id)
        if not (jdir / "status.json").exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        self._write_json(jdir / "control.json", {"command": "paused"})
        st = json.loads((jdir / "status.json").read_text(encoding="utf-8"))
        st["state"] = "paused"
        st["updated_at"] = round(time.time(), 3)
        self._write_json(jdir / "status.json", st)
        self.audit.record("training_paused", details={"job_id": job_id})
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> dict:
        jdir = self._job_dir(job_id)
        if not (jdir / "status.json").exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        self._write_json(jdir / "control.json", {"command": "running"})
        st = json.loads((jdir / "status.json").read_text(encoding="utf-8"))
        st["state"] = "running"
        st["updated_at"] = round(time.time(), 3)
        self._write_json(jdir / "status.json", st)
        self.audit.record("training_resumed", details={"job_id": job_id})
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> dict:
        jdir = self._job_dir(job_id)
        if not (jdir / "status.json").exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        self._write_json(jdir / "control.json", {"command": "cancelled"})
        st = json.loads((jdir / "status.json").read_text(encoding="utf-8"))
        st["state"] = "cancelled"
        st["updated_at"] = round(time.time(), 3)
        self._write_json(jdir / "status.json", st)
        proc = self._procs.get(job_id)
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.terminate()
        self.audit.record("training_cancelled", details={"job_id": job_id})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        jdir = self._job_dir(job_id)
        sp = jdir / "status.json"
        if not sp.exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        st = json.loads(sp.read_text(encoding="utf-8"))
        pp = jdir / "progress.json"
        if pp.exists():
            try:
                prog = json.loads(pp.read_text(encoding="utf-8"))
                st["progress"] = prog
                if st.get("state") not in ("paused", "cancelled"):
                    st["state"] = prog.get("state") or st["state"]
            except Exception:
                pass
        lp = jdir / "worker.log"
        if lp.exists():
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            st["log_tail"] = lines[-30:]
        else:
            st["log_tail"] = []
        return st

    def list_jobs(self) -> list[dict]:
        if not self.jobs_dir.exists():
            return []
        jobs: list[dict] = []
        for sp in self.jobs_dir.glob("*/status.json"):
            try:
                jobs.append(self.get_job(sp.parent.name))
            except Exception:
                pass
        return sorted(jobs, key=lambda j: j.get("created_at", 0), reverse=True)

    def prepare_remote_delegation_bundle(self, job_id: str) -> dict:
        """Export an auditable off-device training bundle when local VRAM (e.g. 4 GB GTX 1650) is insufficient."""
        jdir = self._job_dir(job_id)
        sp = jdir / "status.json"
        if not sp.exists():
            raise FileNotFoundError(f"Training job '{job_id}' not found.")
        st = json.loads(sp.read_text(encoding="utf-8"))
        if self.spec.require_training_approval and not st.get("approved"):
            raise PermissionError("Training job requires explicit owner approval before remote delegation export.")
        cfg = st["config"]
        prep_dir = Path(cfg["prepared_dir"])
        train_path = prep_dir / "train.jsonl"
        manifest = {
            "format": "jarvis-remote-training-spec-v1",
            "job_id": job_id,
            "base_model": cfg.get("base_model"),
            "hf_model_id": cfg.get("hf_model_id"),
            "method": cfg.get("method"),
            "quantization": cfg.get("quantization"),
            "dataset_build_id": cfg.get("dataset_build_id"),
            "train_jsonl": str(train_path),
            "train_sha256": __import__("hashlib").sha256(train_path.read_bytes()).hexdigest() if train_path.exists() else "",
            "requires_post_training_evaluation": self.spec.evaluation_required,
            "auto_promote": False,
        }
        out = jdir / "remote_delegation_bundle.json"
        self._write_json(out, manifest)
        st["state"] = "remote_ready"
        st["remote_bundle_path"] = str(out)
        st["updated_at"] = round(time.time(), 3)
        self._write_json(sp, st)
        self.audit.record("remote_training_bundle_prepared", details={"job_id": job_id, "bundle": str(out)})
        return manifest
