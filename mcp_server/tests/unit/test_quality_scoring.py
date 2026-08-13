"""Unit-тесты для quality/scoring.py — staleness_score() + should_review()."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp_server.quality.scoring import (
    StalenessInput,
    should_review,
    staleness_score,
)


def _now() -> datetime:
    return datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _input(**overrides) -> StalenessInput:
    """Создаёт StalenessInput с разумными defaults."""
    defaults = {
        "updated_at": _now(),
        "evergreen": False,
        "dup_count": 0,
        "recommended_missing": 0,
        "recommended_total": 3,
        "edit_war": False,
        "broken_links": 0,
        "total_links": 0,
    }
    defaults.update(overrides)
    return StalenessInput(**defaults)


class TestFreshEntry:
    """Только что обновлённая запись → низкий score."""

    def test_just_updated_zero_score(self):
        """updated_at = now → age=0 → score ~0."""
        score = staleness_score(_input(updated_at=_now()), now=_now())
        assert score == 0.0

    def test_one_day_old(self):
        """Возраст 1 день → ~0.0013."""
        score = staleness_score(
            _input(updated_at=_now() - timedelta(days=1)), now=_now()
        )
        assert 0.001 < score < 0.003  # 0.47 * (1/365) ≈ 0.00129


class TestAgedEntry:
    """Старая запись → высокий score."""

    def test_one_year_old(self):
        """365 дней → age_norm=1.0 → score = 0.47."""
        score = staleness_score(
            _input(updated_at=_now() - timedelta(days=365)), now=_now()
        )
        assert score == 0.47

    def test_two_years_old_capped(self):
        """730 дней → возраст клипится → score = 0.47."""
        score = staleness_score(
            _input(updated_at=_now() - timedelta(days=730)), now=_now()
        )
        assert score == 0.47  # capped at 1.0

    def test_future_date_treated_as_fresh(self):
        """Будущая дата → age=0 → score ~0."""
        score = staleness_score(
            _input(updated_at=_now() + timedelta(days=10)), now=_now()
        )
        assert score == 0.0


class TestEvergreen:
    """Evergreen-записи стареют в 5× медленнее."""

    def test_evergreen_one_year_low_score(self):
        """evergreen, 365 дней → age_norm=365/1825=0.2 → 0.47*0.2=0.094."""
        score = staleness_score(
            _input(updated_at=_now() - timedelta(days=365), evergreen=True),
            now=_now(),
        )
        assert score == 0.094

    def test_evergreen_five_years_capped(self):
        """evergreen, 5 лет → age_norm=1.0 → score = 0.47."""
        score = staleness_score(
            _input(updated_at=_now() - timedelta(days=1825), evergreen=True),
            now=_now(),
        )
        assert score == 0.47

    def test_evergreen_dup_does_not_change_score(self):
        """Evergreen: дубль НЕ меняет score (дубли → issues-канал)."""
        base = staleness_score(
            _input(updated_at=_now() - timedelta(days=100), evergreen=True),
            now=_now(),
        )
        with_dup = staleness_score(
            _input(
                updated_at=_now() - timedelta(days=100),
                evergreen=True,
                dup_count=1,
            ),
            now=_now(),
        )
        assert with_dup == base


class TestDuplication:
    """Фактор дублирования (2d-lite: дубли НЕ влияют на score)."""

    def test_one_dup_no_effect(self):
        """1 дубль → score == 0.0 (дубли → issues-канал)."""
        score = staleness_score(
            _input(updated_at=_now(), dup_count=1), now=_now()
        )
        assert score == 0.0

    def test_two_dups_no_effect(self):
        """≥2 дублей → score == 0.0."""
        score = staleness_score(
            _input(updated_at=_now(), dup_count=2), now=_now()
        )
        assert score == 0.0

    def test_three_dups_no_effect(self):
        """3 дубля → score == 0.0 (как и 2 дубля)."""
        s2 = staleness_score(_input(updated_at=_now(), dup_count=2), now=_now())
        s3 = staleness_score(_input(updated_at=_now(), dup_count=3), now=_now())
        assert s2 == s3 == 0.0


class TestIncompleteFields:
    """Неполнота recommended-полей."""

    def test_all_present_zero(self):
        """Все recommended на месте → 0."""
        score = staleness_score(
            _input(updated_at=_now(), recommended_missing=0, recommended_total=3),
            now=_now(),
        )
        assert score == 0.0

    def test_one_missing(self):
        """1 из 3 отсутствует → 0.16 * (1/3) ≈ 0.0533."""
        score = staleness_score(
            _input(updated_at=_now(), recommended_missing=1, recommended_total=3),
            now=_now(),
        )
        assert score == pytest.approx(0.0533, abs=0.001)

    def test_all_missing(self):
        """3 из 3 отсутствуют → 0.16."""
        score = staleness_score(
            _input(updated_at=_now(), recommended_missing=3, recommended_total=3),
            now=_now(),
        )
        assert score == 0.16


class TestEditWar:
    """Edit-war флаг."""

    def test_edit_war_adds_tenth(self):
        """edit_war=True → +0.10."""
        score = staleness_score(
            _input(updated_at=_now(), edit_war=True), now=_now()
        )
        assert score == 0.10

    def test_no_edit_war_zero(self):
        """Без edit_war → 0."""
        score = staleness_score(
            _input(updated_at=_now(), edit_war=False), now=_now()
        )
        assert score == 0.0  # свежая запись без проблем


class TestLinkHealth:
    """Битые source-URL."""

    def test_no_links_zero(self):
        """Нет source-URL → link_health_factor=0."""
        score = staleness_score(
            _input(updated_at=_now(), broken_links=0, total_links=0), now=_now()
        )
        assert score == 0.0

    def test_half_broken(self):
        """1 из 2 битых → 0.05 * 0.5 = 0.025."""
        score = staleness_score(
            _input(updated_at=_now(), broken_links=1, total_links=2), now=_now()
        )
        assert score == 0.025

    def test_all_broken(self):
        """Все ссылки битые → 0.05."""
        score = staleness_score(
            _input(updated_at=_now(), broken_links=3, total_links=3), now=_now()
        )
        assert score == 0.05


class TestWorstCase:
    """Максимальный score — всё плохо."""

    def test_maximum_score(self):
        """Всё плохо (кроме дублей) → score = 0.78."""
        score = staleness_score(
            _input(
                updated_at=_now() - timedelta(days=400),  # age capped
                dup_count=3,
                recommended_missing=3,
                edit_war=True,
                broken_links=3,
                total_links=3,
            ),
            now=_now(),
        )
        # 0.47 + 0.16 + 0.10 + 0.05 = 0.78
        assert score == 0.78


class TestRounding:
    """Результат округляется до 4 знаков."""

    def test_score_is_rounded_to_4dp(self):
        """Любой результат имеет ≤4 десятичных знаков."""
        score = staleness_score(
            _input(
                updated_at=_now() - timedelta(days=100),
                recommended_missing=1,
                recommended_total=3,
            ),
            now=_now(),
        )
        # Проверяем что строка имеет ≤6 символов после точки (0.xxxx)
        as_str = f"{score:.10f}"
        decimal_part = as_str.split(".")[1] if "." in as_str else ""
        # После 4-го знака должны быть нули
        assert len(decimal_part) <= 4 or decimal_part[4:] == "0" * (len(decimal_part) - 4)


class TestReviewThreshold:
    """Проверка should_review()."""

    def test_below_threshold_no_review(self):
        """Score 0.44 < 0.45 → нет ревью."""
        assert should_review(0.44) is False
        assert should_review(0.0) is False

    def test_at_threshold_review(self):
        """Score == 0.45 → ревью."""
        assert should_review(0.45) is True

    def test_above_threshold_review(self):
        """Score 0.9 → ревью."""
        assert should_review(0.9) is True

    def test_custom_threshold(self):
        """Кастомный порог."""
        assert should_review(0.5, threshold=0.6) is False
        assert should_review(0.7, threshold=0.6) is True


class TestDupCountIntegration:
    """2d-lite: dup_count НЕ влияет на staleness_score (дубли → issues-канал)."""

    def test_zero_dups_no_contribution(self):
        """dup_count=0 → score = 0.0."""
        score = staleness_score(_input(dup_count=0), now=_now())
        # Свежая запись без дублей → 0.0
        assert score == 0.0

    def test_one_dup_no_contribution(self):
        """1 дубль → score = 0.0 (dup не влияет)."""
        score = staleness_score(_input(dup_count=1), now=_now())
        assert score == 0.0

    def test_two_dups_no_contribution(self):
        """2 дубля → score = 0.0 (dup не влияет)."""
        score = staleness_score(_input(dup_count=2), now=_now())
        assert score == 0.0

    def test_many_dups_no_contribution(self):
        """10 дублей → score = 0.0 (dup не влияет)."""
        score = staleness_score(_input(dup_count=10), now=_now())
        assert score == 0.0

    def test_dup_with_age_combined(self):
        """Дубли НЕ добавляются к возрасту: 365 дней → 0.47."""
        score = staleness_score(
            _input(
                dup_count=2,
                updated_at=_now() - timedelta(days=365),
            ),
            now=_now(),
        )
        assert score == pytest.approx(0.47, rel=0.01)

    def test_dup_with_incomplete_and_editwar(self):
        """Неполнота + edit_war (без dup-вклада): 0.1067 + 0.10 = 0.2067."""
        score = staleness_score(
            _input(
                dup_count=2,           # НЕ влияет (issues-канал)
                recommended_missing=2,  # 0.16 * 2/3 = 0.1067
                edit_war=True,         # 0.10
            ),
            now=_now(),
        )
        # 0.1067 + 0.10 = 0.2067
        assert score == pytest.approx(0.2067, rel=0.01)


# pytest import for approx
import pytest
