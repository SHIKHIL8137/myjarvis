"""Dataset builder (Phase 5): format conversion (Alpaca JSONL <-> conversational messages),
deterministic train/val/test splitting, and prompt-overlap data leakage verification."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import random
import re
import time


@dataclass
class BuiltDataset:
    build_id: str
    dataset_ids: list[str]
    target_format: str  # "messages" | "alpaca"
    train_count: int
    val_count: int
    test_count: int
    leakage_clean: bool
    leakage_overlaps: int
    sha256: str
    output_dir: str
    created_at: float = field(default_factory=lambda: round(time.time(), 3))

    def to_dict(self) -> dict:
        return asdict(self)


def to_messages_record(rec: dict, *, default_system: str = "You are Jarvis, a concise personal AI assistant.") -> dict:
    """Convert Alpaca or prompt/response record uniformly into conversational messages schema."""
    if "messages" in rec and isinstance(rec["messages"], list):
        msgs = [{"role": str(m["role"]), "content": str(m["content"])} for m in rec["messages"]]
        if not any(m["role"] == "system" for m in msgs):
            msgs.insert(0, {"role": "system", "content": default_system})
        return {"messages": msgs}

    if "instruction" in rec:
        instr = str(rec.get("instruction", "")).strip()
        inp = str(rec.get("input", "")).strip()
        user_txt = f"{instr}\n\n{inp}".strip() if inp else instr
        out_txt = str(rec.get("output", "")).strip()
    else:
        user_txt = str(rec.get("prompt", "")).strip()
        out_txt = str(rec.get("response") or rec.get("completion") or "").strip()

    return {
        "messages": [
            {"role": "system", "content": default_system},
            {"role": "user", "content": user_txt},
            {"role": "assistant", "content": out_txt},
        ]
    }


def to_alpaca_record(rec: dict) -> dict:
    """Convert conversational messages or prompt/response record into Alpaca JSONL schema."""
    if "instruction" in rec and "output" in rec:
        return {
            "instruction": str(rec.get("instruction", "")).strip(),
            "input": str(rec.get("input", "")).strip(),
            "output": str(rec.get("output", "")).strip(),
        }
    if "messages" in rec and isinstance(rec["messages"], list):
        users = [str(m.get("content", "")).strip() for m in rec["messages"] if m.get("role") == "user"]
        assistants = [str(m.get("content", "")).strip() for m in rec["messages"] if m.get("role") == "assistant"]
        return {
            "instruction": "\n\n".join(u for u in users if u),
            "input": "",
            "output": "\n\n".join(a for a in assistants if a),
        }
    return {
        "instruction": str(rec.get("prompt", "")).strip(),
        "input": "",
        "output": str(rec.get("response") or rec.get("completion") or "").strip(),
    }


def convert_records(records: list[dict], target_format: str = "messages") -> list[dict]:
    fmt = (target_format or "messages").strip().lower()
    if fmt == "alpaca":
        return [to_alpaca_record(r) for r in records]
    return [to_messages_record(r) for r in records]


def _prompt_fingerprint(rec: dict) -> str:
    if "messages" in rec:
        txt = " ".join(m.get("content", "") for m in rec["messages"] if m.get("role") == "user")
    elif "instruction" in rec:
        txt = f"{rec.get('instruction', '')} {rec.get('input', '')}"
    else:
        txt = str(rec.get("prompt", ""))
    norm = re.sub(r"\s+", " ", txt.lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class DatasetBuilder:
    """Converts cleaned datasets to canonical format, splits deterministically, and checks leakage."""

    def __init__(self, prepared_dir: Path | None = None):
        from . import PREPARED_DIR
        self.prepared_dir = Path(prepared_dir) if prepared_dir else PREPARED_DIR

    def build(
        self,
        records: list[dict],
        *,
        build_id: str,
        dataset_ids: list[str] | None = None,
        target_format: str = "messages",
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        seed: int = 42,
    ) -> BuiltDataset:
        if not records:
            raise ValueError("Cannot build training splits from 0 records.")
        if val_ratio < 0 or test_ratio < 0 or (val_ratio + test_ratio) >= 0.6:
            raise ValueError("Validation and test ratios must be non-negative and leave >= 40% for training.")

        converted = convert_records(records, target_format=target_format)
        # Group by normalized prompt fingerprint so duplicate prompts never cross train/val/test
        groups: dict[str, list[dict]] = {}
        for r in converted:
            fp = _prompt_fingerprint(r)
            groups.setdefault(fp, []).append(r)

        keys = sorted(groups.keys())
        rng = random.Random(seed)
        rng.shuffle(keys)

        n_groups = len(keys)
        n_test = max(1, int(round(n_groups * test_ratio))) if n_groups >= 5 and test_ratio > 0 else 0
        n_val = max(1, int(round(n_groups * val_ratio))) if n_groups >= 4 and val_ratio > 0 else 0
        if n_test + n_val >= n_groups:
            n_val = max(0, n_groups - 2)
            n_test = max(0, min(1, n_groups - 1 - n_val))

        test_keys = set(keys[:n_test])
        val_keys = set(keys[n_test:n_test + n_val])
        train_keys = set(keys[n_test + n_val:])

        train_recs = [r for k in train_keys for r in groups[k]]
        val_recs = [r for k in val_keys for r in groups[k]]
        test_recs = [r for k in test_keys for r in groups[k]]

        train_fps = {_prompt_fingerprint(r) for r in train_recs}
        eval_fps = {_prompt_fingerprint(r) for r in val_recs + test_recs}
        overlaps = len(train_fps & eval_fps)
        if overlaps > 0:
            raise ValueError(f"Dataset split failed leakage check ({overlaps} overlapping prompts).")

        out_dir = self.prepared_dir / build_id
        out_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        for name, split in (("train.jsonl", train_recs), ("val.jsonl", val_recs), ("test.jsonl", test_recs)):
            payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in split)
            text = (payload + "\n") if payload else ""
            (out_dir / name).write_text(text, encoding="utf-8")
            h.update(text.encode("utf-8"))

        built = BuiltDataset(
            build_id=build_id,
            dataset_ids=dataset_ids or [],
            target_format=target_format,
            train_count=len(train_recs),
            val_count=len(val_recs),
            test_count=len(test_recs),
            leakage_clean=True,
            leakage_overlaps=overlaps,
            sha256=h.hexdigest(),
            output_dir=str(out_dir),
        )
        (out_dir / "manifest.json").write_text(json.dumps(built.to_dict(), indent=2), encoding="utf-8")
        return built

    def list_builds(self) -> list[dict]:
        if not self.prepared_dir.exists():
            return []
        builds: list[dict] = []
        for mp in sorted(self.prepared_dir.glob("*/manifest.json")):
            try:
                builds.append(json.loads(mp.read_text(encoding="utf-8")))
            except Exception:
                pass
        return sorted(builds, key=lambda b: b.get("created_at", 0), reverse=True)

