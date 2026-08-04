"""BGE-M3 GPU backend через sentence-transformers (#3, #17).

Задача 1.5 плана Фазы 1.

В production: CUDA через --gpus all (nvidia-container-toolkit).
Вызов embed() через loop.run_in_executor (не блокирует event loop).
"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("mcp_knowledge.embedding.gpu")

_model = None


def load_model() -> bool:
    """Загрузить BGE-M3 на GPU. Возвращает True если успешно."""
    global _model
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Загрузка BGE-M3 на GPU: %s", settings.EMBEDDING_MODEL)
        _model = SentenceTransformer(
            settings.EMBEDDING_MODEL,
            device="cuda",
            cache_folder=settings.MODELS_CACHE_DIR,
        )
        # Прогрев модели
        _ = _model.encode(["warmup"], show_progress_bar=False)
        logger.info("BGE-M3 GPU загружен (dim=%d)", _model.get_sentence_embedding_dimension())
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("GPU-загрузка BGE-M3 не удалась: %s", e)
        _model = None
        return False


def is_available() -> bool:
    """Проверка доступности GPU-бэкенда."""
    return _model is not None


def embed(texts: list[str]) -> list[list[float]]:
    """Эмбеддинг BGE-M3 на GPU.

    Вызывается через loop.run_in_executor()!
    """
    if _model is None:
        raise RuntimeError("GPU backend не загружен")
    vectors = _model.encode(
        texts,
        normalize_embeddings=True,  # для cosine distance
        show_progress_bar=False,
        batch_size=32,
    )
    return vectors.tolist()


def get_model_info() -> dict:
    """Информация о модели для /health."""
    if _model is None:
        return {"backend": "gpu", "loaded": False}
    return {
        "backend": "gpu",
        "loaded": True,
        "model": settings.EMBEDDING_MODEL,
        "dim": _model.get_sentence_embedding_dimension(),
        "device": str(_model.device),
    }
