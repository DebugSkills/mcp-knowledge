"""Э3-2 Ф7 (arch-2026-10-08-f7-calibration): маппинг ProbeReport -> скаляры профиля.

D6 (дизайн §5.3, зафиксирован): **качество-first при ограничениях** — профиль
предлагается, если ``golden_median_score >= quality_floor`` И ``rub <= rub_cap``
И ``wall_s <= wall_cap_s``. НЕ «взвешенная сумма качество/₽»: качество первое,
деньги/время — рамка (значение пола и капов задаёт оператор, P5).

``propose_scalars`` только ПРЕДЛАГАЕТ (возвращает ``applied``/``reason``) —
применяет/утверждает оператор (P5): профиль пишется со ``status: draft``
(дизайн §6.3), утверждение ``calibrated`` — отдельный шаг оператора.

Э3 не включает grid-поиск (В2б; спека §7, gap «целевая функция» закрыт D6,
сами плечи — позже): probe измеряет ТЕКУЩУЮ конфигурацию режима, поэтому
предлагается дефолтный набор калибруемых скаляров (дизайн §4.5, колонка
default движка): ``retries=0``, ``max_iterations=1``, ``shaping=full-context``,
``context_mode=full``. Профиль opt-in (режим П): узлы со своими значениями/
пинами не затрагиваются — предложение ничего не меняет без утверждения.
"""
from __future__ import annotations

from typing import Any

__all__ = ["DEFAULT_PROPOSED_SCALARS", "propose_scalars"]

#: Дефолтные калибруемые скаляры (дизайн §4.5, default-колонка движка) —
#: то, что probe фактически и измерил (сетка/плечи В2б — вне Э3).
DEFAULT_PROPOSED_SCALARS: dict[str, Any] = {
    "retries": 0,
    "max_iterations": 1,
    "shaping": "full-context",
    "context_mode": "full",
}

#: 2e (В2-B «Достоверность», F-3i): порог needle_rate — рамка D6 получает
#: четвёртое условие «retention длинного контекста» (M4). needle_rate —
#: доля needle-фактов, найденных grep'ом в выводе измерителя (probe.py);
#: ниже порога конфигурация теряет контекст → скаляры НЕ предлагаются
#: (violations) и вариант НЕ promoted (variants.evaluate_promotion).
#: ``needle_rate is None`` (needle-набор не прогонялся) → гейт НЕ срабатывает
#: — обратная совместимость прогоны без --needle не ломает.
#: Значение DRAFT (0.6): оператор утверждает/правит по итогам negative-control
#: CC1 (2f, tests/golden/needle-negative-control-CC1.md) — до него порог
#: только черновой ориентир «не терять больше 40% needle-фактов».
#:
#: v2-предложение (после негатива CC1 2026-10-09: full 0.444 < 0.6,
#: compressed 0.333, разница = 1 квант 1/9 — доработан САМ набор,
#: needle-set.yaml v2: 6 заданий × 3 прогона = 18 проверок, квант 1/18,
#: явная инструкция дословного переноса факта в задании): порог
#: оставлен 0.6 (= 11/18 найденных). Обоснование: (а) значение
#: пререгистрировано в сценарии CC1 ДО прогона — менять его под
#: результат нельзя (анти-подгонка критерия); (б) с явным переносом
#: факта ожидаемый full ≥ 0.78 (≥14/18) — запас ≥3 needle-проверки над
#: полом, «хорошая» рука проходит уверенно; (в) 0.6 остаётся выше
#: ожидаемого compressed (<0.6) — отсекающая роль гейта сохраняется.
#: DRAFT — утверждает оператор по итогам повтора 2f (вариант: поднять
#: до 0.67 = 12/18 при подтверждённом full ≥ 0.8).
NEEDLE_RATE_FLOOR: float = 0.6


def propose_scalars(
    report: Any,
    *,
    quality_floor: float,
    rub_cap: float | None = None,
    wall_cap_s: float | None = None,
) -> dict:
    """Применить рамку D6 к ProbeReport → предложение скаляров профиля.

    Возврат: ``{"scalars": {...}, "constraints": {...}, "applied": bool,
    "reason": str}``.

    - все три условия рамки выполнены (``score >= floor``; капы, когда заданы,
      не превышены) → ``applied=True`` + дефолтные калибруемые скаляры;
    - иначе ``applied=False``, ``scalars={}``, ``reason`` перечисляет НАРУШЕННЫЕ
      ограничения (пол / rub_cap / wall_cap_s);
    - ``constraints`` эхом возвращает рамку (только заданные капы) — основа
      блока ``constraints`` профиля (§5.1).
    """
    violations: list[str] = []
    if report.golden_median_score < quality_floor:
        violations.append(
            f"качество ниже пола: golden_median_score={report.golden_median_score:.4f}"
            f" < quality_floor={quality_floor:.4f}"
        )
    if rub_cap is not None and report.rub > rub_cap:
        violations.append(f"превышен rub_cap: rub={report.rub:.4f} > rub_cap={rub_cap}")
    if wall_cap_s is not None and report.wall_s > wall_cap_s:
        violations.append(
            f"превышен wall_cap_s: wall_s={report.wall_s:.4f} > wall_cap_s={wall_cap_s}"
        )
    # 2e (В2-B, F-3i): needle_rate < порога → нарушение рамки; None
    # (набор не прогонялся) → гейт не срабатывает — обратная совместимость
    needle_rate = getattr(report, "needle_rate", None)
    if needle_rate is not None and needle_rate < NEEDLE_RATE_FLOOR:
        violations.append(
            f"needle_rate={needle_rate:.4f} < NEEDLE_RATE_FLOOR="
            f"{NEEDLE_RATE_FLOOR:.4f} (retention длинного контекста, M4)"
        )

    constraints: dict[str, Any] = {"quality_floor": quality_floor}
    if rub_cap is not None:
        constraints["rub_cap"] = rub_cap
    if wall_cap_s is not None:
        constraints["wall_cap_s"] = wall_cap_s

    if violations:
        return {
            "scalars": {},
            "constraints": constraints,
            "applied": False,
            "reason": "нарушена рамка D6: " + "; ".join(violations),
        }
    return {
        "scalars": dict(DEFAULT_PROPOSED_SCALARS),
        "constraints": constraints,
        "applied": True,
        "reason": (
            "рамка D6 выполнена: golden_median_score="
            f"{report.golden_median_score:.4f} >= quality_floor={quality_floor:.4f}"
            + (
                f", rub={report.rub:.4f} <= rub_cap={rub_cap}"
                if rub_cap is not None else ""
            )
            + (
                f", wall_s={report.wall_s:.4f} <= wall_cap_s={wall_cap_s}"
                if wall_cap_s is not None else ""
            )
            + (
                f", needle_rate={needle_rate:.4f} >= NEEDLE_RATE_FLOOR="
                f"{NEEDLE_RATE_FLOOR:.4f}"
                if needle_rate is not None else ""
            )
        ),
    }
