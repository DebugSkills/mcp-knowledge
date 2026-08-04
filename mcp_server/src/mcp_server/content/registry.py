"""Content Preprocessor Registry — content_type → preprocessor.

Фаза 5 §3.1: registry как точка расширения (OCP).
Новый content_type = новый класс + 1 строка `register()`.
"""

from __future__ import annotations

from .preprocessor import ContentPreprocessor

_registry: dict[str, ContentPreprocessor] = {}


def register(preprocessor: ContentPreprocessor) -> None:
    """Зарегистрировать препроцессор для заданного content_type."""
    ct = preprocessor.content_type
    if ct in _registry:
        raise ValueError(
            f"Preprocessor for content_type='{ct}' already registered"
        )
    _registry[ct] = preprocessor


def get(content_type: str) -> ContentPreprocessor:
    """Получить препроцессор по content_type.

    Raises:
        ValueError: если content_type неизвестен, сообщение содержит список доступных типов.
    """
    preprocessor = _registry.get(content_type)
    if preprocessor is None:
        available = list(_registry.keys())
        raise ValueError(
            f"Unknown content_type: '{content_type}'. "
            f"Available types: {available}"
        )
    return preprocessor


def list_types() -> list[str]:
    """Вернуть список всех зарегистрированных content_type."""
    return sorted(_registry.keys())


def reset() -> None:
    """Очистить реестр (для тестов)."""
    _registry.clear()
