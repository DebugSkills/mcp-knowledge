"""Э2-1 Ф7 (arch-2026-10-08-f7-calibration): реестр вариантов mode-variants (§7.4).

``calibration/variants.yaml`` — SSOT promotion-решений вариантов режимов
(``modes/<mode>.<variant>.yaml``, конвенция §7.2). Валидатор fail-closed:
обязательные поля записи, статус-enum, существование файлов варианта и
базового режима в ``modes_dir`` (CV6: id базы с дефисами резолвится
dash→dot через ``resolve_mode_file`` — тот же fallback, что L16
mode_lint; P1-fix критики 3c-promotion), обязательность ``probe_pair{base,variant}``
для ``promoted``/``rejected`` (решение оператора P5 опирается на пару
ProbeReport §7.3). В3-A 3a (F-5): при переданном ``reports_dir`` CV7
ДОПОЛНИТЕЛЬНО резолвит файл отчёта ``probe-<run_id>.json`` по каждому ключу
пары (``probe.report_filename`` — тот же канон, что ``write_report``) и
требует его существования — promotion не может опираться на несуществующий
отчёт (обещание дизайна §7.4). Без ``reports_dir`` — паритет: проверка
только наличия ключей.

Э4-1: ``evaluate_promotion`` — критерий §7.3 (бумажка решения, НЕ решение:
promotion всегда за оператором P5); ``record_decision`` — upsert записи в
``variants.yaml`` с fail-closed ревалидацией реестра до записи.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from ai_workspace.calibration.policy import NEEDLE_RATE_FLOOR
from ai_workspace.orchestrator.mode_lint import resolve_mode_file
from ai_workspace.calibration.probe import report_filename
from ai_workspace.calibration.profiles import SEVERITY_ERROR, Finding

__all__ = [
    "VARIANT_SCHEMA",
    "VARIANT_STATUSES",
    "evaluate_promotion",
    "record_decision",
    "validate_variants",
]

#: Схема реестра вариантов (дизайн §7.4)
VARIANT_SCHEMA = "calibration-variants/1"

#: baseline — запись при старте эксперимента; promoted/rejected — только оператор (P5)
VARIANT_STATUSES: frozenset[str] = frozenset({"baseline", "promoted", "rejected"})

#: Статусы с принятым решением — требуют probe_pair
DECIDED_STATUSES: frozenset[str] = frozenset({"promoted", "rejected"})

REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "variant",
    "variant_of",
    "status",
    "decided_by",
    "decided_at",
)
REQUIRED_PROBE_PAIR: tuple[str, ...] = ("base", "variant")


def _err(code: str, message: str, path: str) -> Finding:
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


def _check_mode_file(
    findings: list[Finding],
    modes: Path,
    value: object,
    field: str,
    prefix: str,
    code: str,
    *,
    dash_fallback: bool = False,
) -> None:
    """Файл режима существует (value — непустая строка).

    ``dash_fallback`` (CV6, P1-fix критики 3c-promotion): value — id режима
    (дефисы), файл — точки; резолв через ``resolve_mode_file`` — тот же
    dash→dot fallback, что L16 (mode_lint.py). Прямой путь проверяется
    первым — прежние записи (stem-имена) не затронуты.
    """
    if not isinstance(value, str) or not value:
        findings.append(_err(code, f"{field} должен быть непустой строкой", f"{prefix}.{field}"))
        return
    mode_path = resolve_mode_file(modes, value) if dash_fallback else modes / f"{value}.yaml"
    if not mode_path.is_file():
        findings.append(_err(code, f"файл режима не найден: {mode_path}", f"{prefix}.{field}"))


def validate_variants(
    doc: dict, modes_dir: Path | str, reports_dir: Path | str | None = None,
) -> list[Finding]:
    """Проверить реестр вариантов по §7.4; ``modes_dir`` — каталог mode-YAML.

    ``reports_dir`` (В3-A 3a, F-5) — включает CV7-existing: значения
    ``probe_pair{base,variant}`` обязаны быть run_id СУЩЕСТВУЮЩИХ отчётов
    probe (``probe-<run_id>.json`` в ``reports_dir``). ``None`` — паритет:
    только наличие ключей пары (прошлое поведение).
    """
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("CV0", "реестр вариантов должен быть YAML-отображением (mapping)", "$")]

    # CV1: литерал схемы.
    if doc.get("schema") != VARIANT_SCHEMA:
        findings.append(
            _err(
                "CV1",
                f"schema должен быть {VARIANT_SCHEMA!r}, получено: {doc.get('schema')!r}",
                "schema",
            )
        )

    entries = doc.get("entries")
    if "entries" not in doc:
        findings.append(_err("CV2", "обязательное поле отсутствует: entries", "entries"))
        entries = None
    elif not isinstance(entries, list):
        findings.append(_err("CV3", "entries должен быть списком", "entries"))
        entries = None

    modes = Path(modes_dir)
    for i, entry in enumerate(entries or []):
        prefix = f"entries[{i}]"
        if not isinstance(entry, Mapping):
            findings.append(
                _err(
                    "CV3",
                    f"запись должна быть отображением: {type(entry).__name__}",
                    prefix,
                )
            )
            continue

        # CV2: обязательные поля записи.
        for field in REQUIRED_ENTRY_FIELDS:
            if field not in entry:
                findings.append(
                    _err("CV2", f"обязательное поле отсутствует: {field}", f"{prefix}.{field}")
                )

        # CV4: status — enum.
        status = entry.get("status")
        if "status" in entry and status not in VARIANT_STATUSES:
            findings.append(
                _err(
                    "CV4",
                    f"неизвестный status: {status!r}; ожидается один из "
                    f"{sorted(VARIANT_STATUSES)}",
                    f"{prefix}.status",
                )
            )

        # CV5: файл варианта существует (modes/<variant>.yaml).
        if "variant" in entry:
            _check_mode_file(findings, modes, entry.get("variant"), "variant", prefix, "CV5")

        # CV6: variant_of указывает на существующий базовый режим; id —
        # дефисы → dash→dot fallback (паритет L16, P1-fix критики 3c).
        if "variant_of" in entry:
            _check_mode_file(
                findings, modes, entry.get("variant_of"), "variant_of", prefix, "CV6",
                dash_fallback=True,
            )

        # CV7: probe_pair{base,variant} обязателен для promoted/rejected;
        # В3-A 3a (F-5): при заданном reports_dir ключи — run_id отчётов
        # probe, файл резолвится и обязан существовать (fail-closed).
        if status in DECIDED_STATUSES:
            pair = entry.get("probe_pair")
            if not isinstance(pair, Mapping) or any(k not in pair for k in REQUIRED_PROBE_PAIR):
                findings.append(
                    _err(
                        "CV7",
                        "для status="
                        f"{status!r} обязателен probe_pair{{{', '.join(REQUIRED_PROBE_PAIR)}}}",
                        f"{prefix}.probe_pair",
                    )
                )
            elif reports_dir is not None:
                for key in REQUIRED_PROBE_PAIR:
                    field = f"{prefix}.probe_pair.{key}"
                    run_id = pair.get(key)
                    if not isinstance(run_id, str) or not run_id.strip():
                        findings.append(
                            _err(
                                "CV7",
                                f"probe_pair.{key} должен быть непустым run_id "
                                "отчёта probe",
                                field,
                            )
                        )
                        continue
                    report_path = Path(reports_dir) / report_filename(run_id)
                    if not report_path.is_file():
                        findings.append(
                            _err(
                                "CV7",
                                f"отчёт probe не существует: {report_path} "
                                f"(run_id {run_id!r}) — promotion не может "
                                "опираться на несуществующий отчёт (§7.4)",
                                field,
                            )
                        )

    return findings


def evaluate_promotion(
    base_report: Any,
    variant_report: Any,
    *,
    quality_floor: float,
    rub_cap: float | None = None,
    wall_cap_s: float | None = None,
    needle_quantum: float | None = None,
) -> dict:
    """Критерий promotion варианта (§7.3) по паре ProbeReport.

    Возвращает ``{"passed": bool, "reasons": [...]}`` — какие условия НЕ
    выполнены (``passed`` ⇔ ``reasons`` пуст). Это вход для решения
    оператора (P5), а не само решение. Условия:

      1. база недостаточна: БЕЗ ceiling — ``base.golden_median_score <
         quality_floor`` (вариант имеет смысл только у проваливающейся
         базы); ПРИ ceiling — ``needle_rate(base) < NEEDLE_RATE_FLOOR``
         (структурный скор насыщен ⇒ adequacy базы меряем по needle-M4);
         ``needle_rate(base) is None`` при ceiling → не promoted (нечем
         решать);
      2. вариант проходит пол: ``variant.golden_median_score >= quality_floor``;
      3. рамка D6: ``rub <= rub_cap`` И ``wall_s <= wall_cap_s`` (кап не
         задан — не проверяется);
      4. held-out подтверждает golden: ``|golden - heldout| <= dispersion``
         (M5; нулевая дисперсия — с допуском 1e-9 против float-шума).

    α (2026-10-09, решение оператора P5 по итогам 2f негатив-3): флаг
    ``ceiling`` у ЛЮБОГО из отчётов пары больше НЕ авто-reject — структурный
    скор насыщен по построению (свойство меры, не контента) и различимость
    даёт needle-M4 (2f: full 0.556 vs compressed 0.000). При ceiling
    решение ПО NEEDLE: promoted требует ``needle_rate(variant) >=
    NEEDLE_RATE_FLOOR`` И (если needle_rate базы доступен) маржу
    ``needle_rate(variant) > needle_rate(base)`` — на ≥ 1 квант шума
    ``1/(tasks×runs)`` (``needle_quantum``; не задан — строгое ``>``).
    ceiling + ``needle_rate(variant) is None`` → не promoted: структурно
    нечем решать. α-достройка (2026-10-09, живой 3c variant_pair: base
    golden=1.0 >= floor блокировал promotion при base needle=0.00): при
    ceiling И adequacy базы решается по needle — «база проваливает пол»
    по golden не проверяется (насыщен всегда); сводно promotion ⇔
    ``needle_rate(base) < NEEDLE_RATE_FLOOR`` ∧ ``needle_rate(variant) >=
    NEEDLE_RATE_FLOOR`` ∧ маржа ≥ квант. Без ceiling — прежняя логика
    (гейт только по needle_rate варианта, 2e; needle базы не участвует).
    """
    reasons: list[str] = []
    # ── needle-гейты: 2e (без ceiling) и α (при ceiling) ──
    needle_rate = getattr(variant_report, "needle_rate", None)
    base_needle_rate = getattr(base_report, "needle_rate", None)
    # α: ceiling у ЛЮБОГО плеча пары — структурный скор насыщен по
    # построению (2f негатив-3: все 24 сегмента hard-наборов = 1.0) ⇒
    # структурная мера не различает конфигурации ⇒ и adequacy базы, и
    # превосходство варианта решаются по needle-M4
    ceiling = (
        "ceiling" in (getattr(base_report, "flags", None) or ())
        or "ceiling" in (getattr(variant_report, "flags", None) or ())
    )
    # ── условие 1: база недостаточна (вариант имеет смысл только у
    # недостаточной базы) ──
    if ceiling:
        # α-достройка (2026-10-09, живой 3c): структурный скор насыщен ⇒
        # «база проваливает пол» по golden бессмысленна (golden=1.0 >=
        # floor всегда ⇒ promotion недостижим). База недостаточна ⇔
        # needle_rate(base) < NEEDLE_RATE_FLOOR; None → нечем решать.
        if base_needle_rate is None:
            reasons.append(
                "структурный скор насыщен (ceiling) → база недостаточна "
                "решается по needle: needle_rate базы неизвестен "
                "(needle-набор не прогонялся) — нечем решать, promoted "
                "невозможен"
            )
        elif base_needle_rate >= NEEDLE_RATE_FLOOR:
            reasons.append(
                "структурный скор насыщен (ceiling) → база достаточна по "
                f"needle: base needle_rate={base_needle_rate:.4f} >= "
                f"NEEDLE_RATE_FLOOR={NEEDLE_RATE_FLOOR:.4f} (база "
                "недостаточна ⇔ needle_rate < пола) — вариант не закрывает "
                "пробел базы"
            )
    elif base_report.golden_median_score >= quality_floor:
        # без ceiling — прежняя логика (паритет F1): пол по golden
        reasons.append(
            f"база не проваливает quality_floor: base golden_median_score="
            f"{base_report.golden_median_score:.4f} >= floor={quality_floor:.4f}"
        )
    if variant_report.golden_median_score < quality_floor:
        reasons.append(
            f"вариант ниже quality_floor: variant golden_median_score="
            f"{variant_report.golden_median_score:.4f} < floor={quality_floor:.4f}"
        )
    if rub_cap is not None and variant_report.rub > rub_cap:
        reasons.append(
            f"превышен rub_cap: rub={variant_report.rub:.4f} > rub_cap={rub_cap:.4f}"
        )
    if wall_cap_s is not None and variant_report.wall_s > wall_cap_s:
        reasons.append(
            f"превышен wall_cap_s: wall_s={variant_report.wall_s:.4f} "
            f"> wall_cap_s={wall_cap_s:.4f}"
        )
    delta = abs(variant_report.golden_median_score - variant_report.heldout_score)
    if delta > max(variant_report.golden_dispersion, 1e-9):
        reasons.append(
            f"held-out расходится с golden: |golden-heldout|={delta:.4f} > "
            f"golden_dispersion={variant_report.golden_dispersion:.4f}"
        )
    if ceiling:
        if needle_rate is None:
            reasons.append(
                "структурный скор насыщен (ceiling) → решение по needle: "
                "needle_rate варианта неизвестен (needle-набор не "
                "прогонялся) — нечем решать, promoted невозможен"
            )
        else:
            if needle_rate < NEEDLE_RATE_FLOOR:
                reasons.append(
                    "структурный скор насыщен (ceiling) → решение по needle: "
                    f"needle_rate варианта {needle_rate:.4f} < "
                    f"NEEDLE_RATE_FLOOR={NEEDLE_RATE_FLOOR:.4f} (retention "
                    "длинного контекста, M4)"
                )
            if base_needle_rate is not None:
                quantum = needle_quantum if needle_quantum is not None else 0.0
                margin = needle_rate - base_needle_rate
                if needle_rate <= base_needle_rate or margin < quantum - 1e-9:
                    reasons.append(
                        "структурный скор насыщен (ceiling) → решение по "
                        f"needle: вариант не лучше базы по retention: "
                        f"needle {needle_rate:.4f} vs база "
                        f"{base_needle_rate:.4f}, маржа {margin:.4f} < "
                        f"требуемой {quantum:.4f}"
                        + (
                            f" (1 квант 1/(tasks×runs)={needle_quantum:.4f})"
                            if needle_quantum is not None
                            else " (строгое превосходство)"
                        )
                    )
    elif needle_rate is not None and needle_rate < NEEDLE_RATE_FLOOR:
        # 2e (В2-B, F-3i), прежняя логика без ceiling: promoted требует
        # needle_rate >= порога у ВАРИАНТА; None — не проверяется
        reasons.append(
            f"needle_rate ниже порога: {needle_rate:.4f} < "
            f"{NEEDLE_RATE_FLOOR:.4f} (retention длинного контекста, M4)"
        )
    return {"passed": not reasons, "reasons": reasons}


def _mode_file_variant_of(modes_dir: Path, variant: str) -> str | None:
    """``variant_of`` из самого mode-файла варианта — канон (S11/L16).

    P1-fix (критика 3c-promotion): именно поле mode-файла — источник истины
    о базе варианта; вывод из имени файла по rsplit для statya.full.local
    давал несуществующую базу statya.full → ложный CV6-отказ записи.
    Файла нет / без поля → ``None`` (вызывающий идёт по legacy-цепочке);
    битый YAML / не-mapping → ``ValueError`` (fail-closed: variant_of
    невыводим, запись с невыводимой базой запрещена).
    """
    mode_path = resolve_mode_file(modes_dir, variant)
    if not mode_path.is_file():
        return None
    try:
        doc = yaml.safe_load(mode_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(
            f"mode-файл варианта не читается: {mode_path} ({exc}) — "
            "variant_of невыводим, запись запрещена (fail-closed)"
        ) from exc
    if not isinstance(doc, Mapping):
        raise ValueError(
            f"mode-файл варианта должен быть YAML-отображением: {mode_path} — "
            "variant_of невыводим, запись запрещена (fail-closed)"
        )
    value = doc.get("variant_of")
    if isinstance(value, str) and value:
        return value
    return None


def record_decision(
    variants_path: Path | str,
    variant: str,
    status: str,
    probe_pair: Mapping | None,
    decided_by: str,
    decided_at: str,
    reports_dir: Path | str | None = None,
) -> None:
    """Upsert записи решения в ``variants.yaml`` (§7.4), fail-closed.

    ``modes_dir`` выводится из layout SSOT: ``<ws>/calibration/variants.yaml``
    → ``<ws>/modes``. Реестр валидируется ЦЕЛИКОМ (включая чужие записи) до
    записи: любые findings → ``ValueError``, файл не трогаем. ``probe_pair``
    обязателен для ``promoted``/``rejected`` (CV7); ``variant_of`` берётся из
    самого mode-файла варианта (канон, P1-fix критики 3c-promotion), при
    отсутствии поля — из существующей записи либо из конвенции
    ``<mode>.<variant>``. CV6 резолвит id базы с dash→dot fallback
    (``resolve_mode_file``, паритет L16) и остаётся fail-closed, если база
    не найдена после обоих кандидатов имени.
    ``reports_dir`` (В3-A 3a, F-5) — включает CV7-existing: run_id пары
    обязаны резолвиться в существующие ``probe-<run_id>.json``; ``None`` —
    паритет (наличие ключей пары).
    """
    if status not in VARIANT_STATUSES:
        raise ValueError(
            f"неизвестный status: {status!r}; ожидается один из "
            f"{sorted(VARIANT_STATUSES)}"
        )
    path = Path(variants_path)
    doc: dict = {"schema": VARIANT_SCHEMA, "entries": []}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            doc = dict(loaded)
    doc.setdefault("schema", VARIANT_SCHEMA)
    entries = doc.get("entries")
    if not isinstance(entries, list):
        entries = []
        doc["entries"] = entries

    modes_dir = path.resolve().parent.parent / "modes"
    existing = next(
        (
            e
            for e in entries
            if isinstance(e, Mapping) and e.get("variant") == variant
        ),
        None,
    )
    # P1-fix (критика 3c-promotion): канонический источник variant_of — сам
    # mode-файл варианта (поле variant_of, как читают его S11/L16), НЕ вывод
    # из имени по rsplit: statya.full.local.yaml → variant_of: statya-local
    # (rsplit давал несуществующий statya.full → CV6-отказ записи). Цепочка:
    # mode-файл → прежняя запись реестра → конвенция <mode>.<variant>
    # (legacy-режимы без поля variant_of в mode-файле).
    variant_of = _mode_file_variant_of(modes_dir, variant)
    if variant_of is None:
        variant_of = (existing or {}).get("variant_of")  # type: ignore[union-attr]
    if not isinstance(variant_of, str) or not variant_of:
        variant_of = variant.rsplit(".", 1)[0] if "." in variant else variant

    entry: dict = {
        "variant": variant,
        "variant_of": variant_of,
        "status": status,
    }
    if probe_pair is not None:
        entry["probe_pair"] = dict(probe_pair)
    entry["decided_by"] = decided_by
    entry["decided_at"] = decided_at

    candidate = dict(doc)
    candidate["entries"] = [
        entry
        if isinstance(e, Mapping) and e.get("variant") == variant
        else e
        for e in [*entries, entry]
    ]
    # Выше entry добавлена в конец; для upsert убираем прежнюю копию.
    seen_variant = False
    deduped: list = []
    for e in candidate["entries"]:
        if isinstance(e, Mapping) and e.get("variant") == variant:
            if seen_variant:
                continue
            seen_variant = True
        deduped.append(e)
    candidate["entries"] = deduped

    findings = validate_variants(candidate, modes_dir, reports_dir=reports_dir)
    if findings:
        raise ValueError(
            "реестр невалиден после записи: "
            + "; ".join(f"{f.code}: {f.message}" for f in findings)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
