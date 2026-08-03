"""Unit-тесты для quality/lifecycle.py (4.7)."""

from __future__ import annotations

from mcp_server.quality.lifecycle import (
    DEFAULT_STATUS,
    QDRANT_PAYLOAD_KEY,
    build_search_filter,
    get_status,
    make_deprecation_payload_update,
    make_published_payload_update,
    make_restore_payload_update,
    validate_transition,
)


class TestGetStatus:
    """Извлечение статуса из Qdrant payload."""

    def test_missing_payload_defaults_to_published(self):
        """Нет payload → published (backward-compatible)."""
        assert get_status(None) == DEFAULT_STATUS
        assert get_status({}) == DEFAULT_STATUS

    def test_explicit_published(self):
        assert get_status({QDRANT_PAYLOAD_KEY: "published"}) == "published"

    def test_explicit_deprecated(self):
        assert get_status({QDRANT_PAYLOAD_KEY: "deprecated"}) == "deprecated"

    def test_unknown_value_defaults_to_published(self):
        """Неизвестное значение → published (защита)."""
        assert get_status({QDRANT_PAYLOAD_KEY: "garbage"}) == DEFAULT_STATUS
        assert get_status({QDRANT_PAYLOAD_KEY: 123}) == DEFAULT_STATUS

    def test_mixed_payload_works(self):
        """Другие ключи не мешают."""
        payload = {QDRANT_PAYLOAD_KEY: "deprecated", "other": "data"}
        assert get_status(payload) == "deprecated"


class TestBuildSearchFilter:
    """Построение Qdrant search-фильтра."""

    def test_default_excludes_deprecated(self):
        f = build_search_filter(include_deprecated=False)
        assert f is not None
        assert "must_not" in f
        # Проверяем структуру (без импорта qdrant_client)
        assert isinstance(f["must_not"], list)
        assert len(f["must_not"]) == 1

    def test_include_deprecated_no_filter(self):
        f = build_search_filter(include_deprecated=True)
        assert f is None

    def test_backward_compatible_default(self):
        """Вызов без аргументов = exclude deprecated."""
        f = build_search_filter()
        assert f is not None


class TestValidateTransition:
    """Валидация переходов статуса."""

    def test_published_to_deprecated_ok(self):
        assert validate_transition("published", "deprecated") is None

    def test_deprecated_to_published_ok(self):
        """Restore: deprecated → published (reversibility)."""
        assert validate_transition("deprecated", "published") is None

    def test_same_status_no_change(self):
        """Уже в целевом статусе — ошибка."""
        err = validate_transition("deprecated", "deprecated")
        assert err is not None
        assert "Already" in err

        err = validate_transition("published", "published")
        assert err is not None
        assert "Already" in err


class TestPayloadUpdates:
    """Фабрики Qdrant payload-обновлений."""

    def test_deprecation_payload(self):
        p = make_deprecation_payload_update()
        assert p[QDRANT_PAYLOAD_KEY] == "deprecated"

    def test_restore_payload(self):
        p = make_restore_payload_update()
        assert p[QDRANT_PAYLOAD_KEY] == "published"

    def test_published_payload(self):
        p = make_published_payload_update()
        assert p[QDRANT_PAYLOAD_KEY] == DEFAULT_STATUS


class TestDefaults:
    """Значения по умолчанию."""

    def test_default_is_published(self):
        assert DEFAULT_STATUS == "published"

    def test_payload_key_constant(self):
        assert QDRANT_PAYLOAD_KEY == "status"
