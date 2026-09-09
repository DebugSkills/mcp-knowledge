"""Тесты настроек Settings (13.19: планировщик скана + лог)."""


from mcp_server.config import Settings


class TestQualityScanCronSettings:
    """Поля планировщика quality-скана (13.19)."""

    def test_cron_defaults(self):
        """Дефолты: enabled=True, hour=3, minute=0, log_dir=/app/data/logs."""
        settings = Settings(_env_file=None)
        assert settings.QUALITY_SCAN_CRON_ENABLED is True
        assert settings.QUALITY_SCAN_CRON_HOUR == 3
        assert settings.QUALITY_SCAN_CRON_MINUTE == 0
        assert settings.QUALITY_SCAN_LOG_DIR == "/app/data/logs"

    def test_cron_env_override(self, monkeypatch):
        """Env-переопределение: QUALITY_SCAN_CRON_HOUR=23, MINUTE=45."""
        monkeypatch.setenv("QUALITY_SCAN_CRON_HOUR", "23")
        monkeypatch.setenv("QUALITY_SCAN_CRON_MINUTE", "45")
        settings = Settings(_env_file=None)
        assert settings.QUALITY_SCAN_CRON_HOUR == 23
        assert settings.QUALITY_SCAN_CRON_MINUTE == 45

    def test_cron_disabled_env(self, monkeypatch):
        """QUALITY_SCAN_CRON_ENABLED=false → планировщик выключен."""
        monkeypatch.setenv("QUALITY_SCAN_CRON_ENABLED", "false")
        settings = Settings(_env_file=None)
        assert settings.QUALITY_SCAN_CRON_ENABLED is False


class TestOllamaTopologyDefaults:
    """Дефолты Ollama-топологии (code-2026-09-08-001: контейнер на 11435).

    11434 занят host-ollama других проектов; после M3 (ollama rm) моделей
    проекта на host нет → bare-запуск без env НЕ должен попадать на 11434.
    """

    def test_ollama_defaults(self, monkeypatch):
        """Дефолты: OLLAMA_URL=11435 (контейнер), chat=qwen2.5:7b, embed=mxbai."""
        # delenv: локальный shell может экспортировать OLLAMA_* (напр. 11434)
        for var in ("OLLAMA_URL", "OLLAMA_MODEL", "OLLAMA_CHAT_MODEL"):
            monkeypatch.delenv(var, raising=False)
        settings = Settings(_env_file=None)
        assert settings.OLLAMA_URL == "http://localhost:11435"
        assert settings.OLLAMA_MODEL == "mxbai-embed-large"
        assert settings.OLLAMA_CHAT_MODEL == "qwen2.5:7b"

    def test_ollama_env_override(self, monkeypatch):
        """Env-переопределение OLLAMA_URL (откат/миграция — 1 строка env)."""
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.delenv("OLLAMA_CHAT_MODEL", raising=False)
        settings = Settings(_env_file=None)
        assert settings.OLLAMA_URL == "http://localhost:11434"


class TestAutoDedupSettings:
    """Фаза 3 (2a): флаги авто-deprecate."""

    def test_auto_dedup_defaults(self):
        """Дефолты: OFF, FP=2 скана, cooldown=3, cap=100."""
        settings = Settings(_env_file=None)
        assert settings.AUTO_DEDUP_ENABLED is False
        assert settings.AUTO_DEDUP_FP_FREE_SCANS == 2
        assert settings.AUTO_DEDUP_RESTORE_COOLDOWN_SCANS == 3
        assert settings.AUTO_DEDUP_MAX_PER_SCAN == 100

    def test_auto_dedup_env_override(self, monkeypatch):
        """Env-переопределение AUTO_DEDUP_ENABLED=true + cap=50."""
        monkeypatch.setenv("AUTO_DEDUP_ENABLED", "true")
        monkeypatch.setenv("AUTO_DEDUP_MAX_PER_SCAN", "50")
        settings = Settings(_env_file=None)
        assert settings.AUTO_DEDUP_ENABLED is True
        assert settings.AUTO_DEDUP_MAX_PER_SCAN == 50
