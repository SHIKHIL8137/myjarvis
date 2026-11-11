"""Provider-independent embedding architecture with version/dimension checks, normalization, and batching."""
import hashlib
import logging
import math
import time
from typing import Protocol

import httpx
import numpy as np

from ..retrieval import tokens

log = logging.getLogger("jarvis.memory.embeddings")


class EmbeddingCompatibilityError(ValueError):
    """Raised when vectors with mismatched dimensions or model versions are mixed in one index."""


class EmbeddingProvider(Protocol):
    model_id: str
    version_tag: str
    dim: int
    is_fallback: bool

    def embed_one(self, text: str) -> np.ndarray: ...
    def embed_batch(self, texts: list[str]) -> list[np.ndarray]: ...


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm > 0.0 and math.isfinite(norm):
        return arr / norm
    return np.zeros_like(arr, dtype=np.float32)


def validate_embedding_vector(vec: np.ndarray, expected_dim: int) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.shape[0] != expected_dim:
        raise EmbeddingCompatibilityError(
            f"Embedding dimension mismatch: got {arr.shape[0]}, expected {expected_dim}"
        )
    if not np.all(np.isfinite(arr)):
        raise ValueError("Embedding vector contains NaN or Inf values")
    return l2_normalize(arr)


def hash_embed(text: str, dim: int = 256) -> np.ndarray:
    """Deterministic local multilingual embedding using word + character trigram feature hashing.
    Maintains Unicode Indic/Malayalam script support as a deterministic fallback (zero GPU VRAM)."""
    vec = np.zeros(dim, dtype=np.float32)
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return vec
    toks = tokens(cleaned)
    for tok in toks:
        idx = int.from_bytes(hashlib.blake2b(f"w:{tok}".encode("utf-8"), digest_size=4).digest(), "little") % dim
        vec[idx] += 2.0
    for i in range(max(0, len(cleaned) - 2)):
        tri = cleaned[i : i + 3]
        if tri.strip():
            idx = int.from_bytes(hashlib.blake2b(f"c:{tri}".encode("utf-8"), digest_size=4).digest(), "little") % dim
            vec[idx] += 1.0
    return l2_normalize(vec)


class DeterministicHashEmbeddingProvider:
    """Deterministic offline fallback embedding provider (explicitly versioned as `hash-v1`)."""

    is_fallback = True

    def __init__(self, dim: int = 256):
        self.dim = int(dim)
        self.model_id = "hash-v1"
        self.version_tag = f"hash-v1:{self.dim}"

    def embed_one(self, text: str) -> np.ndarray:
        return hash_embed(text, dim=self.dim)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [hash_embed(t, dim=self.dim) for t in texts]


class OllamaEmbeddingProvider:
    """Local Ollama embedding provider (`/api/embed` batch with retry + fallback to `/api/embeddings`)."""

    is_fallback = False

    def __init__(
        self,
        *,
        model_id: str,
        dim: int,
        base_url: str = "http://127.0.0.1:11434",
        max_retries: int = 2,
        timeout_s: float = 20.0,
    ):
        self.model_id = model_id
        self.dim = int(dim)
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.max_retries = max(1, int(max_retries))
        self.timeout_s = float(timeout_s)
        self.version_tag = f"ollama:{self.model_id}:{self.dim}"

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(
                        f"{self.base_url}/api/embed",
                        json={"model": self.model_id, "input": texts},
                    )
                    if resp.status_code == 404:
                        # Legacy single-prompt endpoint compatibility
                        out: list[np.ndarray] = []
                        for t in texts:
                            r2 = client.post(
                                f"{self.base_url}/api/embeddings",
                                json={"model": self.model_id, "prompt": t},
                            )
                            r2.raise_for_status()
                            emb = r2.json().get("embedding") or []
                            out.append(validate_embedding_vector(np.array(emb, dtype=np.float32), self.dim))
                        return out
                    resp.raise_for_status()
                    raw = resp.json().get("embeddings") or []
                    if len(raw) != len(texts):
                        raise ValueError(f"Ollama /api/embed count mismatch: expected {len(texts)}, got {len(raw)}")
                    return [validate_embedding_vector(np.array(v, dtype=np.float32), self.dim) for v in raw]
            except EmbeddingCompatibilityError:
                raise
            except Exception as e:
                last_err = e
                if attempt + 1 < self.max_retries:
                    time.sleep(0.15 * (attempt + 1))
        raise RuntimeError(f"Ollama embedding request failed for {self.model_id}: {last_err}") from last_err

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]


class HuggingFaceEmbeddingProvider:
    """Optional local Sentence-Transformers provider (offline-only unless explicitly allowed online)."""

    is_fallback = False

    def __init__(self, *, model_id: str, dim: int, local_files_only: bool = True):
        self.model_id = model_id
        self.dim = int(dim)
        self.local_files_only = local_files_only
        self.version_tag = f"hf:{self.model_id}:{self.dim}"
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("sentence-transformers package is not installed locally") from e
        self._model = SentenceTransformer(self.model_id, local_files_only=self.local_files_only)
        return self._model

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True)
        return [validate_embedding_vector(np.array(v, dtype=np.float32), self.dim) for v in vecs]

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]


def create_embedding_provider(settings=None) -> EmbeddingProvider:
    """Resolve configured embedding provider, falling back explicitly to deterministic hash-v1 only when unconfigured."""
    if settings is None:
        return DeterministicHashEmbeddingProvider(dim=256)
    db_spec = getattr(settings, "database", None)
    learn_spec = getattr(settings, "learning", None)
    prov_kind = (getattr(db_spec, "embedding_provider", "auto") or "auto").lower()
    model_id = (getattr(db_spec, "embedding_model", "") or getattr(learn_spec, "embedding_model", "") or "").strip()
    dim = int(getattr(db_spec, "embedding_dim", 256) or 256)

    if prov_kind == "local_hash":
        return DeterministicHashEmbeddingProvider(dim=dim)

    if prov_kind == "huggingface":
        if not model_id:
            raise ValueError("embedding_model must be configured when embedding_provider='huggingface'")
        local_only = not bool(getattr(getattr(settings, "internet", None), "enabled", False))
        return HuggingFaceEmbeddingProvider(model_id=model_id, dim=dim, local_files_only=local_only)

    if model_id and prov_kind in ("ollama", "auto"):
        base_url = "http://127.0.0.1:11434"
        boot = getattr(settings, "brains", {}).get(getattr(settings, "boot_brain", "local"))
        if boot and getattr(boot, "base_url", ""):
            base_url = boot.base_url
        if prov_kind == "ollama":
            return OllamaEmbeddingProvider(model_id=model_id, dim=dim, base_url=base_url)
        # In "auto" mode: verify model tag exists locally before committing to Ollama embeddings
        try:
            host = base_url.rsplit("/v1", 1)[0].rstrip("/")
            r = httpx.get(f"{host}/api/tags", timeout=1.5)
            if r.status_code == 200:
                names = {m.get("name") for m in (r.json().get("models") or [])}
                if model_id in names or any(str(n).split(":")[0] == model_id for n in names):
                    return OllamaEmbeddingProvider(model_id=model_id, dim=dim, base_url=base_url)
        except Exception:
            pass

    if prov_kind == "ollama" and not model_id:
        raise ValueError("embedding_model must be configured when embedding_provider='ollama'")

    log.debug("Using deterministic fallback embedding provider hash-v1:%d (no Ollama/HF embedding model active)", dim)
    return DeterministicHashEmbeddingProvider(dim=dim)

