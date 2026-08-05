"""Ollama embedding backend (прод) — без torch/transformers.

OllamaEmbedder из quality/embedder.py: mxbai-embed-large (1024-dim),
base_url из settings.OLLAMA_URL. Backend для EMBEDDING_BACKEND=ollama.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..quality.embedder import OllamaEmbedder, create_embedder

logger = logging.getLogger("mcp_knowledge.embedding.ollama")

_embedder: OllamaEmbedder | None = None


def load_model() -> bool:
    """Загрузить модель Ollama (проверка доступности)."""
    global _embedder
    _embedder = create_embedder(
        model=settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_URL,
    )
    if _embedder is not None:
        logger.info(
            "Ollama embedder ready: %s (dim=%d)",
            _embedder.model,
            _embedder.dim,
        )
    return _embedder is not None


def embed(texts: list[str]) -> list[list[float]]:
    """Эмбеддинг текстов через Ollama."""
    if _embedder is None:
        raise RuntimeError("Ollama embedder не инициализирован")
    return _embedder.encode(texts)


def model_name() -> str:
    """Имя текущей загруженной модели."""
    return _embedder.model if _embedder else settings.OLLAMA_MODEL
