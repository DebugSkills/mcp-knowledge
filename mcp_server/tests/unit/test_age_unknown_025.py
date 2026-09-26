"""025-A: fieldless `updated_at` ⇒ возраст неизвестен (наблюдаемый флаг/метрика).

Ядро 024-ловушки в остальных потребителях: `quality/scanner` строил frontmatter
без derived-маркера ⇒ `_age_norm` давал age≈0 и fieldless-запись объявлялась
«самой свежей» и выпадала из ревью. Здесь проверяются: маркер парсера сканера,
флаг `age_unknown` в payload-канале (score для fieldless не меняется — P1-2
Critic 025), нулевой дифф для явных записей и ключ метрики.

RED-мутации: M1 (убрать флаг age_unknown в `_quality_flags`) → T25-1; M2
(parse-хелпер всегда explicit) → T25-6; M3 (убрать ключ метрики) → T25-7.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.scanner import _empty_result, _parse_frontmatter, _quality_flags
from mcp_server.quality.scoring import (
    NORMAL_MAX_AGE_DAYS,
    W_AGE,
    StalenessInput,
    _age_norm,
    staleness_score,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

_WITH = (
    "---\nknowledge_id: kid-25a\ndomain: d\nsubject: s\n"
    "updated_at: '2026-01-01T00:00:00+00:00'\n---\n# body\n"
)
_WITHOUT = "---\nknowledge_id: kid-25a\ndomain: d\nsubject: s\n---\n# body\n"


def test_t25_6_parse_frontmatter_sets_explicit_marker():
    """T25-6: parse-хелпер сканера различает «поле есть» и «поля нет»."""
    assert _parse_frontmatter(_WITH, yaml).updated_at_explicit is True
    assert _parse_frontmatter(_WITHOUT, yaml).updated_at_explicit is False


def test_t25_1_quality_flags_age_unknown():
    """T25-1: fieldless → флаг `age_unknown` (единственный наблюдаемый канал)."""
    fm_fieldless = KnowledgeFrontmatter(
        knowledge_id="kid-25b", domain="d", subject="s", updated_at_explicit=False
    )
    fm_explicit = KnowledgeFrontmatter(knowledge_id="kid-25c", domain="d", subject="s")

    assert "age_unknown" in _quality_flags(fm_fieldless, 0.1)
    assert "age_unknown" not in _quality_flags(fm_explicit, 0.1)
    # порог ревью сохранён
    assert "needs_review" in _quality_flags(fm_explicit, 0.9)
    assert "needs_review" not in _quality_flags(fm_explicit, 0.1)


def test_t25_2_explicit_score_unchanged():
    """T25-2: для явных записей скоринг не изменился (нулевой дифф)."""
    old = T0 - timedelta(days=30)
    assert _age_norm(old, False, now=T0) == pytest.approx(30 / NORMAL_MAX_AGE_DAYS)

    inp = StalenessInput(
        updated_at=old, evergreen=False, recommended_missing=0, recommended_total=3
    )
    # score округляется до 4 знаков (контракт scoring.py): 0.47*30/365 = 0.03863 → 0.0386
    assert staleness_score(inp, now=T0) == pytest.approx(0.0386, abs=1e-4)
    assert staleness_score(inp, now=T0) == pytest.approx(W_AGE * (30 / NORMAL_MAX_AGE_DAYS), abs=1e-4)


def test_t25_3_fresh_explicit_zero():
    """T25-3: свежая явная запись → 0.0."""
    assert _age_norm(T0, False, now=T0) == 0.0
    inp = StalenessInput(
        updated_at=T0, evergreen=False, recommended_missing=0, recommended_total=3
    )
    assert staleness_score(inp, now=T0) == 0.0


def test_t25_1b_age_unknown_no_penalty():
    """T25-1b: `age_unknown=True` не штрафует (и не льстит — флаг обязателен)."""
    inp = StalenessInput(
        updated_at=T0,
        evergreen=False,
        recommended_missing=0,
        recommended_total=3,
        age_unknown=True,
    )
    assert _age_norm(T0, False, now=T0, age_unknown=True) == 0.0
    assert staleness_score(inp, now=T0) == 0.0


def test_t25_7_metrics_key_present():
    """T25-7: счётчик `age_unknown_count` есть в контракте метрик скана."""
    res = _empty_result()
    assert res.get("age_unknown_count") == 0
