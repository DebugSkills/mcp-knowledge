"""Настройки MCP Knowledge Server (pydantic-settings)."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Qdrant
    QDRANT_URL: str = "http://qdrant:6334"
    QDRANT_COLLECTION: str = "knowledge"

    # Embedding
    EMBEDDING_BACKEND: str = "auto"  # auto | cpu | gpu
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024
    MODELS_CACHE_DIR: str = "/app/models_cache"

    # MCP Auth (мульти-ключи #18)
    MCP_READ_KEYS: List[str] = []
    MCP_WRITE_KEYS: List[str] = []

    # Git audit (#21) + SSOT
    GIT_AUDIT: bool = True
    KNOWLEDGE_DIR: str = "/app/knowledge"
    KNOWLEDGE_ROOT: str = "/app/knowledge"  # Псевдоним для MarkdownStore (тот же путь)

    # Chunking (#13, #20)
    CHUNK_MAX_TOKENS: int = 512
    CHUNK_OVERLAP: int = 80

    # Pipeline
    WORKERS: int = 1  # ИНВАРИАНТ — не менять!

    # Rate limiting (Фаза 3 E2: token bucket, per-key)
    RATE_LIMIT_READ_PER_MIN: int = 100   # read-ключ: 100 запросов/мин (≈1.67 токенов/сек)
    RATE_LIMIT_WRITE_PER_MIN: int = 20   # write-ключ: 20 запросов/мин (≈0.33 токенов/сек)

    # DLQ (#14)
    DLQ_DIR: str = "/app/data/dlq"
    DLQ_MAX_RETRIES: int = 3

    # Quality (Фаза 4)
    QUALITY_DIR: str = "/app/data/quality"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.WORKERS != 1:
            raise ValueError(
                f"WORKERS={self.WORKERS}, ожидается 1. "
                "In-memory состояние (asyncio.Queue, sync_barrier) не переживает >1 worker."
            )


settings = Settings()
