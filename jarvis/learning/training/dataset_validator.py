"""Dataset validation & quarantine engine (Phase 5): schema checks, license policy enforcement,
credential/PII detection, corrupt input rejection, and prompt-injection/poisoning screening."""
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import time

from ...config import LearningSpec
from ...memory_store import detect_secret

_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("system_override", re.compile(r"\b(?:ignore\s+(?:all\s+)?previous\s+instructions|disregard\s+system\s+prompt|you\s+are\s+now\s+(?:dan|unrestricted|root))\b", re.I)),
    ("chat_template_smuggling", re.compile(r"<\|im_start\|>\s*system|<\|system\|>|<<SYS>>", re.I)),
    ("remote_shell_pipe", re.compile(r"(?:curl|wget)\s+https?://\S+\s*\|\s*(?:bash|sh|pwsh|powershell)", re.I)),
    ("destructive_shell", re.compile(r"(?:\brm\s+-rf\s+/|\bformat\s+[a-z]:|\bInvoke-Expression\b|\bpowershell\b[^\n]{0,60}-enc(?:odedcommand)?\b)", re.I)),
    ("exfiltration_instruction", re.compile(r"\b(?:exfiltrate|steal|upload)\s+(?:all\s+)?(?:secrets|tokens|passwords|ssh\s+keys|\.env)\b", re.I)),
]


@dataclass
class ValidationReport:
    dataset_id: str
    version: int
    valid: bool
    approved: bool
    license: str
    license_ok: bool
    total_records: int
    valid_records: int
    rejected_records: int
    format_detected: str
    reasons: list[str] = field(default_factory=list)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    checked_at: float = field(default_factory=lambda: round(time.time(), 3))

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_license(raw: str | None) -> str:
    val = (raw or "unknown").strip().lower()
    aliases = {
        "apache 2.0": "apache-2.0",
        "apache2": "apache-2.0",
        "bsd 3-clause": "bsd-3-clause",
        "bsd 2-clause": "bsd-2-clause",
        "mit license": "mit",
        "cc by 4.0": "cc-by-4.0",
        "cc-by-sa 4.0": "cc-by-sa-4.0",
        "cc0": "cc0-1.0",
    }
    return aliases.get(val, val)


def is_license_allowed(raw_license: str | None, spec: LearningSpec) -> bool:
    norm = normalize_license(raw_license)
    if not spec.require_verified_license:
        return norm not in ("", "proprietary-blocked")
    if norm in ("", "unknown", "none", "unspecified"):
        return False
    allowed = {normalize_license(x) for x in spec.allowed_licenses}
    return norm in allowed


def detect_poisoning(text: str) -> str | None:
    s = text or ""
    for label, pat in _INJECTION_PATTERNS:
        if pat.search(s):
            return label
    return None


def _extract_record_texts(rec: dict) -> tuple[bool, list[str], str]:
    """Validate record schema (Alpaca or conversational messages) and extract text chunks."""
    if not isinstance(rec, dict):
        return False, [], "not_object"
    if "messages" in rec:
        msgs = rec.get("messages")
        if not isinstance(msgs, list) or len(msgs) < 2:
            return False, [], "invalid_messages_schema"
        roles = set()
        texts: list[str] = []
        for m in msgs:
            if not isinstance(m, dict):
                return False, [], "invalid_message_item"
            role = str(m.get("role", "")).strip().lower()
            content = m.get("content")
            if role not in ("system", "user", "assistant") or not isinstance(content, str) or not content.strip():
                return False, [], "invalid_message_content"
            roles.add(role)
            texts.append(content)
        if "user" not in roles or "assistant" not in roles:
            return False, [], "missing_user_or_assistant"
        return True, texts, "messages"

    instr = rec.get("instruction")
    out = rec.get("output")
    inp = rec.get("input", "")
    if isinstance(instr, str) and isinstance(out, str) and instr.strip() and out.strip():
        texts = [instr, str(inp or ""), out]
        return True, texts, "alpaca"

    # Also accept prompt/response pairs cleanly
    prompt = rec.get("prompt")
    resp = rec.get("response") or rec.get("completion")
    if isinstance(prompt, str) and isinstance(resp, str) and prompt.strip() and resp.strip():
        return True, [prompt, resp], "prompt_response"

    return False, [], "invalid_schema"


