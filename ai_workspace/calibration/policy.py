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
        ),
    }
