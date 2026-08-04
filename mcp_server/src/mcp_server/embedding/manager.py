"""Embedding Manager: авто-выбор GPU/CPU + авто-деградация (#8, #17).

Задачи 1.5, 1.6 плана Фазы 1.

Логика:
- EMBEDDING_BACKEND=gpu → GPU, при отказе — исключение
- EMBEDDING_BACKEND=cpu → CPU
- EMBEDDING_BACKEND=auto → GPU → при отказе CPU с WARN
"""

from __future__ import annotations

import logging
import time

from ..config import settings
from . import cpu_backend, gpu_backend

logger = logging.getLogger("mcp_knowledge.embedding.manager")


class EmbeddingManager:
    """Управление embedding-бэкендом (GPU primary + CPU fallback)."""

    def __init__(self):
        self._backend = None  # "gpu" | "cpu"
        self._initialized = False

    async def initialize(self) -> bool:
        """Инициализировать бэкенд согласно EMBEDDING_BACKEND."""
        backend = settings.EMBEDDING_BACKEND.lower()

        if backend == "gpu":
            ok = gpu_backend.load_model()
            if not ok:
                raise RuntimeError("EMBEDDING_BACKEND=gpu, но GPU-модель не загрузилась")
            self._backend = "gpu"

        elif backend == "cpu":
            ok = cpu_backend.load_model()
            if not ok:
                raise RuntimeError("EMBEDDING_BACKEND=cpu, но CPU-модель не загрузилась")
            self._backend = "cpu"

        elif backend == "auto":
            # Пробуем GPU
            gpu_ok = gpu_backend.load_model()
            if gpu_ok:
                self._backend = "gpu"
                logger.info("Embedding: GPU (auto-detected)")
            else:
                # Fallback на CPU
                logger.warning("GPU недоступен — переключаюсь на CPU (WARN)")
                cpu_ok = cpu_backend.load_model()
                if not cpu_ok:
                    raise RuntimeError("EMBEDDING_BACKEND=auto: ни GPU, ни CPU не загрузились")
                self._backend = "cpu"

        else:
            raise ValueError(f"Неизвестный EMBEDDING_BACKEND: {backend}")

        self._initialized = True
        logger.info("EmbeddingManager: backend=%s, initialized=%s", self._backend, self._initialized)
        return True

    @property
    def backend_name(self) -> str:
        return self._backend or "none"

    @property
    def is_ready(self) -> bool:
        return self._initialized

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Синхронный embed (должен вызываться через run_in_executor)."""
        if not self._initialized:
            raise RuntimeError("EmbeddingManager не инициализирован")

        if self._backend == "gpu":
            return gpu_backend.embed(texts)
        else:
            return cpu_backend.embed(texts)

    def embed_latency_check(self) -> dict:
        """Проверка latency embedding для /health (задача 1.7)."""
        if not self._initialized:
            return {"backend": "none", "model": settings.EMBEDDING_MODEL, "loaded": False}

        try:
            t0 = time.monotonic()
            test_text = ["Проверка доступности embedding-модели"]
            vectors = self.embed_sync(test_text)
            elapsed_ms = (time.monotonic() - t0) * 1000
            return {
                "backend": self._backend,
                "model": settings.EMBEDDING_MODEL,
                "loaded": True,
                "latency_ms": round(elapsed_ms, 1),
                "dim": len(vectors[0]) if vectors else 0,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "backend": self._backend,
                "model": settings.EMBEDDING_MODEL,
                "loaded": False,
                "error": str(e),
            }

    def get_model_info(self) -> dict:
        """Информация о модели."""
        if self._backend == "gpu":
            return gpu_backend.get_model_info()
        elif self._backend == "cpu":
            return cpu_backend.get_model_info()
        return {"backend": "none", "loaded": False}