class DatasetValidator:
    """Validates raw datasets without executing untrusted pickles or code."""

    def __init__(self, spec: LearningSpec | None = None, quarantine_dir: Path | None = None):
        from . import QUARANTINE_DIR
        self.spec = spec or LearningSpec()
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir else QUARANTINE_DIR

    def validate_file(
        self,
        raw_file: Path,
        *,
        dataset_id: str,
        version: int,
        license_name: str,
        max_quarantine_samples: int = 200,
    ) -> tuple[ValidationReport, list[dict]]:
        path = Path(raw_file)
        norm_lic = normalize_license(license_name)
        lic_ok = is_license_allowed(norm_lic, self.spec)
        reasons: list[str] = []
        if not lic_ok:
            reasons.append(f"license '{norm_lic}' is not in verified allowed_licenses policy")

        if not path.exists() or not path.is_file():
            return ValidationReport(
                dataset_id=dataset_id, version=version, valid=False, approved=False,
                license=norm_lic, license_ok=lic_ok, total_records=0, valid_records=0,
                rejected_records=0, format_detected="missing", reasons=reasons + ["dataset file missing"],
            ), []

        ext = path.suffix.lower()
        if ext not in (".jsonl", ".json"):
            return ValidationReport(
                dataset_id=dataset_id, version=version, valid=False, approved=False,
                license=norm_lic, license_ok=lic_ok, total_records=0, valid_records=0,
                rejected_records=0, format_detected=ext.lstrip(".") or "unknown",
                reasons=reasons + [f"unsupported or unsafe file format '{ext}'; only .json/.jsonl accepted"],
            ), []

        raw_bytes = path.read_bytes()
        if b"\x00" in raw_bytes:
            return ValidationReport(
                dataset_id=dataset_id, version=version, valid=False, approved=False,
                license=norm_lic, license_ok=lic_ok, total_records=0, valid_records=0,
                rejected_records=0, format_detected="corrupt", reasons=reasons + ["corrupt binary/NUL bytes in dataset"],
            ), []

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return ValidationReport(
                dataset_id=dataset_id, version=version, valid=False, approved=False,
                license=norm_lic, license_ok=lic_ok, total_records=0, valid_records=0,
                rejected_records=0, format_detected="corrupt", reasons=reasons + [f"invalid UTF-8 encoding: {e}"],
            ), []

        records: list[dict] = []
        parse_errors = 0
        if ext == ".json":
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    records = [x for x in loaded if isinstance(x, dict)]
                    parse_errors += len(loaded) - len(records)
                elif isinstance(loaded, dict):
                    records = [loaded]
                else:
                    parse_errors += 1
            except Exception as e:
                reasons.append(f"JSON parse error: {e}")
        else:
            for ln in text.splitlines():
                if not ln.strip():
                    continue
                try:
                    obj = json.loads(ln)
                    if isinstance(obj, dict):
                        records.append(obj)
                    else:
                        parse_errors += 1
                except Exception:
                    parse_errors += 1

        accepted: list[dict] = []
        quarantined: list[dict] = []
        counts: dict[str, int] = {}
        if parse_errors:
            counts["parse_error"] = parse_errors
        formats: set[str] = set()

        for idx, rec in enumerate(records):
            ok_schema, texts, fmt = _extract_record_texts(rec)
            if not ok_schema:
                counts[fmt] = counts.get(fmt, 0) + 1
                if len(quarantined) < max_quarantine_samples:
                    quarantined.append({"index": idx, "reason": fmt, "record": rec})
                continue

            joined = "\n".join(texts)
            secret = detect_secret(joined)
            if secret:
                key = f"credential:{secret}"
                counts[key] = counts.get(key, 0) + 1
                if len(quarantined) < max_quarantine_samples:
                    quarantined.append({"index": idx, "reason": key})  # never copy secret text into quarantine
                continue

            poison = detect_poisoning(joined)
            if poison:
                key = f"poisoning:{poison}"
                counts[key] = counts.get(key, 0) + 1
                if len(quarantined) < max_quarantine_samples:
                    quarantined.append({"index": idx, "reason": key, "record": rec})
                continue

            formats.add(fmt)
            accepted.append(rec)

        total = len(records) + parse_errors
        rejected = total - len(accepted)
        if not accepted:
            reasons.append("zero valid training records remained after validation")
        if total > 0 and (rejected / total) > 0.5:
            reasons.append(f"high rejection rate ({rejected}/{total} rejected)")

        if quarantined:
            qdir = self.quarantine_dir / dataset_id / f"v{version}"
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "quarantined.jsonl").write_text(
                "\n".join(json.dumps(q, ensure_ascii=False) for q in quarantined) + "\n",
                encoding="utf-8",
            )

        approved = bool(lic_ok and accepted and len(reasons) == 0)
        fmt_str = ",".join(sorted(formats)) if formats else ("corrupt" if parse_errors else "empty")
        report = ValidationReport(
            dataset_id=dataset_id,
            version=version,
            valid=bool(accepted),
            approved=approved,
            license=norm_lic,
            license_ok=lic_ok,
            total_records=total,
            valid_records=len(accepted),
            rejected_records=rejected,
            format_detected=fmt_str,
            reasons=reasons,
            rejection_counts=counts,
        )
        return report, accepted

