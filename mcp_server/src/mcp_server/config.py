"""Настройки MCP Knowledge Server (pydantic-settings)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Qdrant
    QDRANT_URL: str = "http://qdrant:6334"
    QDRANT_PREFER_GRPC: bool = True  # false → REST (для хостового qdrant, gRPC 6334 не проброшен)
    QDRANT_COLLECTION: str = "knowledge"

    # Embedding
    EMBEDDING_BACKEND: str = "auto"  # auto | ollama | gpu | cpu
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024  # mxbai-embed-large (русская семантика); bge-m3 F16 из Ollama — NaN-баг, отбракован
    MODELS_CACHE_DIR: str = "/app/models_cache"
    # 11435 — ollama-КОНТЕЙНЕР этого compose (bridge 127.0.0.1:11435:11434);
    # 11434 занят host-ollama других проектов (после миграции моделей проекта там нет).
    OLLAMA_URL: str = "http://localhost:11435"
    OLLAMA_MODEL: str = "mxbai-embed-large"

    # Content Analysis (Фаза 13.8: AI-рекомендации)
    # qwen2.5:7b — chat-модель в ollama-контейнере (ТЕКСТОВАЯ; qwen2.5vl vision
    # не влезает в 8GB GPU на ollama 0.20.2; analyzer шлёт текст-онли).
    OLLAMA_CHAT_MODEL: str = "qwen2.5:7b"
    ANALYZE_FRAGMENT_CHARS: int = 8000
    ANALYZE_TIMEOUT: float = 60.0
    ANALYZE_LLM_ENABLED: bool = True
    ANALYZE_LLM_NUM_CTX: int = 4096
    ANALYZE_MAX_TAGS: int = 10

    # MCP Auth (мульти-ключи #18)
    MCP_READ_KEYS: list[str] = []
    MCP_WRITE_KEYS: list[str] = []
    # Import-ключи (Фаза 13.8): read-tools + import_content (без delete/reindex/write).
    # Используется kb-console: импорт учебников без полного write-доступа.
    MCP_IMPORT_KEYS: list[str] = []
    # Ключ kb-console (лежит в .env рядом с ключами сервера; сам сервер его НЕ использует —
    # консоль шлёт его как X-API-Key. Поле нужно, чтобы pydantic не падал на extra_forbidden).
    MCP_API_KEY: str = ""

    # Git audit (#21) + SSOT
    GIT_AUDIT: bool = True
    KNOWLEDGE_DIR: str = "/app/knowledge"
    KNOWLEDGE_ROOT: str = "/app/knowledge"  # Псевдоним для MarkdownStore (тот же путь)

    # Chunking (#13, #20)
    # 120 токенов × ~4 симв/токен (fallback) ≈ 480 символов + заголовок секции ≤ ~540c.
    # Лимит mxbai-embed-large = 512 токенов (~650 символов русского) — чанк обязан
    # влезать БЕЗ обрезки, иначе Ollama 500/NaN. Был 512 (чанки до 2048c — не влезали).
    CHUNK_MAX_TOKENS: int = 120
    CHUNK_OVERLAP: int = 80

    # Pipeline
    WORKERS: int = 1  # ИНВАРИАНТ — не менять!

    # Rate limiting (Фаза 3 E2: token bucket, per-key)
    RATE_LIMIT_READ_PER_MIN: int = 100   # read-ключ: 100 запросов/мин (≈1.67 токенов/сек)
    RATE_LIMIT_WRITE_PER_MIN: int = 20   # write-ключ: 20 запросов/мин (≈0.33 токенов/сек)
    # Subscriber-ключ (W3, двухконтурная модель доступа): 45 req/min (решение оператора)
    RATE_LIMIT_SUBSCRIBER_PER_MIN: int = 45

    # DLQ (#14)
    DLQ_DIR: str = "/app/data/dlq"
    DLQ_MAX_RETRIES: int = 3

    # Quality (Фаза 4)
    QUALITY_DIR: str = "/app/data/quality"

    # Token store (W3, план two-zone-access §2.3) — SSOT токенов доступа.
    # TOKENS_DIR: паттерн QUALITY_DIR (pydantic-settings → env-override);
    # локально /app недоступен → тесты задают TokenStore(tokens_dir=...) или env.
    TOKENS_DIR: str = "/app/data/tokens"
    TOKEN_INDEX_TTL_SEC: int = 5  # TTL in-memory индекса (перечитывание файла)

    # Quality scan scheduler (13.19) — ночной периодический скан ВНУТРИ контейнера
    QUALITY_SCAN_CRON_ENABLED: bool = True
    QUALITY_SCAN_CRON_HOUR: int = 3
    QUALITY_SCAN_CRON_MINUTE: int = 0
    # Лог сканирования (13.19) — файл в /app/data/logs (volume → хост)
    QUALITY_SCAN_LOG_DIR: str = "/app/data/logs"

    # ── Dedup auto-deprecate (Фаза 3) ──────────────────────
    # OFF по умолчанию (блокер 2): авто-скрытие включается только явно (env).
    AUTO_DEDUP_ENABLED: bool = False
    # FP=0 за ≥N ПОЛНЫХ сканов (scan_completed в audit) открывает гейт.
    AUTO_DEDUP_FP_FREE_SCANS: int = 2
    # restore оператором исключает запись из авто на N сканов (cooldown-щит).
    AUTO_DEDUP_RESTORE_COOLDOWN_SCANS: int = 3
    # cap авто-скрытий за один скан (защита от массового скрытия, P2-10).
    AUTO_DEDUP_MAX_PER_SCAN: int = 100

    # MCP request size limit (Фаза 13.21 P1-2)
    # 128 МБ default: безопасный баланс при mem_limit 2g.
    # Memory analysis: baseline ~120MB, JSON parsing 2-3x → пик ~670MB.
    # 256MB было бы рискованно (~1220MB пик при concurrent embedding).
    MCP_MAX_REQUEST_SIZE: int = 134_217_728  # 128 МБ

    # Periodic git-commit during import (Фаза 13.21 P1-5)
    # Каждые N секций — промежуточный git-commit через store.flush().
    # При git-ошибке: warning и продолжение без коммита (non-fatal).
    # Финальный flush ВСЕГДА в конце импорта.
    IMPORT_PERIODIC_COMMIT: int = 100

    # ── PDF Import (13.21) ───────────────────────────────────
    # Лимиты размера и страниц для PDF-импорта (валидация в PDFPreprocessor)
    MAX_PDF_PAGES: int = 2000
    MAX_PDF_FILE_SIZE: int = 104_857_600  # 100 MB

    # Кеш извлечённого текста PDF (resume checkpoint — Фаза 2.2)
    PDF_IMPORT_CACHE_DIR: str = "/app/data/pdf_cache"

    # [P0-2] Checkpoint cleanup: предотвращение disk exhaustion
    PDF_IMPORT_CACHE_MAX_AGE_DAYS: int = 30
    PDF_IMPORT_CACHE_MAX_SIZE_MB: int = 500

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.WORKERS != 1:
            raise ValueError(
                f"WORKERS={self.WORKERS}, ожидается 1. "
                "In-memory состояние (asyncio.Queue, sync_barrier) не переживает >1 worker."
            )


settings = Settings()
