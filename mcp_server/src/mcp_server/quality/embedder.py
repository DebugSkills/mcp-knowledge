"""Ollama embedder adapter — замена BGE-M3 для air-gap среды (4.3 дополнение).

Использует локальный Ollama API вместо sentence-transformers:
- mxbai-embed-large (1024-dim, 669 MB) — primary (русская семантика OK)
- nomic-embed-text (768-dim, 274 MB) — fallback (англоцентричный; для collection dim 1024
  использовать НЕЛЬЗЯ — только если коллекции пересозданы под 768)
- bge-m3 (1024-dim) — проверен 2026-08-19: сборка F16 из registry.ollama.ai даёт NaN
  на реальных текстах ("json: unsupported value: NaN") — НЕ использовать.

Преимущества:
- Нет зависимостей torch/transformers (только httpx/requests)
- Air-gap: Ollama работает полностью офлайн
- 1024-dim векторы совместимы с Qdrant (авто-ресайз при миграции)

API: POST http://localhost:11434/api/embed
"""

from __future__ import annotations

import logging

logger = logging.getLogger("mcp_knowledge.quality.embedder")

# ── Конфигурация ─────────────────────────────────────────────

OLLAMA_BASE_URL: str = "http://localhost:11434"
DEFAULT_EMBED_MODEL: str = "mxbai-embed-large"
FALLBACK_EMBED_MODEL: str = "nomic-embed-text"

# Известные размерности (для валидации)
MODEL_DIMS: dict[str, int] = {
    "mxbai-embed-large": 1024,
    "nomic-embed-text": 768,
    "bge-m3": 1024,
}


class OllamaEmbedder:
    """Адаптер для Ollama Embeddings API.

    Совместим с интерфейсом SentenceTransformer:
    - encode(text) → list[float]
    - encode([text, ...]) → list[list[float]]
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        base_url: str = OLLAMA_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._dim: int | None = MODEL_DIMS.get(model)

    @property
    def dim(self) -> int:
        """Размерность векторов."""
        return self._dim or 0

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        """Эмбеддинг текста(ов) через Ollama API.

        Args:
            text: строка или список строк.

        Returns:
            list[float] для одной строки, list[list[float]] для списка.
        """
        single = isinstance(text, str)
        inputs = [text] if single else text

        if not inputs:
            return [] if single else [[]]

        try:
            import httpx
        except ImportError as exc:
            logger.error("httpx is required for Ollama embedder: %s", exc)
            raise RuntimeError("httpx is required for Ollama embedder") from exc

        try:
            # truncate=True: Ollama обрезает вход до контекста модели (иначе
            # длинные секции/чанки → 400 "input length exceeds context length").
            payload = {"model": self.model, "input": inputs, "truncate": True}
            response = httpx.post(
                f"{self.base_url}/api/embed",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings", [])

            if not embeddings:
                raise RuntimeError(f"Ollama returned empty embeddings for model {self.model}")

            if single:
                return embeddings[0]
            return embeddings

        except Exception as exc:
            logger.error("Ollama embed failed for model %s: %s", self.model, exc)
            raise


def create_embedder(
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = OLLAMA_BASE_URL,
) -> OllamaEmbedder | None:
    """Фабрика эмбеддера с проверкой доступности Ollama.

    Пробует primary-модель, при неудаче — fallback.
    Возвращает None если ни одна не доступна.
    """
    for attempt_model in (model, FALLBACK_EMBED_MODEL):
        try:
            emb = OllamaEmbedder(model=attempt_model, base_url=base_url)
            test_vec = emb.encode("test")
            if test_vec and len(test_vec) > 0:
                logger.info("Embedder ready: %s (dim=%d)", attempt_model, len(test_vec))
                return emb
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedder %s not available: %s", attempt_model, exc)

    return None
