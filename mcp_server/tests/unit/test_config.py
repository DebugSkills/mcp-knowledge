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
