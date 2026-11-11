"""Validated training job configuration specification (Phase 5)."""
import time
import uuid
from pydantic import BaseModel, Field


class TrainingJobConfig(BaseModel):
    job_id: str = Field(default_factory=lambda: f"train-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
    base_model: str = "qwen3:4b-instruct"
    hf_model_id: str = "Qwen/Qwen2.5-0.5B-Instruct"
    dataset_build_id: str
    backend: str = Field("peft_lora", pattern="^(peft_lora|remote_worker)$")
    method: str = Field("qlora", pattern="^(lora|qlora)$")
    quantization: str = Field("4bit", pattern="^(4bit|8bit|fp16)$")
    lora_r: int = Field(8, ge=2, le=64)
    lora_alpha: int = Field(16, ge=4, le=128)
    lora_dropout: float = Field(0.05, ge=0.0, le=0.3)
    target_modules: list[str] = Field(default_factory=lambda: ["q_proj", "v_proj"])
    epochs: int = Field(1, ge=1, le=20)
    batch_size: int = Field(1, ge=1, le=32)
    grad_accum_steps: int = Field(4, ge=1, le=64)
    learning_rate: float = Field(2e-4, ge=1e-6, le=5e-3)
    max_seq_length: int = Field(512, ge=128, le=4096)
    output_name: str = ""
    approved_by: str = ""
    created_at: float = Field(default_factory=lambda: round(time.time(), 3))

