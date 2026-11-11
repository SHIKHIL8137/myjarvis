"""Versioned multilingual decision evaluation dataset loader & strict non-leakage split validator."""
import json
from pathlib import Path

from ..config import ROOT
from .schemas import ALLOWED_COMPLEXITIES, ALLOWED_DEPARTMENTS, ALLOWED_INTENTS, ALLOWED_RISK_TIERS

DEFAULT_DATASET_DIR = ROOT / "data" / "decision" / "datasets" / "v1"


def _normalize_text_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def validate_dataset_record(rec: dict) -> dict:
    """Validate a single JSONL dataset record strictly."""
    for req in ("id", "text", "language", "department", "intent", "complexity", "risk_tier"):
        if req not in rec or str(rec[req]).strip() == "":
            raise ValueError(f"Dataset record missing required field {req!r}: {rec!r}")
    dept = str(rec["department"]).strip().lower()
    intent = str(rec["intent"]).strip().lower()
    comp = str(rec["complexity"]).strip().lower()
    risk = str(rec["risk_tier"]).strip().upper()
    if dept not in ALLOWED_DEPARTMENTS:
        raise ValueError(f"Invalid dataset department {dept!r} in {rec['id']}")
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"Invalid dataset intent {intent!r} in {rec['id']}")
    if comp not in ALLOWED_COMPLEXITIES:
        raise ValueError(f"Invalid dataset complexity {comp!r} in {rec['id']}")
    if risk not in ALLOWED_RISK_TIERS:
        raise ValueError(f"Invalid dataset risk_tier {risk!r} in {rec['id']}")
    return {
        "id": str(rec["id"]),
        "text": str(rec["text"]),
        "language": str(rec["language"]).lower(),
        "category": str(rec.get("category") or "standard"),
        "department": dept,
        "intent": intent,
        "complexity": comp,
        "risk_tier": risk,
        "should_abstain": bool(rec.get("should_abstain", False)),
        "sensitive": bool(rec.get("sensitive", False)),
        "expected_workflow": str(rec.get("expected_workflow") or ""),
    }


def load_jsonl_split(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s:
            continue
        out.append(validate_dataset_record(json.loads(s)))
    return out


def verify_no_split_leakage(train_rows: list[dict], val_rows: list[dict], test_rows: list[dict]) -> dict:
    """Guarantee zero prompt overlap between train, val, and held-out test splits."""
    train_keys = {_normalize_text_key(r["text"]): r["id"] for r in train_rows}
    val_keys = {_normalize_text_key(r["text"]): r["id"] for r in val_rows}
    test_keys = {_normalize_text_key(r["text"]): r["id"] for r in test_rows}

    tv = set(train_keys) & set(val_keys)
    tt = set(train_keys) & set(test_keys)
    vt = set(val_keys) & set(test_keys)
    if tv or tt or vt:
        raise ValueError(
            f"Dataset split leakage detected! train∩val={len(tv)}, train∩test={len(tt)}, val∩test={len(vt)}"
        )
    return {
        "leakage_free": True,
        "counts": {"train": len(train_rows), "val": len(val_rows), "test": len(test_rows)},
    }


def load_decision_dataset(dataset_dir: Path | None = None) -> dict:
    d = Path(dataset_dir or DEFAULT_DATASET_DIR)
    train = load_jsonl_split(d / "train.jsonl")
    val = load_jsonl_split(d / "val.jsonl")
    test = load_jsonl_split(d / "test.jsonl")
    integrity = verify_no_split_leakage(train, val, test)
    return {
        "version": d.name,
        "dir": str(d),
        "train": train,
        "val": val,
        "test": test,
        **integrity,
    }

