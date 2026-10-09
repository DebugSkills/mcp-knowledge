"""В3-B (шаг 3c Ф7, arch-2026-10-08-f7-calibration): живая пара base-vs-variant.

CLI «эксперимент продвижения» (дизайн §7.3/§7.4): ДВА probe-прогона —
base (``modes/statya.yaml``) и variant (``modes/statya.deep.yaml``) — ТЕМ ЖЕ
живым путём, что ``probe_run.py``: ``calibration.probe.run_probe`` (мера
``vp_ab_pilot.run_one``) с клиентом из ``probe_run.resolve_llm_client``
(``--live`` → реальный ``OllamaClient`` после preflight-ping, иначе
контурный стаб ``StubShelfLLM``) и фактом полки
``probe_run._resolve_live_facts`` (В1-1a: digest → run_id отчёта). Живую
логику НЕ дублируем — только вызываем существующее. Затем
``calibration.variants.evaluate_promotion(base, variant, …)`` → **reasons
оператору** (печать): ``evaluate_promotion`` — бумажка решения, НЕ решение.

**Предусловие 2f (F-3ii):** шаг 3c запускается ТОЛЬКО после negative-control
CC1 (``tests/golden/needle-negative-control-CC1.md``: подтверждённая
различимость needle-метрики на заведомо плохой конфигурации). Без явного
``--cc1-confirmed`` CLI печатает ПРЕДУПРЕЖДЕНИЕ — продвижение по
неразличимой метрике лишено смысла; живой прогон CC1 — Operator Gate ВНЕ
этого кода (критерий приёмки В2).

**ДВА раздельных Operator Gate:**

- *живой прогон*: ``--live`` без ``--confirm-live`` → отказ exit 2 (как
  ``probe_run``; GPU/время); в сессии реализации живой прогон НЕ запускать;
- *запись решения*: по умолчанию решение НЕ записывается (печать reasons =
  dry-run); запись — только ``--record {promoted,rejected}`` +
  ``--confirm-record`` (P5: решение всегда за оператором). Запись требует
  ``--reports-dir``: ``record_decision`` резолвит ``probe-<run_id>.json``
  обоих плеч по канону ``probe.report_filename`` (CV7-existing В3-A,
  fail-closed «отчёт не существует») и делает upsert в
  ``calibration/variants.yaml``. Повторный запуск записи без пересчёта —
  ``--resume`` (дорогие сегменты не повторяются, 3b).

Развязка по выходам: 0 — план/пара посчитана/решение записано;
1 — probe прерван гейтом (``ProbeAborted``); 2 — гейты CLI (live/record без
подтверждения, полка недоступна, реестр невалиден CV0–CV7).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_workspace import conformance as cf
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration import variants as variants_mod
from ai_workspace.registry import Registry
from ai_workspace.tools import golden_run, probe_run

#: Пара эксперимента по умолчанию (§7.2: base + mode-variant конвенции)
DEFAULT_BASE_MODE: Path = golden_run.DEFAULT_MODE
DEFAULT_VARIANT_MODE: Path = (
    golden_run.AI_WORKSPACE_DIR / "modes" / "statya.deep.yaml"
)
#: Реестр promotion-решений (§7.4; SSOT-канон calibration/variants.yaml)
DEFAULT_VARIANTS_YAML: Path = (
    golden_run.AI_WORKSPACE_DIR / "calibration" / "variants.yaml"
)
#: Рекомендуемый каталог отчётов пары (gitignored; тот же, что probe-run)
REPORTS_DIR_DEFAULT: Path = probe_run._REPORTS_DIR_DEFAULT

_LIVE_WITHOUT_CONFIRM_EXIT = 2
_LIVE_SHELF_UNAVAILABLE_EXIT = 2
_RECORD_WITHOUT_CONFIRM_EXIT = 2
_RECORD_INVALID_EXIT = 2
_PROBE_ABORTED_EXIT = 1

#: Плечи пары: метка → режим (порядок = порядок прогонов)
SIDES: tuple[tuple[str, Path], ...] = (("base", DEFAULT_BASE_MODE),
                                        ("variant", DEFAULT_VARIANT_MODE))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="variant-pair",
        description="Живая пара base-vs-variant → reasons оператора "
                    "(живой прогон и запись решения — раздельные Operator Gate)",
    )
    parser.add_argument(
        "--base", type=Path, default=DEFAULT_BASE_MODE,
        help="базовый режим (mode YAML) плеча base",
    )
    parser.add_argument(
        "--variant", type=Path, default=DEFAULT_VARIANT_MODE,
        help="режим-вариант плеча variant (modes/<mode>.<variant>.yaml)",
    )
    parser.add_argument(
        "--golden", type=Path, default=golden_run.DEFAULT_GOLDEN,
        help="golden-set YAML (основной замер, M1/M5)",
    )
    parser.add_argument(
        "--heldout", type=Path, required=True,
        help="held-out YAML — отдельный замер (F6)",
    )
    parser.add_argument(
        "--needle", type=Path, default=None,
        help="needle-set YAML (M4 retention; гейт promotion 2e — по ВАРИАНТУ)",
    )
    parser.add_argument(
        "--class", dest="model_class", required=True,
        help="класс модели (heavy/fast; привязка применения, A4)",
    )
    parser.add_argument(
        "--runs", type=int, default=probe_mod.MIN_RUNS,
        help=f"прогонов на задание (>= {probe_mod.MIN_RUNS}; медиана N>=3)",
    )
    parser.add_argument(
        "--zone", choices=("public", "private"), default="public",
        help="зонный контур: private → только local-полка (I5/P3)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="живой local-движок (ollama qwen2.5:7b, ~0₽) вместо стаба;"
             " требует --confirm-live",
    )
    parser.add_argument(
        "--quality-floor", type=float, default=None,
        help="пол качества D6 (P5: оператор); по умолчанию Q_FLOOR зоны",
    )
    parser.add_argument(
        "--rub-cap", type=float, default=None,
        help="кап ₽ рамки D6 для evaluate_promotion (вариант дороже — reason)",
    )
    parser.add_argument(
        "--wall-cap", type=float, default=None, dest="wall_cap_s",
        help="кап wall_s (сек) рамки D6 для evaluate_promotion",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="Operator Gate: запустить прогоны пары (без флага — dry-run план)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только план (поведение по умолчанию; ничего не запускает)",
    )
    parser.add_argument(
        "--reports-dir", type=Path, default=None,
        help="каталог отчётов: probe-<run_id>.json ОБЕИХ плеч + partial по "
             "мере прогона (3a/3b); рекомендуемый: " + str(REPORTS_DIR_DEFAULT)
             + " (gitignored). Требуется для --record (CV7-existing)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="дочитать partial-снапшоты и НЕ перезапускать завершённые "
             "сегменты (битый partial — отказ); требует --reports-dir",
    )
    parser.add_argument(
        "--cc1-confirmed", action="store_true",
        help="подтверждение предусловия 2f: negative-control CC1 прошёл, "
             "различимость метрики доказана (гасит ПРЕДУПРЕЖДЕНИЕ)",
    )
    parser.add_argument(
        "--record", choices=("promoted", "rejected"), default=None,
        help="Operator Gate записи решения в variants.yaml (P5); требует "
             "--confirm-record и --reports-dir; без флага — dry-run",
    )
    parser.add_argument(
        "--confirm-record", action="store_true",
        help="подтверждение записи решения (без него --record → отказ exit 2)",
    )
    parser.add_argument(
        "--decided-by", default="operator",
        help="автор решения для записи (decided_by, §7.4)",
    )
    parser.add_argument(
        "--variants-yaml", type=Path, default=DEFAULT_VARIANTS_YAML,
        help="реестр variants.yaml (по умолчанию calibration/variants.yaml)",
    )
    return parser


def _warn_2f() -> None:
    """ПРЕДУПРЕЖДЕНИЕ предусловия 2f (печатается без --cc1-confirmed)."""
    print("⚠ ПРЕДУПРЕЖДЕНИЕ 2f: шаг 3c (живая пара) запускается ТОЛЬКО после")
    print("  negative-control CC1 (tests/golden/needle-negative-control-CC1.md)")
    print("  — подтверждённой различимости метрики; без него promotion-гейт")
    print("  может опираться на неразличимую метрику (F-3ii).")
    print("  Подтверждение (после отчёта CC1): --cc1-confirmed")


def _print_plan(args: argparse.Namespace, quality_floor: float) -> None:
    """Dry-run: план эксперимента без единого вызова харнесса."""
    print("== variant-pair: ПЛАН (dry-run; ничего не запускается) ==")
    print(f"  base:         {args.base}")
    print(f"  variant:      {args.variant}")
    print(f"  golden:       {args.golden}")
    print(f"  held-out:     {args.heldout}")
    if args.needle is not None:
        print(f"  needle:       {args.needle} (M4; гейт promotion — по варианту, 2e)")
    print(f"  класс модели: {args.model_class}")
    print(f"  прогонов:     {args.runs} (медиана при N>=3)")
    print(f"  зона:         {args.zone} (private → только local-полка)")
    print(f"  движок:       {'ЖИВОЙ local-ollama (--live)' if args.live else 'стаб-полки (контурный прогон)'}")
    print(f"  пол качества: {quality_floor} "
          f"{'(из CLI)' if args.quality_floor is not None else '(Q_FLOOR зоны)'}")
    if args.rub_cap is not None:
        print(f"  кап ₽ (D6):     {args.rub_cap} (--rub-cap)")
    if args.wall_cap_s is not None:
        print(f"  кап wall (D6):  {args.wall_cap_s}s (--wall-cap)")
    if args.reports_dir is not None:
        print(f"  отчёты:       {args.reports_dir} (probe-<run_id>.json обоих "
              "плеч + partial, 3a/3b)")
    if args.resume:
        print("  resume:       да — завершённые сегменты partial пропускаются")
    print("  критерий:     evaluate_promotion §7.3 → reasons (решение — P5)")
    print("  реестр:       " + str(args.variants_yaml) + " (запись — только --record)")
    if args.cc1_confirmed:
        print("  2f:           CC1 подтверждён (--cc1-confirmed)")
    else:
        _warn_2f()
    print("живой прогон: добавить --confirm-live (Operator Gate: GPU/время)")
    print("запись решения: --record promoted|rejected + --confirm-record "
          "(отдельный Operator Gate, P5; требует --reports-dir)")


def _print_side(label: str, mode: Path, report: Any) -> None:
    needle_rate = getattr(report, "needle_rate", None)
    needle_line = f"{needle_rate:.2f}" if needle_rate is not None else "—"
    print(f"  {label:8s} {mode.name} → {report.run_id}")
    print(f"           golden={report.golden_median_score:.4f} "
          f"heldout={report.heldout_score:.4f} "
          f"disp={report.golden_dispersion:.4f} ₽={report.rub:.4f} "
          f"wall={report.wall_s:.1f}s needle={needle_line} "
          f"flags={list(report.flags) or '—'}")


def main(argv: list[str] | None = None, *, llm: Any = None) -> int:
    """CLI variant-pair. 0 — план/пара/запись; 1 — probe прерван гейтом;
    2 — гейты CLI (live/record без подтверждения, полка, CV0–CV7)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.runs < probe_mod.MIN_RUNS:
        parser.error(f"--runs должен быть >= {probe_mod.MIN_RUNS} (спека: медиана при N>=3)")
    if args.resume and args.reports_dir is None:
        parser.error("--resume требует --reports-dir (partial живёт в каталоге отчётов)")
    if args.record is not None and args.reports_dir is None:
        parser.error("--record требует --reports-dir (CV7-existing: запись "
                     "резолвит probe-<run_id>.json обоих плеч)")

    quality_floor = (
        args.quality_floor
        if args.quality_floor is not None
        else cf.q_floor_for(args.zone)
    )

    # Operator Gate 1 — живой прогон: --live без подтверждения → отказ ДО плана
    if args.live and not args.confirm_live:
        print(
            "ОТКАЗ: --live требует --confirm-live — живой прогон это Operator Gate",
            file=sys.stderr,
        )
        return _LIVE_WITHOUT_CONFIRM_EXIT

    # Operator Gate 2 — запись решения: --record без подтверждения → отказ
    if args.record is not None and not args.confirm_record:
        print(
            "ОТКАЗ: --record требует --confirm-record — запись promoted/rejected "
            "это отдельный Operator Gate (P5: решение оператора)",
            file=sys.stderr,
        )
        return _RECORD_WITHOUT_CONFIRM_EXIT

    if not args.confirm_live:
        _print_plan(args, quality_floor)
        return 0

    # ── прогоны пары (явный --confirm-live) ──
    if not args.cc1_confirmed:
        _warn_2f()

    client = llm
    if client is None:
        try:
            # живой путь переиспользуется из probe-run (без дублирования):
            # preflight-ping + OllamaClient либо контурный стаб
            client = probe_run.resolve_llm_client(args.live)
        except probe_run.LiveShelfUnavailable as exc:
            print(f"ОТКАЗ: {exc}", file=sys.stderr)
            return _LIVE_SHELF_UNAVAILABLE_EXIT
    model_facts: Any = probe_run._resolve_live_facts() if args.live else None

    registry = Registry(golden_run.AI_WORKSPACE_DIR / "registry")
    reports: dict[str, probe_mod.ProbeReport] = {}
    for side, mode in (("base", args.base), ("variant", args.variant)):
        try:
            reports[side] = probe_mod.run_probe(
                mode=mode,
                golden=args.golden,
                heldout=args.heldout,
                needle=args.needle,
                model_class=args.model_class,
                registry=registry,
                llm=client,
                runs=args.runs,
                zone=args.zone,
                model_facts=model_facts,
                reports_dir=args.reports_dir,
                resume=args.resume,
            )
        except probe_mod.ProbeAborted as exc:
            print(f"probe прерван (гейт, плечо {side}): {exc}", file=sys.stderr)
            return _PROBE_ABORTED_EXIT
        # 3a: отчёт плеча на носителе СРАЗУ после замера — ДО критерия
        # (имя резолвит CV7-existing при --record)
        if args.reports_dir is not None:
            probe_mod.write_report(reports[side], args.reports_dir)

    verdict = variants_mod.evaluate_promotion(
        reports["base"], reports["variant"],
        quality_floor=quality_floor,
        rub_cap=args.rub_cap, wall_cap_s=args.wall_cap_s,
    )
    print("== пара base-vs-variant ==")
    _print_side("base", args.base, reports["base"])
    _print_side("variant", args.variant, reports["variant"])
    print(f"  пол качества: {quality_floor}")
    print(f"== evaluate_promotion (§7.3): passed={verdict['passed']} ==")
    if verdict["reasons"]:
        print("  причины (что НЕ выполнено):")
        for reason in verdict["reasons"]:
            print(f"   - {reason}")
    else:
        print("  причины: — (все условия §7.3 выполнены)")

    # ── запись решения: отдельный Operator Gate (по умолчанию dry-run) ──
    if args.record is None:
        print("== решение: Operator Gate (P5) — по умолчанию НЕ записано ==")
        print("  записать: --record promoted|rejected + --confirm-record "
              "(после прогона пары; повтор без пересчёта — --resume)")
        return 0

    if not verdict["passed"]:
        print("  ⚠ расхождение: записывается "
              f"{args.record!r}, а критерий §7.3 не пройден (passed=False) — "
              "решение и ответственность оператора (P5)")
    probe_pair = {
        "base": reports["base"].run_id,
        "variant": reports["variant"].run_id,
    }
    try:
        variants_mod.record_decision(
            args.variants_yaml,
            args.variant.stem,
            args.record,
            probe_pair,
            args.decided_by,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            reports_dir=args.reports_dir,
        )
    except ValueError as exc:
        print(f"ОТКАЗ записи (реестр не тронут): {exc}", file=sys.stderr)
        return _RECORD_INVALID_EXIT
    print("== решение записано: " + str(args.variants_yaml)
          + f" ({args.variant.stem} → {args.record}) ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
