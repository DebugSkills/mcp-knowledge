"""BGE-M3 CPU fallback через torch-CPU (#8, #17).

Задача 1.6 плана Фазы 1.

Вызов embed() через loop.run_in_executor (600–1000 мс/чанк на CPU).

Примечание (P1-1 Critic): готовая ONNX-модель BGE-M3 отсутствует в открытом доступе.
Для MVP используем sentence-transformers на CPU через torch — даёт те же векторы,
что и GPU-версия. ONNX Runtime остаётся в зависимостях для будущей конверсии.
"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("mcp_knowledge.embedding.cpu")

_model = None
_tokenizer = None


def load_model() -> bool:
    """Загрузить BGE-M3 через ONNX Runtime на CPU.

    Примечание (P1-1 Critic): готовая ONNX-модель BGE-M3 может отсутствовать.
    Для MVP используем sentence-transformers на CPU через torch — даёт те же векторы,
    что и GPU-версия (решение P1-1: torch-CPU вместо ONNX для MVP).
    """
    global _model
    try:
        import torch
        from sentence_transformers import SentenceTransformer

        logger.info("Загрузка BGE-M3 на CPU (torch): %s", settings.EMBEDDING_MODEL)
        # Принудительно CPU
        _model = SentenceTransformer(
            settings.EMBEDDING_MODEL,
            device="cpu",
            cache_folder=settings.MODELS_CACHE_DIR,
        )
        # Прогрев
        _ = _model.encode(["warmup"], show_progress_bar=False)
        logger.info("BGE-M3 CPU загружен (dim=%d, threads=%d)",
                     _model.get_sentence_embedding_dimension(),
                     torch.get_num_threads())
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("CPU-загрузка BGE-M3 не удалась: %s", e)
        _model = None
        return False


def is_available() -> bool:
    """Проверка доступности CPU-бэкенда."""
    return _model is not None


def embed(texts: list[str]) -> list[list[float]]:
    """Эмбеддинг BGE-M3 на CPU.

    Вызывается через loop.run_in_executor()!
    600–1000 мс/чанк (batch ×8 для throughput).
    """
    if _model is None:
        raise RuntimeError("CPU backend не загружен")
    vectors = _model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=8,  # CPU: маленький батч для меньшей латентности
    )
    return vectors.tolist()


def get_model_info() -> dict:
    """Информация о модели для /health."""
    if _model is None:
        return {"backend": "cpu", "loaded": False}
    return {
        "backend": "cpu",
        "loaded": True,
        "model": settings.EMBEDDING_MODEL,
        "dim": _model.get_sentence_embedding_dimension(),
        "device": str(_model.device),
    }
