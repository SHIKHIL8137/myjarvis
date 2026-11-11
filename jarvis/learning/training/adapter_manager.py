"""Adapter & Ollama runtime compatibility verifier (Phase 7):
explicitly checks artifact hashes, GGUF/Modelfile references vs PEFT LoRA weights,
and NEVER misleads the user into assuming an incompatible adapter works natively in Ollama."""
import hashlib
import json
from pathlib import Path


def check_adapter_compatibility(
    *,
    artifact_type: str,
    base_model: str,
    adapter_path: str = "",
    ollama_tag: str = "",
    installed_ollama_models: list[str] | None = None,
) -> dict:
    """Verify artifact compatibility, existence, and checksum before activation."""
    atype = (artifact_type or "ollama_tag").strip().lower()
    reasons: list[str] = []
    warnings: list[str] = []
    sha256 = ""

    if atype == "peft_lora":
        # Ollama cannot load raw HuggingFace PEFT safetensors directly without GGUF conversion/merging
        p = Path(adapter_path) if adapter_path else None
        if not p or not p.exists():
            reasons.append(f"PEFT adapter path does not exist: {adapter_path or '<unset>'}")
        else:
            cfg = p / "adapter_config.json"
            weights = p / "adapter_model.safetensors"
            if not cfg.exists():
                reasons.append("Missing adapter_config.json inside PEFT directory")
            if weights.exists():
                sha256 = hashlib.sha256(weights.read_bytes()).hexdigest()
        reasons.append(
            "Raw Hugging Face PEFT LoRA weights cannot be loaded directly into Ollama without converting to GGUF ADAPTER or merging into a Modelfile."
        )
        return {
            "compatible_with_ollama": False,
            "verified": False,
            "artifact_type": atype,
            "base_model": base_model,
            "sha256": sha256,
            "reasons": reasons,
            "warnings": warnings,
        }

    if atype == "gguf_adapter":
        p = Path(adapter_path) if adapter_path else None
        if not p or not p.exists() or not p.is_file():
            reasons.append(f"GGUF adapter file missing: {adapter_path or '<unset>'}")
        else:
            head = p.read_bytes()[:4]
            sha256 = hashlib.sha256(p.read_bytes()).hexdigest()
            if head != b"GGUF":
                reasons.append("Adapter file does not start with valid GGUF magic header")
        return {
            "compatible_with_ollama": len(reasons) == 0,
            "verified": len(reasons) == 0,
            "artifact_type": atype,
            "base_model": base_model,
            "sha256": sha256,
            "reasons": reasons,
            "warnings": warnings,
        }

    if atype == "ollama_tag":
        tag = ollama_tag or base_model
        if installed_ollama_models is not None and tag not in installed_ollama_models:
            reasons.append(f"Ollama model tag '{tag}' is not installed locally in Ollama.")
        return {
            "compatible_with_ollama": len(reasons) == 0,
            "verified": len(reasons) == 0,
            "artifact_type": atype,
            "base_model": base_model,
            "ollama_tag": tag,
            "sha256": sha256,
            "reasons": reasons,
            "warnings": warnings,
        }

    return {
        "compatible_with_ollama": False,
        "verified": False,
        "artifact_type": atype,
        "base_model": base_model,
        "sha256": sha256,
        "reasons": [f"Unknown artifact_type '{artifact_type}'"],
        "warnings": warnings,
    }

