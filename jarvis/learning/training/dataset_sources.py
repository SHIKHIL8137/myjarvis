"""Dataset source connectors & curated/Hugging Face dataset discovery (Phase 5)."""
from dataclasses import asdict, dataclass
import json
from urllib.parse import quote

from ... import audit
from ...config import InternetSpec, LearningSpec
from ...netsec import safe_request
from ...search import SearchOfflineError
from .dataset_validator import normalize_license


@dataclass
class DatasetSourceSpec:
    id: str
    name: str
    kind: str  # "approved_feedback" | "local_jsonl" | "huggingface_dataset" | "curated_bundle"
    location: str
    license: str = "unknown"
    description: str = ""
    sha256: str = ""
    format: str = "auto"  # "auto" | "alpaca" | "messages"
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_DATASET_SOURCES: list[DatasetSourceSpec] = [
    DatasetSourceSpec(
        id="local-approved-feedback",
        name="Jarvis Approved Feedback Examples",
        kind="approved_feedback",
        location="data/learning/feedback/records.jsonl",
        license="mit",
        description="User-approved corrections, style preferences, and workflow lessons.",
        format="alpaca",
    ),
]


class DatasetSourceCatalog:
    """Manages registered dataset descriptors and gated Hugging Face dataset discovery."""

    def __init__(self, spec: LearningSpec | None = None, *, is_online_fn=lambda: False):
        self.spec = spec or LearningSpec()
        self.is_online_fn = is_online_fn
        self._sources: dict[str, DatasetSourceSpec] = {s.id: s for s in DEFAULT_DATASET_SOURCES}
        for raw in self.spec.dataset_sources:
            if isinstance(raw, dict) and raw.get("id") and raw.get("location"):
                try:
                    self.register(DatasetSourceSpec(**{k: raw[k] for k in DatasetSourceSpec.__dataclass_fields__ if k in raw}))
                except Exception:
                    pass

    def register(self, src: DatasetSourceSpec) -> DatasetSourceSpec:
        self._sources[src.id] = src
        return src

    def list_sources(self) -> list[dict]:
        return [s.to_dict() for s in self._sources.values()]

    async def discover_huggingface(self, query: str, *, limit: int = 8) -> list[dict]:
        """Search Hugging Face public datasets API ONLY in Online Assisted Mode via netsec."""
        if not (self.is_online_fn() and audit.is_online_enabled()):
            raise SearchOfflineError("Local Mode active: Hugging Face dataset discovery requires Online Assisted Mode.")
        url = f"https://huggingface.co/api/datasets?search={quote(query.strip())}&limit={max(1, min(limit, 20))}"
        net_spec = InternetSpec(enabled=True, max_fetch_bytes=2_000_000, timeout_s=8.0)
        res = await safe_request("GET", url, net_spec, allowed_hosts=self.spec.allowed_knowledge_hosts, max_bytes=2_000_000)
        items = json.loads(res.body.decode("utf-8", errors="replace"))
        out: list[dict] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            repo_id = str(item.get("id") or "").strip()
            if not repo_id:
                continue
            lic = "unknown"
            card = item.get("cardData") or {}
            if isinstance(card, dict) and card.get("license"):
                raw_lic = card["license"]
                lic = str(raw_lic[0] if isinstance(raw_lic, list) and raw_lic else raw_lic)
            else:
                for tag in item.get("tags") or []:
                    if isinstance(tag, str) and tag.startswith("license:"):
                        lic = tag.split(":", 1)[1]
                        break
            out.append({
                "id": f"hf-{repo_id.replace('/', '--')}",
                "repo_id": repo_id,
                "name": repo_id,
                "kind": "huggingface_dataset",
                "location": f"https://huggingface.co/datasets/{repo_id}/resolve/main/data.jsonl",
                "license": normalize_license(lic),
                "downloads": int(item.get("downloads") or 0),
                "likes": int(item.get("likes") or 0),
                "lastModified": item.get("lastModified") or "",
            })
        return out

