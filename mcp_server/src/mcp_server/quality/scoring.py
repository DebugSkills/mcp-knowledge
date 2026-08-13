"""Staleness scoring — чистая функция оценки качества записи (4.5).

Формула (план §6.1, v1.2 → 2d-lite: дубли выведены из формулы):
  staleness_score = clip01(
      0.47 * age_norm          # свежесть (evergreen-сниженный вклад)
    + 0.16 * incomplete_factor # неполнота recommended-полей
    + 0.10 * edit_war_factor   # edit-war flag
    + 0.05 * link_health_factor # broken source-URL

Дублирование НЕ входит в staleness_score: дубли учитываются в issues-канале
(duplicate issues, cosine ≥ 0.92 в scanner.py). «Устарело» = возраст +
неполнота + edit_war + links, а не «есть дубль».

Результат округляется до 4 знаков для стабильной сортировки review_queue.

Зависимости: ТОЛЬКО stdlib (datetime, math). Ноль внешних библиотек.
"""

from __future__ import annotations

from datetime import datetime, timezone

# ── Конфигурируемые константы ────────────────────────────────

# Веса формулы (без дублей сумма = 0.78)
W_AGE: float = 0.47
# Дубли учитываются в issues-канале (duplicate issues, cosine≥0.92 в scanner.py).
# dup_count сохранён в StalenessInput для совместимости сигнатуры, но НЕ влияет на score.
W_DUP: float = 0.0
W_INCOMPLETE: float = 0.16
W_EDIT_WAR: float = 0.10
W_LINK_HEALTH: float = 0.05

# Константы старения (дни)
NORMAL_MAX_AGE_DAYS: int = 365      # обычная запись: 1 год → age_norm=1.0
EVERGREEN_MAX_AGE_DAYS: int = 1825  # evergreen: 5 лет → age_norm=1.0

# Порог для review-очереди
REVIEW_THRESHOLD: float = 0.45

# ── Входная модель (легковесная, без Pydantic для чистоты) ──

class StalenessInput:
    """Входные данные для staleness_score() — только то что нужно формуле."""

    __slots__ = (
        "broken_links",
        "dup_count",
        "edit_war",
        "evergreen",
        "recommended_missing",
        "recommended_total",
        "total_links",
        "updated_at",
    )

    def __init__(
        self,
        updated_at: datetime,
        *,
        evergreen: bool = False,
        dup_count: int = 0,
        recommended_missing: int = 0,
        recommended_total: int = 3,  # source, cross_subjects, evergreen
        edit_war: bool = False,
        broken_links: int = 0,
        total_links: int = 0,
    ) -> None:
        self.updated_at = updated_at
        self.evergreen = evergreen
        self.dup_count = dup_count
        self.recommended_missing = recommended_missing
        self.recommended_total = recommended_total
        self.edit_war = edit_war
        self.broken_links = broken_links
        self.total_links = total_links


# ── Помощники ────────────────────────────────────────────────

def _clip01(x: float) -> float:
    """Обрезает значение в [0, 1]."""
    return max(0.0, min(1.0, x))


def _age_norm(updated_at: datetime, evergreen: bool, now: datetime | None = None) -> float:
    """Нормализованный возраст записи: 0 (только что) → 1 (предельный возраст)."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Приводим к UTC для корректного вычисления
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_days = (now - updated_at).days
    age_days = max(age_days, 0)  # будущая дата → считаем свежей

    max_days = EVERGREEN_MAX_AGE_DAYS if evergreen else NORMAL_MAX_AGE_DAYS
    return _clip01(age_days / max_days)


def _dup_factor(dup_count: int) -> float:
    """Фактор дублирования: 0 → 0.5 (1 дубль) → 1.0 (≥2 дублей).

    2d-lite: НЕ используется в staleness_score() — дубли учитываются в
    issues-канале. Сохранён для обратной совместимости сигнатуры.
    """
    if dup_count <= 0:
        return 0.0
    if dup_count == 1:
        return 0.5
    return 1.0


def _incomplete_factor(missing: int, total: int) -> float:
    """Доля отсутствующих recommended-полей."""
    if total <= 0:
        return 0.0
    return _clip01(missing / total)


def _link_health_factor(broken: int, total: int) -> float:
    """Доля битых source-URL."""
    if total <= 0:
        return 0.0
    return _clip01(broken / total)


# ── Главная функция ──────────────────────────────────────────

def staleness_score(
    inp: StalenessInput,
    *,
    now: datetime | None = None,
) -> float:
    """Вычисляет staleness score записи знаний.

    Чистая функция — нет I/O, нет состояния, детерминированный результат.
    Тестируется без моков.

    Args:
        inp: StalenessInput с данными записи.
        now: «текущее время» для тестирования (по умолчанию datetime.now(UTC)).

    Returns:
        float [0, 0.78], округлённый до 4 знаков.
        0 = идеально свежая; чем выше — тем критичнее (максимум 0.78).
    """
    age_component = W_AGE * _age_norm(inp.updated_at, inp.evergreen, now)
    incomplete_component = W_INCOMPLETE * _incomplete_factor(
        inp.recommended_missing, inp.recommended_total
    )
    edit_war_component = W_EDIT_WAR * (1.0 if inp.edit_war else 0.0)
    link_component = W_LINK_HEALTH * _link_health_factor(
        inp.broken_links, inp.total_links
    )

    raw = (
        age_component
        + incomplete_component
        + edit_war_component
        + link_component
    )

    return round(_clip01(raw), 4)


def should_review(score: float, threshold: float = REVIEW_THRESHOLD) -> bool:
    """Нужна ли запись в review-очередь?"""
    return score >= threshold
