from __future__ import annotations

import math
import threading
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Safe for zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class FunctionEmbeddingProvider:
    def __init__(self, fn: Callable[[str], list[float]], model_name: str = "custom") -> None:
        self._fn = fn
        self._model_name = model_name
        self._dimensions: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            try:
                probe = self._fn("probe")
                self._dimensions = len(probe)
            except Exception:
                self._dimensions = 0
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        return self._fn(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._fn(t) for t in texts]


class LocalEmbeddingProvider:
    _MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    _model = None
    _lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._MODEL_NAME

    @property
    def dimensions(self) -> int:
        return 384

    def _load_model(self) -> None:
        if LocalEmbeddingProvider._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    "sentence-transformers is required for LocalEmbeddingProvider. "
                    "Install it with: pip install 'agentdb[local-embed]'"
                ) from e
            import logging
            logging.getLogger("agentdb").info(
                "agentdb: loading local embedding model (first use)"
            )
            LocalEmbeddingProvider._model = SentenceTransformer(self._MODEL_NAME)

    def embed(self, text: str) -> list[float]:
        with LocalEmbeddingProvider._lock:
            self._load_model()
            vecs = LocalEmbeddingProvider._model.encode(
                [text], normalize_embeddings=True
            )
        return vecs[0].tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with LocalEmbeddingProvider._lock:
            self._load_model()
            vecs = LocalEmbeddingProvider._model.encode(
                texts, normalize_embeddings=True
            )
        return [v.tolist() for v in vecs]
