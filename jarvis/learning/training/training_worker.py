"""Isolated training worker (Phase 5): lazy-probes optional PEFT/LoRA libraries,
cooperatively supports pause/resume/cancel control files, and NEVER fabricates fake weights."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def inspect_training_capabilities() -> dict:
    """Safely check whether optional fine-tuning dependencies are installed without heavy imports at startup."""
    pkgs = ["torch", "transformers", "peft", "trl", "bitsandbytes", "accelerate", "datasets"]
    installed = {p: (importlib.util.find_spec(p) is not None) for p in pkgs}
    cuda = {"available": False, "device_name": None, "vram_gb": None}
    if installed["torch"]:
        try:
            import torch
            cuda["available"] = bool(torch.cuda.is_available())
            if cuda["available"]:
                props = torch.cuda.get_device_properties(0)
                cuda["device_name"] = props.name
                cuda["vram_gb"] = round(props.total_memory / (1024 ** 3), 2)
        except Exception:
            pass
    required_core = ("torch", "transformers", "peft")
    missing = [p for p in required_core if not installed[p]]
    return {
        "installed": installed,
        "missing_required": missing,
        "ready_for_peft_lora": len(missing) == 0,
        "ready_for_qlora_4bit": len(missing) == 0 and installed["bitsandbytes"] and cuda["available"],
        "cuda": cuda,
    }


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_control(path: Path) -> str:
    if not path.exists():
        return "running"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("command") or "running").lower()
    except Exception:
        return "running"


def run_job_directory(job_dir: Path) -> int:
    job_dir = Path(job_dir)
    cfg = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
    prep_dir = Path(cfg["prepared_dir"])
    progress_path = job_dir / "progress.json"
    control_path = job_dir / "control.json"
    log_path = job_dir / "worker.log"
    out_dir = job_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    def log_line(msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {msg}\n")

    def save_prog(state: str, **extra):
        data = {
            "job_id": cfg["job_id"],
            "state": state,
            "updated_at": round(time.time(), 3),
            **extra,
        }
        _write_json(progress_path, data)

    caps = inspect_training_capabilities()
    log_line(f"Capability check: ready_for_peft_lora={caps['ready_for_peft_lora']} missing={caps['missing_required']}")
    if not caps["ready_for_peft_lora"]:
        msg = (
            f"Fine-tuning runtime missing required Python packages: {', '.join(caps['missing_required'])}. "
            "No fake training was executed. Install optional training dependencies in a dedicated environment or configure a remote worker."
        )
        log_line(msg)
        save_prog("blocked", error=msg, capabilities=caps, epoch=0, step=0, total_steps=0)
        return 2

    # Lazy import only after dependency check succeeds
    import torch
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    train_path = prep_dir / "train.jsonl"
    rows = [json.loads(ln) for ln in train_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows:
        save_prog("failed", error="Prepared train.jsonl is empty.", epoch=0, step=0, total_steps=0)
        return 1

    model_id = cfg["hf_model_id"]
    log_line(f"Loading local tokenizer/base model (local_files_only=True): {model_id}")
    save_prog("loading_model", epoch=0, step=0, total_steps=len(rows) * int(cfg["epochs"]))
    tok = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=dtype, local_files_only=True)
    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=int(cfg["lora_r"]),
        lora_alpha=int(cfg["lora_alpha"]),
        lora_dropout=float(cfg["lora_dropout"]),
        target_modules=list(cfg["target_modules"]),
    )
    model = get_peft_model(model, lora_cfg)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.train()

    optimizer = torch.optim.AdamW(model.parameters(), lr=float(cfg["learning_rate"]))
    total_steps = max(1, len(rows) * int(cfg["epochs"]))
    step = 0
    t_start = time.time()
    losses: list[float] = []

    for epoch in range(1, int(cfg["epochs"]) + 1):
        for rec in rows:
            while True:
                cmd = _read_control(control_path)
                if cmd == "cancelled":
                    log_line("Training cancelled cleanly by operator.")
                    save_prog("cancelled", epoch=epoch, step=step, total_steps=total_steps, loss=losses[-1] if losses else None)
                    return 0
                if cmd == "paused":
                    save_prog("paused", epoch=epoch, step=step, total_steps=total_steps, loss=losses[-1] if losses else None)
                    time.sleep(0.25)
                    continue
                break

            if "messages" in rec:
                text = "\n".join(f"{m['role']}: {m['content']}" for m in rec["messages"])
            else:
                text = f"User: {rec.get('instruction', '')} {rec.get('input', '')}\nAssistant: {rec.get('output', '')}"
            batch = tok(text, return_tensors="pt", truncation=True, max_length=int(cfg["max_seq_length"])).to(device)
            out = model(**batch, labels=batch["input_ids"])
            loss = out.loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            step += 1
            loss_val = round(float(loss.detach().item()), 4)
            losses.append(loss_val)
            elapsed = max(0.01, time.time() - t_start)
            eta = round((elapsed / step) * max(0, total_steps - step), 1)
            save_prog("running", epoch=epoch, step=step, total_steps=total_steps, loss=loss_val, eta_s=eta)
            if step == 1 or step % 5 == 0 or step == total_steps:
                log_line(f"epoch={epoch}/{cfg['epochs']} step={step}/{total_steps} loss={loss_val:.4f} eta={eta}s")

    model.save_pretrained(str(out_dir))
    tok.save_pretrained(str(out_dir))
    mean_loss = round(sum(losses) / max(1, len(losses)), 4)
    save_prog(
        "completed",
        epoch=int(cfg["epochs"]),
        step=step,
        total_steps=total_steps,
        loss=losses[-1] if losses else None,
        mean_loss=mean_loss,
        output_dir=str(out_dir),
    )
    log_line(f"Training completed cleanly. Adapter saved to {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Jarvis isolated LoRA training worker")
    ap.add_argument("--job-dir", required=True)
    args = ap.parse_args()
    return run_job_directory(Path(args.job_dir))


if __name__ == "__main__":
    sys.exit(main())

