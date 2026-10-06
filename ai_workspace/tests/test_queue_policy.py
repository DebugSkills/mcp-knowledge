"""Offline-тесты политики WFQ 3×3 + aging (Ф3.2) — БЕЗ Redis.

Фиксация контракта: weight-матрица 9 значений; WFQ-порядок по приоритетам
внутри класса; class-порядок при равном приоритете; aging-пол — абсолютное
право (просроченный background побеждает interactive); не-просроченный aging
не вмешивается в WFQ; просроченные — FIFO по дедлайну. Плюс offline-проверка
нарезки queue.lua (SSOT-файл не битый) — сам Redis не нужен.
"""

from __future__ import annotations

import math

import pytest

from ai_workspace.scheduler import policy

NOW = 1_000.0  # фиксированное «сейчас» для детерминизма


# ── weight: матрица 3×3 (инвариант I2) ───────────────────────────────────

def test_weight_table_3x3():
    expected = {
        ("high", "interactive"): 400.0,
        ("med", "interactive"): 200.0,
        ("low", "interactive"): 100.0,
        ("high", "batch"): 40.0,
        ("med", "batch"): 20.0,
        ("low", "batch"): 10.0,
        ("high", "background"): 4.0,
        ("med", "background"): 2.0,
        ("low", "background"): 1.0,
    }
    for (p, c), w in expected.items():
        assert policy.weight(p, c) == w, f"w({p},{c})"


def test_weight_unknown_fail_closed():
    with pytest.raises(KeyError):
        policy.weight("urgent", "batch")  # type: ignore[call-overload]
    with pytest.raises(KeyError):
        policy.weight("high", "inline")  # type: ignore[call-overload]


def test_t_starve_contract():
    assert policy.T_STARVE == {
        "interactive": 60.0,
        "batch": 1800.0,
        "background": 7200.0,
    }


# ── WFQ-порядок ──────────────────────────────────────────────────────────

def _cand(
    name: str,
    prio: str,
    cls: str,
    cost: float,
    dl: float | None = None,
) -> dict:
    """Кандидат с vft от чистых нулевых часов; dl=None — далеко в будущем."""
    return {
        "call": name,
        "vft": policy.virtual_finish(0.0, 0.0, policy.weight(prio, cls), cost),
        "class": cls,
        "prio": prio,
        "starve_deadline": NOW + 10_000.0 if dl is None else dl,
    }


def _drain(cands: list[dict], now: float = NOW) -> list[str]:
    order = []
    while cands:
        best = policy.pick_best(cands, now)
        order.append(best)
        cands = [c for c in cands if c["call"] != best]
    return order


def test_wfq_priority_order_within_class():
    """Один класс (batch): high → med → low (vft-шаг = cost/w)."""
    cands = [
        _cand("b-low", "low", "batch", 100.0),
        _cand("b-high", "high", "batch", 100.0),
        _cand("b-med", "med", "batch", 100.0),
    ]
    assert _drain(cands) == ["b-high", "b-med", "b-low"]


def test_wfq_class_order_equal_priority():
    """Равный приоритет (med): interactive → batch → background."""
    cands = [
        _cand("bg", "med", "background", 100.0),
        _cand("ba", "med", "batch", 100.0),
        _cand("in", "med", "interactive", 100.0),
    ]
    assert _drain(cands) == ["in", "ba", "bg"]


def test_virtual_finish_max_semantics():
    """vft = max(V, last) + cost/w — защита от «прыжка» мимо своих."""
    assert policy.virtual_finish(5.0, 2.0, 10.0, 100.0) == pytest.approx(15.0)
    assert policy.virtual_finish(2.0, 5.0, 10.0, 100.0) == pytest.approx(15.0)
    assert math.isclose(policy.virtual_finish(0.0, 0.0, 400.0, 100.0), 0.25)


# ── aging-пол (I2: абсолютное право) ─────────────────────────────────────

def test_aging_floor_beats_anyone():
    """Просроченный background побеждает interactive с меньшим vft."""
    cands = [
        _cand("in", "high", "interactive", 1.0, dl=NOW + 60.0),
        _cand("bg", "low", "background", 1e9, dl=NOW - 1.0),
    ]
    assert policy.pick_best(cands, NOW) == "bg"


def test_not_expired_aging_keeps_wfq():
    """Дедлайн background БЛИЖЕ, но не истёк — WFQ по vft: interactive."""
    cands = [
        _cand("in", "high", "interactive", 1.0, dl=NOW + 60.0),
        _cand("bg", "low", "background", 1.0, dl=NOW + 7_140.0),
    ]
    assert policy.pick_best(cands, NOW) == "in"


def test_expired_fifo_by_deadline():
    """Оба просрочены: берётся меньший дедлайн (не меньший vft)."""
    cands = [
        _cand("late-dl", "low", "background", 1.0, dl=NOW - 5.0),
        _cand("early-dl", "high", "interactive", 100.0, dl=NOW - 300.0),
    ]
    assert policy.pick_best(cands, NOW) == "early-dl"


def test_select_order_floor_scale():
    """Индикаторный score: просроченный < ЛЮБОГО vft (гарантия пола)."""
    expired = policy.select_order("x", NOW, vft=0.0, starve_deadline=NOW)
    fresh_huge = policy.select_order("y", NOW, vft=1e12, starve_deadline=NOW + 1.0)
    assert expired < fresh_huge


def test_pick_best_fifo_exact_small_dl_gap():
    """Регрессия parity (найдено integration-тестом): дедлайны 0.5 c apart.
    Одно-float score (dl - 1e18) их сглаживает (ulp ≈ 128 c) → тай по имени;
    точный pick_best различает по СЫРОМУ дедлайну — как Lua."""
    cands = [
        _cand("zzz-small-dl", "low", "background", 1.0, dl=NOW - 10.5),
        _cand("aaa-big-dl", "high", "interactive", 1.0, dl=NOW - 10.0),
    ]
    # меньший дедлайн побеждает, хотя имя лекс. больше
    assert policy.pick_best(cands, NOW) == "zzz-small-dl"


# ── queue.lua: offline-проверка SSOT-нарезки ─────────────────────────────

def test_lua_scripts_split():
    from ai_workspace.scheduler import queue as queue_mod

    for name, must_have in (
        ("enqueue", "ZADD"),
        ("dequeue", "ZRANGEBYSCORE"),
        ("complete", "GET"),
    ):
        src = queue_mod._script(name)
        assert must_have in src, f"секция {name} без {must_have}"
        assert "redis.call" in src
    # двух-индексное снятие: ZREM из q И starve
    assert queue_mod._script("dequeue").count("ZREM") >= 2
    # publish/XADD — НЕ в этой фазе (Ф3.3): в секции нет таких redis.call
    # (упоминание в комментарии — документирование границы, не вызов)
    src = queue_mod._script("enqueue")
    assert "redis.call('XADD'" not in src
    assert "redis.call('PUBLISH'" not in src
