"""
Lightweight semantic embeddings for title-similarity dedup.

Deliberately avoids PyTorch + sentence-transformers. Uses fastembed
(Qdrant), which runs on ONNX Runtime instead of torch — same quality
embeddings, a fraction of the dependency size, and no CUDA bloat.
This matters even on Azure with headroom: smaller Docker images,
faster builds, faster worker cold start.

  torch + sentence-transformers install size: ~600-900MB
  fastembed (onnxruntime) install size:       ~150-200MB
  Model itself (BAAI/bge-small-en-v1.5, ONNX): ~130MB, loaded once
"""
from app.core.logging import get_logger

log = get_logger(__name__)

_MODEL_NAME = "BAAI/bge-small-en-v1.5"


class EmbeddingService:
    """Process-level singleton — model loads once per worker process, reused forever."""

    _model = None

    @classmethod
    def _get_model(cls):
        if cls._model is None:
            from fastembed import TextEmbedding
            log.info("embedding_model_loading", model=_MODEL_NAME)
            cls._model = TextEmbedding(model_name=_MODEL_NAME)
            log.info("embedding_model_ready")
        return cls._model

    @classmethod
    def embed(cls, text: str) -> list[float]:
        return cls.embed_batch([text])[0]

    @classmethod
    def embed_batch(cls, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = cls._get_model()
        return [vec.tolist() for vec in model.embed(texts)]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot    = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
