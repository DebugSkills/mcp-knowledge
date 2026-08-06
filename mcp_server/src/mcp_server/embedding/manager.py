"""Embedding Manager: авто-выбор Ollama/GPU/CPU + авто-деградация (#8, #17).

Задачи 1.5, 1.6 плана Фазы 1.

Логика (Фаза 13.5 — ollama first):
- EMBEDDING_BACKEND=ollama → Ollama (mxbai-embed-large, без torch)
- EMBEDDING_BACKEND=gpu → GPU, при отказе — исключение
- EMBEDDING_BACKEND=cpu → CPU
- EMBEDDING_BACKEND=auto → Ollama → GPU → CPU (fallback chain)
"""

from __future__ import annotations

import logging
import time

from ..config import settings
from . import cpu_backend, gpu_backend, ollama_backend

logger = logging.getLogger("mcp_knowledge.embedding.manager")


class EmbeddingManager:
    """Управление embedding-бэкендом (Ollama primary + GPU/CPU fallback)."""

    def __init__(self):
        self._backend = None  # "ollama" | "gpu" | "cpu"
        self._initialized = False
        self._degraded = False  # Ollama недоступна на старте — сервер жив, embed лениво переподключается

    async def initialize(self) -> bool:
        """Инициализировать бэкенд согласно EMBEDDING_BACKEND.

        Ollama/auto: при недоступности — degraded-режим (НЕ падаем: сервер
        поднимается, health показывает embedding loaded=false, первый же
        embed-вызов повторяет попытку подключения к Ollama).
        """
        backend = settings.EMBEDDING_BACKEND.lower()

        if backend == "ollama":
            ok = ollama_backend.load_model()
            if not ok:
                # Инцидент 2026-08-06: сервер уходил в crash-loop без Ollama.
                # Degraded-режим: сервер жив, при старте Ollama — авто-подхват.
                logger.warning(
                    "Ollama недоступна (url=%s, model=%s) — DEGRADED: "
                    "импорт/поиск вернут ошибку до запуска Ollama; "
                    "повторная попытка при первом вызове embed",
                    settings.OLLAMA_URL,
                    settings.OLLAMA_MODEL,
                )
                self._degraded = True
                self._initialized = False
                return True
            self._backend = "ollama"
            logger.info("Embedding: Ollama (mxbai-embed-large, %s)", settings.OLLAMA_URL)

        elif backend == "gpu":
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
            # Фаза 13.5: пробуем Ollama первой (быстро, без скачивания 3 ГБ моделей)
            ollama_ok = ollama_backend.load_model()
            if ollama_ok:
                self._backend = "ollama"
                logger.info("Embedding: Ollama (auto-detected)")
            else:
                # Пробуем GPU
                gpu_ok = gpu_backend.load_model()
                if gpu_ok:
                    self._backend = "gpu"
                    logger.info("Embedding: GPU (auto-detected)")
                else:
                    # Fallback на CPU
                    logger.warning(
                        "Ollama и GPU недоступны — переключаюсь на CPU (WARN)"
                    )
                    cpu_ok = cpu_backend.load_model()
                    if not cpu_ok:
                        # Degraded вместо crash-loop (инцидент 2026-08-06)
                        logger.warning(
                            "EMBEDDING_BACKEND=auto: ни Ollama, ни GPU, ни CPU — DEGRADED "
                            "(сервер жив, embed недоступен до запуска Ollama)"
                        )
                        self._degraded = True
                        self._initialized = False
                        return True
                    self._backend = "cpu"

        else:
            raise ValueError(f"Неизвестный EMBEDDING_BACKEND: {backend}")

        self._initialized = True
        logger.info(
            "EmbeddingManager: backend=%s, initialized=%s",
            self._backend,
            self._initialized,
        )
        return True

    @property
    def backend_name(self) -> str:
        return self._backend or "none"

    @property
    def is_ready(self) -> bool:
        return self._initialized

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Синхронный embed (должен вызываться через run_in_executor).

        Degraded-режим: ленивая повторная инициализация — при запущенной
        Ollama первый же вызов переподключается без рестарта сервера.
        """
        if not self._initialized:
            # Ленивый retry: Ollama могла подняться после старта сервера
            if self._degraded:
                try:
                    ok = ollama_backend.load_model()
                    if ok:
                        self._backend = "ollama"
                        self._initialized = True
                        self._degraded = False
                        logger.info("Embedding: Ollama подключена (lazy retry)")
                except Exception as exc:  # noqa: BLE001  — retry намеренно тихий, ниже понятная ошибка
                    logger.debug("Lazy embed retry failed: %s", exc)
            if not self._initialized:
                raise RuntimeError(
                    "Embedding недоступна: Ollama не запущена "
                    "(выполните: sudo systemctl start ollama)"
                )

        t0 = time.monotonic()
        if self._backend == "ollama":
            vectors = ollama_backend.embed(texts)
        elif self._backend == "gpu":
            vectors = gpu_backend.embed(texts)
        else:
            vectors = cpu_backend.embed(texts)

        # Диагностика производительности: медленный embed (>5 сек) — аномалия
        # (Ollama грузит модель, сеть, большой батч). INFO-уровень для
        # обнаружения деградации в логах (инцидент 2026-08-06).
        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > 5000:
            logger.warning(
                "[EMBED] SLOW %.1fs n=%d",
                elapsed_ms / 1000, len(texts),
            )
        else:
            logger.debug(
                "embed_sync: %.0f ms (%d текстов)", elapsed_ms, len(texts),
            )
        return vectors

    def embed_latency_check(self) -> dict:
        """Проверка latency embedding для /health (задача 1.7)."""
        # Фаза 13.5: model зависит от backend
        if self._backend == "ollama":
            current_model = settings.OLLAMA_MODEL
        else:
            current_model = settings.EMBEDDING_MODEL

        if not self._initialized:
            return {
                "backend": "none",
                "model": current_model,
                "loaded": False,
            }

        try:
            t0 = time.monotonic()
            test_text = ["Проверка доступности embedding-модели"]
            vectors = self.embed_sync(test_text)
            elapsed_ms = (time.monotonic() - t0) * 1000
            return {
                "backend": self._backend,
                "model": current_model,
                "loaded": True,
                "latency_ms": round(elapsed_ms, 1),
                "dim": len(vectors[0]) if vectors else 0,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "backend": self._backend,
                "model": current_model,
                "loaded": False,
                "error": str(exc),
            }

    def get_model_info(self) -> dict:
        """Информация о модели."""
        if self._backend == "ollama":
            return {"backend": "ollama", "model": settings.OLLAMA_MODEL, "loaded": True}
        elif self._backend == "gpu":
            return gpu_backend.get_model_info()
        elif self._backend == "cpu":
            return cpu_backend.get_model_info()
        return {"backend": "none", "loaded": False}
