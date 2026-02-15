"""Optional text embedding support.

This module is intentionally dependency-light at import time. The heavy
dependencies (sentence-transformers -> torch) are imported lazily only when the
embedding model is first used.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from sentence_transformers import SentenceTransformer


class EmbeddingService:
    """Service for generating text embeddings using local models.

    Default model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
    - Supports 50+ languages (incl. Chinese/English)
    - Output dimension: 384
    """

    MODEL_NAME: ClassVar[str] = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    _model: ClassVar[SentenceTransformer | None] = None

    @classmethod
    def get_model(cls) -> SentenceTransformer:
        if cls._model is not None:
            return cls._model

        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Embedding support is not installed. Install optional deps with:\n"
                "  uv sync --group embeddings\n"
                "Then set PIXAV_EMBEDDINGS_ENABLED=1"
            ) from exc

        logger.info("loading embedding model: %s", cls.MODEL_NAME)
        cls._model = SentenceTransformer(cls.MODEL_NAME)
        return cls._model

    def generate(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * 384

        model = self.get_model()
        embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
