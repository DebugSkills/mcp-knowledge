"""Э3-2 Ф7 CLI: probe-run → draft-профиль калибровки (arch-2026-10-08-f7-calibration).

Пайплайн: ``calibration.probe.run_probe`` (живой измеритель
``vp_ab_pilot.run_one``: переданный ``llm``-клиент; fix §10 LIVE-PROBE-1 —
``golden_run``-харнесс из probe удалён)
→ ``calibration.policy.propose_scalars`` (рамка D6: качество-first, ₽/wall —
ограничения) → ``calibration.profiles.build_draft_profile`` (схема
``calibration-profile/1``, ``status: draft``) → ``calibration.profiles.write_profile``
(валидация fail-closed → ``profiles_dir/<profile_id>.yaml``).

**Живой прогон = Operator Gate** (GPU/время; ext-полка — реальные ₽):

- по умолчанию (и с явным ``--dry-run``) — печатает ПЛАН, ничего не запускает;
- запуск только с явным ``--confirm-live``;
- ``--ext`` (разрешение ext-полки) без ``--confirm-live`` — отказ (exit 2);
- ``--live`` (живой local-движок, ollama ``qwen2.5:7b``) без ``--confirm-live`` —
  отказ (exit 2); при ``--live --confirm-live`` измерителю передаётся реальный
  ``OllamaClient`` (``vp_ab_pilot``, импорт; after live-prefail ping) — все
  прогоны идут в модель; без ``--live`` — контурный стаб ``StubShelfLLM``
  (``golden_run``, только для CLI-контура; сам probe golden_run не использует).

Если рамка D6 не пройдена — профиль НЕ пишется (схема требует скаляры;
пустое предложение публиковать нельзя), печатается причина, выход 1.
Утверждение профиля (``draft`` → ``calibrated``) — отдельный шаг оператора (P5).

В1-1 «Применимость»: в ``--live`` факт полки (тег+digest) резолвится до
прогона — ``fetch_local_facts`` напрямую из ``/api/tags`` полки ``OllamaClient``
(endpoint деривируется из ``OLLAMA_MODELS_URL``, F-9) — и передаётся
``run_probe(model_facts=…)``: digest доходит до ``ProbeReport`` и
``calibrated_for`` профиля. Запись профиля в ``--live`` БЕЗ фактов запрещена
(fail-closed, F-2в); escape-хатч — ``--allow-no-facts`` (пометка в отчёте).
Без ``--live`` — паритет F1: ``model_facts=None``, поведение прежнее.

В1-2 (1e): guard перезаписи (F6) — ``write_profile`` не затирает существующий
не-draft профиль (``run_id`` детерминирован → повторный probe даёт тот же
``profile_id``): без ``--bump-revision`` — отказ exit 2; с флагом — новая
ревизия ``version+1`` со сохранением статуса (никакого тихого downgrade в
``draft/version:1``).

В3-A «Lifecycle» (шаги 3a/3b, F-5/F-7):

- 3a: ``--reports-dir`` — персистенция итогового отчёта
  ``probe-<run_id>.json`` (``probe.write_report``) СРАЗУ после замера —
  ДО рамки D6 (измерение ценно и при непройденной рамке: отчёт пишется и
  в exit-1 ветках); это же имя резолвит CV7-existing в
  ``variants.validate_variants/record_decision``;
- 3b: при ``--reports-dir`` каждый завершённый сегмент (задание × прогон)
  дописывается в ``<run_id>.partial.json`` (только метрики/скаляры — тексты
  document/draft/critic_fragment НЕ едут, приватность I5); ``--resume``
  дочитывает partial и пропускает завершённые сегменты (битый partial —
  отказ, fail-closed; без ``--reports-dir`` — ошибка CLI);
- без ``--reports-dir`` — паритет F1: на носитель ничего не пишется.

В2-A «Достоверность» (шаги 2c/2d/2g):

- 2c: ₽ — точная оценка (``RunOutcome.tokens_in/tokens_out`` из usage
  ответов: вход 0.30 / выход 1.20 USD за 1M); local-полка — по-прежнему 0.0;
- 2d: режим без critic-узла → ``parse_rate`` нейтрален и НЕ публикуется —
  в отчёте печатается «—» (``ProbeReport.parse_rate_defined=False``);
- 2g: капы рамки D6 из CLI — ``--rub-cap``/``--wall-cap`` едут в
  ``propose_scalars`` (нарушение → профиль не предлагается, exit 1); кап не
  задан — прежнее поведение (только пол качества). Флаги замера (напр.
  ``ceiling``, 2b) попадают в ``evidence.flags`` профиля — гейт на approve.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ai_workspace import conformance as cf
from ai_workspace.calibration import policy, profiles
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.model_facts import (
    fetch_local_facts,
    tags_endpoint_from_models_url,
    urllib_http_get,
)
from ai_workspace.registry import Registry
from ai_workspace.tools import golden_run
from ai_workspace.tools.golden_run import StubShelfLLM
from ai_workspace.tools.vp_ab_pilot import OLLAMA_MODEL, OLLAMA_MODELS_URL, OllamaClient

#: Каталог профилей по умолчанию (дизайн §5.1: ``calibration/profiles/*.yaml``)
DEFAULT_PROFILES_DIR: Path = golden_run.AI_WORKSPACE_DIR / "calibration" / "profiles"

_EXT_WITHOUT_LIVE_EXIT = 2
_LIVE_SHELF_UNAVAILABLE_EXIT = 2   # --live: ollama не отвечает / модели нет
_LIVE_NO_FACTS_EXIT = 2   # В1-1c (F-2в): --live без фактов полки — запись профиля запрещена
_OVERWRITE_GUARD_EXIT = 2  # В1-2 (1e, F6): не-draft профиль не затирается без --bump-revision
_D6_NOT_APPLIED_EXIT = 1
_REPORTS_DIR_DEFAULT: Path = golden_run.AI_WORKSPACE_DIR / "calibration" / "reports"
#: В3-A 3b: каталог probe-отчётов (gitignored — метрики живых прогонов)


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
        "--needle", type=Path, default=None,
        help="needle-set YAML (2a, В2-B): retention M4 — grep expect_needle "
        "в выводе; отдельный файл, golden-манифест не меняется",
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
        "--live", action="store_true",
        help="живой local-движок (ollama qwen2.5:7b, ~0₽) вместо стаба; требует --confirm-live",
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
        "--rub-cap", type=float, default=None,
        help="кап ₽ рамки D6 (2g, В2-A): rub > капа → скаляры не предлагаются",
    )
    parser.add_argument(
        "--wall-cap", type=float, default=None, dest="wall_cap_s",
        help="кап wall_s (сек) рамки D6 (2g, В2-A): превышение → скаляры не предлагаются",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="Operator Gate: запустить живой прогон (без флага — dry-run план)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только план (поведение по умолчанию; ничего не запускает)",
    )
    parser.add_argument(
        "--allow-no-facts", action="store_true",
        help="escape-хатч (В1-1c): разрешить запись профиля в --live без фактов "
        "полки (fail-closed F-2в обходится осознанно; помечается в отчёте)",
    )
    parser.add_argument(
        "--bump-revision", action="store_true",
        help="guard перезаписи (F6): существующий не-draft профиль получает "
        "новую ревизию version+1 со сохранением статуса; без флага — отказ",
    )
    parser.add_argument(
        "--reports-dir", type=Path, default=None,
        help="В3-A (3a/3b): включить персистенцию probe — partial-снапшот по "
        "мере прогона + итоговый probe-<run_id>.json после замера (ДО рамки "
        "D6); рекомендуемый каталог: " + str(_REPORTS_DIR_DEFAULT)
        + " (gitignored). Без флага — паритет: ничего не пишется",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="В3-A (3b): дочитать partial-снапшот из --reports-dir и "
        "пропустить завершённые сегменты (дорогие живые прогоны не "
        "повторяются); битый partial — отказ. Требует --reports-dir; без "
        "--resume существующий partial перезаписывается",
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
    print(f"  движок:       {'ЖИВОЙ local-ollama (--live)' if args.live else 'стаб-полки (контурный прогон)'}")
    print(f"  пол качества: {quality_floor} "
          f"{'(из CLI)' if args.quality_floor is not None else '(Q_FLOOR зоны)'}")
    if args.rub_cap is not None:
        print(f"  кап ₽ (D6):     {args.rub_cap} (--rub-cap)")
    if args.wall_cap_s is not None:
        print(f"  кап wall (D6):  {args.wall_cap_s}s (--wall-cap)")
    if args.needle is not None:
        print(f"  needle:       {args.needle} (M4 retention; needle_rate —"
              " гейт D6/promotion, 2e)")
    if args.reports_dir is not None:
        print(f"  отчёты:       {args.reports_dir} (partial по мере прогона;"
              " итог probe-<run_id>.json, В3-A)")
    if args.resume:
        print("  resume:       да — завершённые сегменты partial "
              "пропускаются (битый partial — отказ)")
    print(f"  профили:      {args.profiles_dir} (status: draft, утверждает оператор P5)")
    print("живой прогон: добавить --confirm-live (Operator Gate: GPU/время;"
      " ext-полка — реальные ₽ и требует --ext)")


def _print_report(
    report: Any, quality_floor: float, proposal: dict, profile_path: Path | None,
    *, allow_no_facts: bool = False, report_path: Path | None = None,
) -> None:
    if report_path is not None:
        print(f"== отчёт probe (В3-A): {report_path} ==")
    print("== ProbeReport ==")
    print(f"  run_id:              {report.run_id}")
    print(f"  model_id / digest:   {report.model_id} / {report.digest}")
    print(f"  golden_manifest:     {report.golden_manifest[:16]}…")
    print(f"  pricing_manifest:    {report.pricing_manifest[:16]}…")
    print(f"  golden_median_score: {report.golden_median_score:.4f} (пол {quality_floor})")
    print(f"  heldout_score:       {report.heldout_score:.4f} (F6: раздельно)")
    print(f"  golden_dispersion:   {report.golden_dispersion:.4f} (M5)")
    # 2d (В2-A): без critic-узла verdict_parse_ok нейтрален (run_one ставит
    # True) — parse_rate НЕ метрика, печатаем «—», значение не публикуем
    if getattr(report, "parse_rate_defined", True):
        parse_line = f"{report.parse_rate:.2f}"
    else:
        parse_line = "— (без critic-узла метрика нейтральна, не публикуется)"
    print(f"  parse_rate / rub / wall_s: {parse_line} / {report.rub:.4f} ₽"
          f" / {report.wall_s:.1f}s")
    print(f"  n_runs: {report.n_runs}; flags: {list(report.flags) or '—'}")
    # 2a (В2-B): needle_rate публикуется только когда needle-набор прогнан
    needle_rate = getattr(report, "needle_rate", None)
    needle_line = (
        f"{needle_rate:.2f} (пол {policy.NEEDLE_RATE_FLOOR}; ниже — рамка D6"
        " не применяется, 2e)" if needle_rate is not None
        else "— (needle-набор не прогонялся; гейт 2e не срабатывает)"
    )
    print(f"  needle_rate (M4):   {needle_line}")
    print(f"== Рамка D6: applied={proposal['applied']} ==")
    print(f"  {proposal['reason']}")
    if allow_no_facts:
        print("  ⚠ факты полки не разрешены (--allow-no-facts): профиль записан"
              " без model_id/digest — применимость не заявлена (F-2в)")
    if profile_path is not None:
        print(f"== draft-профиль: {profile_path} (status: draft; утверждение — оператор P5) ==")


STAB_ANSWER = (
    "# Статья\n\nКонтурный стаб-ответ probe-run: секции непустые, маркер "
    "цитат src-0001 присутствует. " * 12
)
"""Ответ контурного стаба (без ``--live``): непустые секции + ``src-`` маркер
(длина в коридоре структурного скора); живой факт маршрута — только ``--live``."""


class LiveShelfUnavailable(RuntimeError):
    """Живой preflight: полка не отвечает или модели нет — прогон невозможен."""


def resolve_llm_client(live: bool) -> Any:
    """Preflight + LLM-клиент probe-прогона (общая точка, В3-B 3c).

    ``live=True``: реальный ``OllamaClient`` ПОСЛЕ живого preflight-ping
    (полка отвечает, модель на месте — fail-fast до первого задания);
    недоступность → ``LiveShelfUnavailable`` (CLI печатает ОТКАЗ, exit 2).
    ``live=False``: контурный стаб ``StubShelfLLM`` (только CLI-контур).
    Выделено из ``main`` для ``variant_pair``: живая логика не дублируется,
    а переиспользуется вызовом (инвариант 3c «не дублируй — вызывай»).
    """
    if live:
        probe_client = OllamaClient()
        try:
            models = probe_client.ping()
        except Exception as exc:  # полка недоступна = живой прогон невозможен
            raise LiveShelfUnavailable(
                f"local-полка недоступна ({OLLAMA_MODELS_URL}): {exc}"
            ) from exc
        if OLLAMA_MODEL not in models:
            raise LiveShelfUnavailable(
                f"модель {OLLAMA_MODEL!r} отсутствует на полке: {models}"
            )
        return probe_client
    return StubShelfLLM("local", STAB_ANSWER)


def _resolve_live_facts() -> Any:
    """Факт полки для ``--live`` (В1-1a): тег+digest из ``/api/tags``.

    Резолвится НАПРЯМУЮ ``fetch_local_facts(model_id=OLLAMA_MODEL)`` — НЕ
    ``facts_for``: тот при ``calibrated_for: null`` честно вернёт None (это
    защита drift-контура, а не источник факта для свежего замера). Endpoint —
    из ``OLLAMA_MODELS_URL`` полки ``OllamaClient`` (F-9, единый :11435).
    Полка/тег недоступны → None: решение принимает fail-closed записи (В1-1c).
    """
    return fetch_local_facts(
        urllib_http_get,
        endpoint=tags_endpoint_from_models_url(OLLAMA_MODELS_URL),
        model_id=OLLAMA_MODEL,
    )


def main(argv: list[str] | None = None, *, llm: Any = None) -> int:
    """CLI probe-run. 0 — план/успех; 1 — D6 не пройдена/прогон прерван; 2 — гейт
    CLI (ext/live без --confirm-live; --live без фактов полки — В1-1c)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.runs < probe_mod.MIN_RUNS:
        parser.error(f"--runs должен быть >= {probe_mod.MIN_RUNS} (спека: медиана при N>=3)")

    # В3-A 3b: resume без каталога отчётов бессмыслен — partial неоткуда читать
    if args.resume and args.reports_dir is None:
        parser.error("--resume требует --reports-dir (partial живёт в каталоге отчётов)")

    quality_floor = (
        args.quality_floor
        if args.quality_floor is not None
        else cf.q_floor_for(args.zone)
    )

    # Operator Gate: ext/live без явного живого подтверждения — отказ ДО плана
    gated = [flag for flag, on in (("--ext", args.ext), ("--live", args.live)) if on]
    if gated and not args.confirm_live:
        print(
            "ОТКАЗ: " + " и ".join(gated) + " требует --confirm-live — "
            "живой прогон это Operator Gate",
            file=sys.stderr,
        )
        return _EXT_WITHOUT_LIVE_EXIT

    if not args.confirm_live:
        _print_plan(args, quality_floor)
        return 0

    # ── живой прогон (явный --confirm-live) ──
    registry = Registry(golden_run.AI_WORKSPACE_DIR / "registry")
    client = llm
    if client is None:
        try:
            # общая точка живого пути (В3-B 3c): preflight-ping → OllamaClient
            # либо контурный стаб; сообщение ОТКАЗа — то же, что было в main
            client = resolve_llm_client(args.live)
        except LiveShelfUnavailable as exc:
            print(f"ОТКАЗ: {exc}", file=sys.stderr)
            return _LIVE_SHELF_UNAVAILABLE_EXIT

    # ── В1-1a: факт полки --live → digest в ProbeReport (раньше run_probe
    #    звался без model_facts — профили получали пустые model_id/digest).
    #    Паритет F1: без --live фактов нет (model_facts=None) ──
    model_facts: Any = _resolve_live_facts() if args.live else None

    try:
        report = probe_mod.run_probe(
            mode=args.mode,
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
        print(f"probe прерван (гейт): {exc}", file=sys.stderr)
        return _D6_NOT_APPLIED_EXIT

    # ── В3-A 3a: персистенция отчёта СРАЗУ после замера — ДО рамки D6
    #    (измерение ценно и при непройденной рамке/proposal; имя файла —
    #    канон report_filename, его же резолвит CV7-existing) ──
    report_path: Path | None = None
    if args.reports_dir is not None:
        report_path = probe_mod.write_report(report, args.reports_dir)

    # 2g (В2-A): капы D6 из CLI (не задан → None → прежнее поведение)
    proposal = policy.propose_scalars(
        report, quality_floor=quality_floor,
        rub_cap=args.rub_cap, wall_cap_s=args.wall_cap_s,
    )
    if not proposal["applied"]:
        # рамка не пройдена → пустых скаляров нет, профиль писать нельзя
        _print_report(report, quality_floor, proposal, None, report_path=report_path)
        return _D6_NOT_APPLIED_EXIT

    # ── В1-1c (F-2в): fail-closed — в --live профиль БЕЗ фактов полки не
    #    пишется (пустые model_id/digest легитимировали ложный зелёный);
    #    escape-хатч --allow-no-facts с пометкой в отчёте. Без --live — как
    #    было (сухой контур, паритет F1).
    no_facts = not (
        str(report.model_id or "").strip() and str(report.digest or "").strip()
    )
    if args.live and no_facts and not args.allow_no_facts:
        _print_report(report, quality_floor, proposal, None, report_path=report_path)
        print(
            "ОТКАЗ: --live без фактов полки (model_id/digest отчёта пусты) — "
            "запись профиля запрещена (fail-closed, F-2в); escape-хатч: "
            "--allow-no-facts",
            file=sys.stderr,
        )
        return _LIVE_NO_FACTS_EXIT

    doc = profiles.build_draft_profile(
        report,
        model_class=args.model_class,
        scalars=proposal["scalars"],
        quality_floor=quality_floor,
        constraints=proposal["constraints"],
    )
    # В1-2 (1e, F6): guard в write_profile — не-draft ревизия не затирается
    try:
        profile_path = profiles.write_profile(
            args.profiles_dir, doc, bump_revision=args.bump_revision
        )
    except ValueError as exc:
        _print_report(report, quality_floor, proposal, None, report_path=report_path)
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return _OVERWRITE_GUARD_EXIT
    _print_report(
        report, quality_floor, proposal, profile_path,
        allow_no_facts=args.allow_no_facts and no_facts,
        report_path=report_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
