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
#:
#: Регime-dependent (α, 2026-10-09, утверждает оператор P5): исходный DRAFT
#: 0.6 пререгистрирован под КОРОТКИЕ входы needle-set v2; длинно-входовой
#: режим v3 (секции > бюджета сжатия 4000) даёт full≈0.556, compressed=0.0
#: (2f 2026-10-09, негатив-3: plans/_provenance/arch-2026-10-08-f7-calibri-
#: tion/cc1-result.md) ⇒ порог 0.5: «хорошая» рука (full 0.556) проходит,
#: ломающая контекст (compressed 0.0) — отсекается с запасом 10 квантов
#: (1/18). Смена порога обоснована сменой входового режима v2→v3, а не
#: подгонкой под результат (анти-подгонка критерия).
NEEDLE_RATE_FLOOR: float = 0.5


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

    α (2026-10-09, решение оператора P5 по итогам 2f негатив-3): флаг
    ``ceiling`` замера НЕ блокирует предложение скаляров Сам По Себе —
    структурный скор насыщен по построению (свойство меры/конвейера, не
    контента), и содержательные гейты рамки — это needle_rate < порога
    (retention M4), пол качества и капы ₽/wall. При насыщенном структурном
    скоре различимость даёт needle (α); гейт approve/profile_approve и
    promotion/variants.evaluate_promotion решают по needle отдельно.
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
