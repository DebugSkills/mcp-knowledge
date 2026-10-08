"""Э3-2 Ф7 CLI: probe-run → draft-профиль калибровки (arch-2026-10-08-f7-calibration).

Пайплайн: ``calibration.probe.run_probe`` (композиция golden_run-харнесса;
``engine_factory`` — из ``golden_run``: реальный Redis-контур, стаб-полки)
→ ``calibration.policy.propose_scalars`` (рамка D6: качество-first, ₽/wall —
ограничения) → ``calibration.profiles.build_draft_profile`` (схема
``calibration-profile/1``, ``status: draft``) → ``calibration.profiles.write_profile``
(валидация fail-closed → ``profiles_dir/<profile_id>.yaml``).

**Живой прогон = Operator Gate** (GPU/время; ext-полка — реальные ₽):

- по умолчанию (и с явным ``--dry-run``) — печатает ПЛАН, ничего не запускает;
- запуск только с явным ``--confirm-live``;
- ``--ext`` (разрешение ext-полки) без ``--confirm-live`` — отказ (exit 2).

Если рамка D6 не пройдена — профиль НЕ пишется (схема требует скаляры;
пустое предложение публиковать нельзя), печатается причина, выход 1.
Утверждение профиля (``draft`` → ``calibrated``) — отдельный шаг оператора (P5).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ai_workspace import conformance as cf
from ai_workspace.calibration import policy, profiles
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.registry import Registry
from ai_workspace.tools import golden_run

#: Каталог профилей по умолчанию (дизайн §5.1: ``calibration/profiles/*.yaml``)
DEFAULT_PROFILES_DIR: Path = golden_run.AI_WORKSPACE_DIR / "calibration" / "profiles"

_EXT_WITHOUT_LIVE_EXIT = 2
_D6_NOT_APPLIED_EXIT = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe-run",
        description="Probe-suite → draft-профиль калибровки (живой прогон — Operator Gate)",
    )
    parser.add_argument(
        "--mode", type=Path, default=golden_run.DEFAULT_MODE,
        help="режим (mode YAML) для прогона",
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
        "--ext", action="store_true",
        help="явное разрешение ext-полки (реальные ₽); требует --confirm-live",
    )
    parser.add_argument(
        "--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
        help="каталог draft-профилей (по умолчанию calibration/profiles)",
    )
    parser.add_argument(
        "--quality-floor", type=float, default=None,
        help="пол качества D6 (P5: оператор); по умолчанию Q_FLOOR зоны (conformance)",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="Operator Gate: запустить живой прогон (без флага — dry-run план)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только план (поведение по умолчанию; ничего не запускает)",
    )
    return parser


def _print_plan(args: argparse.Namespace, quality_floor: float) -> None:
    """Dry-run: план прогона без единого вызова харнесса."""
    print("== probe-run: ПЛАН (dry-run; ничего не запускается) ==")
    print(f"  режим:        {args.mode}")
    print(f"  golden:       {args.golden}")
    print(f"  held-out:     {args.heldout}")
    print(f"  класс модели: {args.model_class}")
    print(f"  прогонов:     {args.runs} (медиана при N>=3)")
    print(f"  зона:         {args.zone} (private → только local-полка)")
    print(f"  ext-полка:    {'разрешена (--ext)' if args.ext else 'не разрешена'}")
    print(f"  пол качества: {quality_floor} "
          f"{'(из CLI)' if args.quality_floor is not None else '(Q_FLOOR зоны)'}")
    print(f"  профили:      {args.profiles_dir} (status: draft, утверждает оператор P5)")
    print("живой прогон: добавить --confirm-live (Operator Gate: GPU/время;"
      " ext-полка — реальные ₽ и требует --ext)")


def _print_report(report: Any, quality_floor: float, proposal: dict, profile_path: Path | None) -> None:
    print("== ProbeReport ==")
    print(f"  run_id:              {report.run_id}")
    print(f"  model_id / digest:   {report.model_id} / {report.digest}")
    print(f"  golden_manifest:     {report.golden_manifest[:16]}…")
    print(f"  pricing_manifest:    {report.pricing_manifest[:16]}…")
    print(f"  golden_median_score: {report.golden_median_score:.4f} (пол {quality_floor})")
    print(f"  heldout_score:       {report.heldout_score:.4f} (F6: раздельно)")
    print(f"  golden_dispersion:   {report.golden_dispersion:.4f} (M5)")
    print(f"  parse_rate / rub / wall_s: {report.parse_rate:.2f} / {report.rub:.4f} ₽"
          f" / {report.wall_s:.1f}s")
    print(f"  n_runs: {report.n_runs}; flags: {list(report.flags) or '—'}")
    print(f"== Рамка D6: applied={proposal['applied']} ==")
    print(f"  {proposal['reason']}")
    if profile_path is not None:
        print(f"== draft-профиль: {profile_path} (status: draft; утверждение — оператор P5) ==")


def main(argv: list[str] | None = None, *, engine_factory: Any = None) -> int:
    """CLI probe-run. 0 — план/успех; 1 — D6 не пройдена/прогон прерван; 2 — гейт CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.runs < probe_mod.MIN_RUNS:
        parser.error(f"--runs должен быть >= {probe_mod.MIN_RUNS} (спека: медиана при N>=3)")

    quality_floor = (
        args.quality_floor
        if args.quality_floor is not None
        else cf.q_floor_for(args.zone)
    )

    # Operator Gate: ext без явного живого подтверждения — отказ ДО плана
    if args.ext and not args.confirm_live:
        print(
            "ОТКАЗ: --ext (ext-полка, реальные ₽) требует --confirm-live — "
            "живой прогон это Operator Gate",
            file=sys.stderr,
        )
        return _EXT_WITHOUT_LIVE_EXIT

    if not args.confirm_live:
        _print_plan(args, quality_floor)
        return 0

    # ── живой прогон (явный --confirm-live) ──
    registry = Registry(golden_run.AI_WORKSPACE_DIR / "registry")
    factory = engine_factory
    if factory is None:
        config = golden_run.RunConfig(
            golden=args.golden, mode=args.mode, min_runs=args.runs
        )
        factory = golden_run._default_engine_factory(config)

    try:
        report = probe_mod.run_probe(
            mode=args.mode,
            golden=args.golden,
            heldout=args.heldout,
            model_class=args.model_class,
            registry=registry,
            engine_factory=factory,
            runs=args.runs,
            zone=args.zone,
        )
    except probe_mod.ProbeAborted as exc:
        print(f"probe прерван (гейт): {exc}", file=sys.stderr)
        return _D6_NOT_APPLIED_EXIT

    proposal = policy.propose_scalars(
        report, quality_floor=quality_floor
    )
    if not proposal["applied"]:
        # рамка не пройдена → пустых скаляров нет, профиль писать нельзя
        _print_report(report, quality_floor, proposal, None)
        return _D6_NOT_APPLIED_EXIT

    doc = profiles.build_draft_profile(
        report,
        model_class=args.model_class,
        scalars=proposal["scalars"],
        quality_floor=quality_floor,
        constraints=proposal["constraints"],
    )
    profile_path = profiles.write_profile(args.profiles_dir, doc)
    _print_report(report, quality_floor, proposal, profile_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
