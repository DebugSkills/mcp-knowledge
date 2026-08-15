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
