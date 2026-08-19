"""W5.4: тесты страницы «Токены» (чистые функции, без NiceGUI-рантайма).

Покрывает: бейджи статуса (active/revoked/expired/expiring), баннер
«скоро деактивация» (Q9 v1.7), предпросмотр/расшифровку префикса (v1.5),
маппинг бейджей уровня/зоны (v1.3), фильтрацию по статусу.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kb_console.pages.tokens import (
    DEACTIVATE_DAYS,
    WARNING_WINDOW_DAYS,
    _days_old,
    _prefix,
    _prefix_decode,
    _stale_banner_rows,
    _status_badge,
)


def _rec(**overrides) -> dict:
    base = {
        "id": "tok_test1",
        "level": "subscriber",
        "zone": "public",
        "active": True,
        "expires_at": None,
        "note": "",
        "source": "manual",
        "created_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
        "last_used_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "mask": "mcp_sa_****",
    }
    base.update(overrides)
    return base


def _iso(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# ── статус-бейджи (v1.3) ─────────────────────────────────────


def test_status_active():
    label, color = _status_badge(_rec())
    assert label == "✅ active"
    assert color == "green"


def test_status_revoked():
    label, color = _status_badge(_rec(active=False))
    assert "revoked" in label
    assert color == "grey"


def test_status_expired():
    label, color = _status_badge(_rec(expires_at=_iso(1)))  # вчера
    assert "expired" in label
    assert color == "grey"


def test_status_expiring_soon():
    label, color = _status_badge(_rec(expires_at=_iso(-5)))  # через 5 дней
    assert "expires" in label
    assert color == "orange"


def test_status_stale_subscriber_warns():
    """Q9: subscriber без last_used_at, создан 83+ дней назад → ⚠️."""
    rec = _rec(created_at=_iso(WARNING_WINDOW_DAYS + 5), last_used_at=None)
    label, color = _status_badge(rec)
    assert "неактивен" in label
    assert color == "orange"


def test_status_read_not_warned_for_stale():
    """Q9: read-токен не предупреждается (свеп только subscriber)."""
    rec = _rec(level="read", created_at=_iso(200), last_used_at=None)
    label, _ = _status_badge(rec)
    assert label == "✅ active"


# ── баннер «скоро деактивация» (Q9 v1.7) ─────────────────────


def test_stale_banner_rows_empty_for_fresh_tokens():
    rows = _stale_banner_rows([
        _rec(last_used_at=_iso(1)),          # свежий
        _rec(level="read", last_used_at=None, created_at=_iso(200)),  # не subscriber
    ])
    assert rows == []


def test_stale_banner_rows_finds_old_subscriber():
    rows = _stale_banner_rows([
        _rec(last_used_at=_iso(WARNING_WINDOW_DAYS + 2)),
        _rec(last_used_at=_iso(1)),
    ])
    assert len(rows) == 1
    assert rows[0]["rec"]["id"] == "tok_test1"
    assert rows[0]["days"] >= WARNING_WINDOW_DAYS


def test_stale_banner_uses_created_at_fallback():
    rows = _stale_banner_rows([
        _rec(last_used_at=None, created_at=_iso(DEACTIVATE_DAYS + 10)),
    ])
    assert len(rows) == 1


def test_stale_banner_skips_revoked():
    rows = _stale_banner_rows([
        _rec(active=False, last_used_at=_iso(WARNING_WINDOW_DAYS + 10)),
    ])
    assert rows == []


# ── префикс (v1.5) ───────────────────────────────────────────


def test_prefix_mapping():
    assert _prefix(_rec(level="subscriber", zone="public")) == "mcp_sa_"
    assert _prefix(_rec(level="read", zone="private")) == "mcp_rb_"
    assert _prefix(_rec(level="import", zone="both")) == "mcp_ix_"
    assert _prefix(_rec(level="write", zone="public")) == "mcp_wa_"


def test_prefix_decode_tooltip():
    d = _prefix_decode(_rec(level="subscriber", zone="public"))
    assert d == "mcp_sa_ → 🟢 подписчик, public"


# ── хелперы ──────────────────────────────────────────────────


def test_days_old_parses_iso():
    assert _days_old(_iso(3), datetime.now(UTC)) is not None
    assert _days_old(None, datetime.now(UTC)) is None
    assert _days_old("garbage", datetime.now(UTC)) is None


def test_days_old_handles_z_suffix():
    """py3.11: fromisoformat не парсит 'Z' — replace обязателен (noqa FURB162)."""
    assert _days_old(
        (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        datetime.now(UTC),
    ) is not None


# ── регрессия: expires_at без зоны (naive datetime) ───────────


def test_days_old_naive_expires_at_no_zone():
    """Регрессия: expires_at без зоны (оператор ввёл только дату) →
    fromisoformat даёт naive datetime → раньше TypeError (naive - aware)."""
    base = datetime.now(UTC)
    val = _days_old("2026-12-12T00:00:00", base)
    assert val is not None
    assert val < 0  # будущая дата → отрицательное число дней


def test_days_old_aware_expires_at_with_zone():
    """Контроль: expires_at с зоной +00:00 — как и раньше, не падает."""
    base = datetime.now(UTC)
    val = _days_old("2026-12-12T00:00:00+00:00", base)
    assert val is not None
    assert val < 0


def test_status_badge_naive_expired_expires_at():
    """Регрессия: expired-строка БЕЗ зоны (прошлая дата) → '⏳ expired', не TypeError."""
    label, color = _status_badge(_rec(expires_at="2020-01-01T00:00:00"))
    assert "expired" in label
    assert color == "grey"
