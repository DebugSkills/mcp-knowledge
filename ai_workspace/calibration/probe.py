"""Э3 Ф7 (fix §10 LIVE-PROBE-1): probe — ЖИВОЙ измеритель над ``vp_ab_pilot.run_one``.

Э3-1 мерил через ``golden_run.run_golden`` — conformance-обёртку (негативный
egress-контроль + Q по стаб-ответам ``shelf_answer``): живой прогон она роняет
(``ConformanceError``), а «меряет» стаб (§10 LIVE-PROBE-1). Теперь единица
измерения — живой измеритель A/B-пилота ``vp_ab_pilot.run_one`` (реальный
``llm``-клиент движка, структурный скор 0..1 ``score_run``, auto-approve
гейтов, ``RunOutcome{tokens, job_wall_s, verdict_parse_ok}``); ``conformance``
остаётся для АГРЕГАЦИИ/порогов (``QReport``/floors). ``golden_run`` из probe
УДАЛЁН и не импортируется (A8).

Метрики ProbeReport (M1–M7, F6):
- M1 ``golden_median_score`` — медиана ВСЕХ (задание, прогон) скоров golden;
- M5 ``golden_dispersion`` — max−min по тем же скорам (флаг при > 0.15);
- M2 ``parse_rate`` — доля прогонов с распарсенным вердиктом критика
  (живой измеритель отдаёт честный ``verdict_parse_ok``); 2d (В2-A): в
  режиме без critic-узла вердикт нейтрален (``run_one`` ставит True) —
  метрика НЕ публикуется: ``parse_rate_defined=False``, CLI печатает «—»;
- M6 ``rub`` — ``PricingRegistry`` полки класса (heavy→ext: ``price_for`` ×
  токены; local → 0.0). 2c (В2-A): in/out-разбивка из ``usage`` ответов
  (``RunOutcome.tokens_in/tokens_out``) → вход по входной цене, выход по
  выходной (0.30/1.20 USD за 1M — pricing.yaml) — точная оценка; разбивки
  нет (стаб без usage) → прежняя нижняя: все токены входные;
- M7 ``wall_s`` — сумма ``job_wall_s`` прогонов golden (время измерителя,
  не обвязки); ``clock`` оставлен в контракте для обвязки/совместимости;
- M4 ``needle_rate`` — 2a (В2-B «Достоверность»): ОТДЕЛЬНЫЙ needle-набор
  (``needle=``/CLI ``--needle``; golden_manifest и метрики golden НЕ
  трогаются) — доля needle-фактов, найденных grep'ом ``expect_needle`` в
  выводе измерителя (``document``/``draft``, без LLM-оценщика) по всем
  (задание, прогон); ``None`` — набор не прогонялся (гейты 2e молчат);
- F6 ``heldout_score`` — медиана ОТДЕЛЬНОГО held-out набора в своём поле.

Один сбой не валит замер: ``outcome.status == "error"`` → скор прогона 0
(``run_one`` сам глотает исключение в ``detail``), медиана при N>=3
устойчива. ``runs`` задаётся параметром: N<3 допускается с флагом
``n_runs_lt3`` (контурный прогон), N<1 — ``ValueError``.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from ai_workspace import conformance as cf
from ai_workspace.calibration import drift as _drift
from ai_workspace.orchestrator.engine import load_mode
from ai_workspace.registry.pricing import MICRO_PER_UNIT, PricingRegistry
from ai_workspace.tools.vp_ab_pilot import AI_WORKSPACE_DIR, BASE_VARIANT, run_one

#: Прайс-манифест для drift T3 (тот же файл, что читает ``PricingRegistry``)
PRICING_YAML: Path = AI_WORKSPACE_DIR / "registry" / "pricing.yaml"

#: Спека probe-suite §143: медиана/разброс осмысленны при N>=3
MIN_RUNS: int = 3

# ── В3-A «Lifecycle» (шаги 3a/3b, F-5/F-7, приватность I5 — риск 8) ────────
#: Состав partial-сегмента СТРОГО ограничен метриками/скалярами: тексты
#: прогона в снапшот НЕ попадают (приватность I5). ``run`` — индекс прогона
#: (нужен resume-дедупликации (задание, прогон)); ``tokens`` — тотал для
#: нижней оценки M6 (стаб без usage-разбивки).
PARTIAL_SEGMENT_KEYS: tuple[str, ...] = (
    "task_id", "run", "score", "checks", "tokens", "tokens_in", "tokens_out",
    "wall_s", "needle_found",
)
#: Текстовые поля ``RunOutcome`` — в снапшот ЗАПРЕЩЕНЫ (приватность I5):
#: document/draft/critic_fragment — генерации; verdict/detail — текст
#: вердикта/ошибки; node_events/live_sample — рантайм-дампы с фрагментами.
PARTIAL_TEXT_KEYS: frozenset[str] = frozenset({
    "document", "draft", "critic_fragment", "verdict", "detail",
    "node_events", "live_sample",
})
#: Булевы критерии сегмента: verdict_parse_ok — всегда; структурные три —
#: когда вычислены (error-прогон оставляет только verdict_parse_ok).
PARTIAL_CHECK_KEYS: tuple[str, ...] = (
    "verdict_parse_ok", "sections_ok", "citation_ok", "length_ok",
)


class ProbeAborted(Exception):
    """Прогон запрещён (drift T1 / private->ext)."""


@dataclass(frozen=True)
class ProbeReport:
    """Итог probe-прогона: M1–M7 + манифесты для drift T2/T3 (дизайн §3.3, §5.1)."""

    run_id: str
    model_id: str
    digest: str
    golden_manifest: str          # sha256 golden-сета (drift T2)
    pricing_manifest: str         # sha256 pricing.yaml (drift T3)
    golden_median_score: float    # медиана (задание, прогон) скоров golden (M1, N>=3)
    golden_dispersion: float      # max−min по прогонам golden (M5)
    heldout_score: float          # ОТДЕЛЬНОЕ поле held-out набора (F6)
    parse_rate: float             # M2: доля распарсенных вердиктов (живой измеритель)
    rub: float                    # M6: PricingRegistry полки класса (local → 0.0)
    wall_s: float                 # M7: сумма job_wall_s прогонов golden
    n_runs: int                   # N прогонов на задание (задаёт runs)
    flags: tuple[str, ...] = ()   # "unstable_cell"|"parse_fail"|"n_runs_lt3"|"ceiling"
    q_report: cf.QReport | None = None  # агрегат M1/M5 (напрямую не сериализуется)
    parse_rate_defined: bool = True  # 2d (В2-A): False = нет critic-узла, метрика нейтральна
    # 2a (В2-B): M4 — доля найденных needle-фактов (grep expect_needle в
    # document/draft); None = needle-набор не прогонялся → гейты 2e не срабатывают
    needle_rate: float | None = None


def _hash_file(p: Path | str) -> str:
    """sha256 файла-манифеста (golden-сет / pricing)."""
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ── В3-A 3a: персистенция ProbeReport (F-5 — резолв CV7 по run_id) ────────


def report_filename(run_id: str) -> str:
    """Имя файла отчёта ``probe-<run_id>.json`` (канон записи И резолва CV7).

    ``run_id`` сам несёт префикс ``probe-`` (детерминизм ниже) — дубль
    префикса не допускается; «голый» id канонизируется добавлением.
    Запись (``write_report``) и проверка существования (CV7 в
    ``variants.validate_variants``) обязаны звать ОДНУ эту функцию.
    """
    rid = str(run_id)
    if not rid.strip():
        raise ValueError("run_id не может быть пустым")
    return f"{rid if rid.startswith('probe-') else 'probe-' + rid}.json"


def write_report(report: ProbeReport, reports_dir: Path | str) -> Path:
    """Сохранить ProbeReport → ``reports_dir/probe-<run_id>.json`` (В3-A 3a).

    ``dataclasses.asdict``; ``q_report`` (рантайм-агрегат QReport) в JSON не
    сериализуется → ``null``; ``flags`` → список. Каталог создаётся при
    отсутствии. Возвращает путь записанного файла — это же имя резолвит
    CV7-existing (``variants.validate_variants(reports_dir=…)``, fail-closed
    «отчёт не существует»).
    """
    data = asdict(report)
    data["q_report"] = None
    data["flags"] = list(report.flags)
    path = Path(reports_dir) / report_filename(report.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


# ── В3-A 3b: partial-снапшот/resume (F-7, приватность I5) ─────────────────


def _partial_path(reports_dir: Path | str, run_id: str) -> Path:
    return Path(reports_dir) / f"{run_id}.partial.json"


def _write_partial(path: Path, run_id: str, segments: list[dict]) -> None:
    """Перезаписать partial-целиком (сегментов мало — атомарность не критична)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"run_id": run_id, "segments": segments},
            ensure_ascii=False, indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )


def load_partial(
    reports_dir: Path | str, run_id: str,
) -> dict[tuple[str, int], dict]:
    """Прочитать partial-снапшот прогона: ``(task_id, run) → сегмент``.

    Файла нет → ``{}`` (resume «если есть»). Битый файл (не JSON / не
    отображение / сегмент без task_id-run_score) → ``ValueError``:
    тихо перезапустить дорогие живые прогоны ИЛИ тихо пропустить
    сегменты нельзя — оператор решает, что делать с файлом (fail-closed).
    """
    path = _partial_path(reports_dir, run_id)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"partial-снапшот не читается: {path}: {exc}") from exc
    segments = doc.get("segments") if isinstance(doc, dict) else None
    if not isinstance(segments, list):
        # TRY004 осознанно: битое СОДЕРЖИМОЕ файла (данные), не тип аргумента
        raise ValueError(  # noqa: TRY004
            f"partial-снапшот без списка segments: {path}")
    done: dict[tuple[str, int], dict] = {}
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("task_id"), str) \
                or not isinstance(seg.get("run"), int):
            # TRY004 осознанно: битое содержимое снапшота, не тип аргумента
            raise ValueError(  # noqa: TRY004
                f"битый сегмент partial (нет task_id/run): {path}")
        done[(seg["task_id"], seg["run"])] = seg
    return done


def _segment_checks(outcome: Any) -> dict[str, bool]:
    """Булевы критерии сегмента: verdict_parse_ok всегда, структурные —
    если вычислены (``RunOutcome.checks``); тексты не участвуют (I5)."""
    checks = {"verdict_parse_ok": bool(outcome.verdict_parse_ok)}
    doc_checks = getattr(outcome, "checks", None)
    if doc_checks is not None:
        for key in ("sections_ok", "citation_ok", "length_ok"):
            checks[key] = bool(getattr(doc_checks, key))
    return checks


def _facts_get(facts: Any, key: str) -> Any:
    """Поле фактов полки (``ModelFacts.get`` или Mapping); нет фактов — None."""
    getter = getattr(facts, "get", None)
    return getter(key) if callable(getter) else None


def _class_shelf(registry: Any, model_class: str) -> str | None:
    """Полка класса из реестра; реестр/класс недоступны или класс-правило — None."""
    try:
        classes = registry.get("model_classes") or {}
    except Exception:  # noqa: BLE001 — реестр недоступен: верифицировать нечем
        return None
    spec = classes.get(model_class) if hasattr(classes, "get") else None
    if isinstance(spec, dict):
        shelf = spec.get("shelf")
        return str(shelf) if shelf is not None else None
    return None


def _mode_has_critic(mode: Path | str) -> bool:
    """Есть ли в режиме critic-gate-узел (2d, В2-A «Достоверность»).

    Без critic-узла ``run_one`` ставит ``verdict_parse_ok=True`` нейтрально
    (vp_ab_pilot: «критика в режиме нет») — parse_rate перестаёт быть
    измерением и не публикуется (``parse_rate_defined=False`` → CLI печатает
    «—»). Режим не читается → True: прогоны всё равно упадут в error-скор 0,
    метрику прятать молча нельзя.
    """
    try:
        graph = load_mode(mode)
    except Exception:  # noqa: BLE001 — битый режим валит прогоны, не метки
        return True
    return any(node.kind == "critic-gate" for node in graph.nodes.values())


def _load_tasks(path: Path | str) -> list[Any]:
    """Задания набора (golden/held-out): yaml ``tasks`` — непустой список."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = doc.get("tasks") if isinstance(doc, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{path}: ожидается непустой список 'tasks'")
    return tasks


def _measure(
    tasks: list[Any], *, mode: Path | str, runs: int, llm: Any, registry: Any,
    shelf: str, zone: str, q_report: cf.QReport,
    done: Mapping[tuple[str, int], Mapping] | None = None,
    on_segment: Any = None,
) -> dict[str, Any]:
    """Прогнать набор живым измерителем: (задание × run_idx) → ``run_one``.

    Возвращает сырые скаляры прогона: scores/parse_ok по каждому (задание,
    прогон), суммы tokens/job_wall_s. Q-агрегация — по строке на задачу
    (``conformance`` floors/маркеры), зона строки — из задачи (fallback —
    зона прогона).

    В3-A 3b (resume): сегмент ``(task_id, run)`` из ``done`` (partial)
    НЕ перезапускается — его скаляры идут в агрегаты как есть; новый
    сегмент передаётся в ``on_segment`` (запись partial; состав строго
    ``PARTIAL_SEGMENT_KEYS`` — тексты не едут, приватность I5).
    """
    scores: list[float] = []
    parse_ok: list[bool] = []
    tokens = 0
    tokens_in = 0
    tokens_out = 0
    wall_s = 0.0
    # 2a (В2-B): M4 — needle-счётчики (задание × прогон); error-прогон
    # вывода не даёт → честный missed (fail-closed)
    needle_total = 0
    needle_found = 0
    for task in tasks:
        task_id = str(task.get("id", "?"))
        expect = str(task.get("expect_needle") or "")
        row: list[float] = []
        for run_idx in range(1, runs + 1):
            prev = (done or {}).get((task_id, run_idx))
            if prev is not None:
                # resume: скаляры из partial, run_one не зовётся
                score = float(prev.get("score") or 0.0)
                parse = bool((prev.get("checks") or {}).get("verdict_parse_ok"))
                tokens += int(prev.get("tokens") or 0)
                tokens_in += int(prev.get("tokens_in") or 0)
                tokens_out += int(prev.get("tokens_out") or 0)
                wall_s += float(prev.get("wall_s") or 0.0)
                needle_hit: bool | None = prev.get("needle_found")
            else:
                outcome = run_one(
                    Path(mode), task, BASE_VARIANT, run_idx, llm,
                    base_registry=registry,
                )
                # один сбой не валит замер: error-прогон честно даёт скор 0
                score = 0.0 if outcome.status == "error" else float(outcome.score or 0.0)
                parse = bool(outcome.verdict_parse_ok)
                tokens += int(outcome.tokens or 0)
                tokens_in += int(outcome.tokens_in or 0)
                tokens_out += int(outcome.tokens_out or 0)
                wall_s += float(outcome.job_wall_s or 0.0)
                needle_hit = None
                if expect:
                    haystack = "\n".join(
                        part for part in (outcome.document, outcome.draft) if part
                    )
                    # fail-closed: error-прогон вывода не подтверждает → missed
                    needle_hit = (
                        outcome.status != "error" and expect in haystack
                    )
                if on_segment is not None:
                    # состав СТРОГО PARTIAL_SEGMENT_KEYS — без текстов (I5)
                    on_segment({
                        "task_id": task_id, "run": run_idx, "score": score,
                        "checks": _segment_checks(outcome),
                        "tokens": int(outcome.tokens or 0),
                        "tokens_in": int(outcome.tokens_in or 0),
                        "tokens_out": int(outcome.tokens_out or 0),
                        "wall_s": float(outcome.job_wall_s or 0.0),
                        "needle_found": needle_hit,
                    })
            row.append(score)
            scores.append(score)
            parse_ok.append(parse)
            if expect:
                needle_total += 1
                if needle_hit is True:
                    needle_found += 1
        q_report.add(task_id, shelf, str(task.get("zone", zone)), row)
    return {
        "scores": scores, "parse_ok": parse_ok,
        "tokens": tokens, "tokens_in": tokens_in, "tokens_out": tokens_out,
        "wall_s": wall_s,
        "needle_total": needle_total, "needle_found": needle_found,
    }


def _compute_run_id(
    model_id: str, digest: str, model_class: str,
    golden_manifest: str, pricing_manifest: str,
    mode: Path | str, n_runs: int,
) -> str:
    """Детерминированный run_id: sha256(model|digest|class|golden|pricing|
    mode.name|N)[:12] с префиксом ``probe-``. Инвариант В3-A: вычисляется ДО
    замера (partial/resume) и попадает в отчёт без изменений."""
    return "probe-" + hashlib.sha256(
        "|".join(
            (model_id, digest, model_class, golden_manifest, pricing_manifest,
             Path(mode).name, str(n_runs))
        ).encode("utf-8")
    ).hexdigest()[:12]


def run_probe(
    *,
    mode: Path | str,
    golden: Path | str,
    heldout: Path | str,
    model_class: str,
    needle: Path | str | None = None,
    registry: Any,
    llm: Any,
    runs: int = 3,
    zone: str = "public",
    drift_profile: dict | None = None,
    model_facts: Any = None,
    clock: Any = time.time,
    reports_dir: Path | str | None = None,
    resume: bool = False,
) -> ProbeReport:
    """Прогнать probe-suite живым измерителем: гейты → golden×N + held-out.

    - измерение — ``vp_ab_pilot.run_one``: режим × задание × прогон на
      переданном ``llm``-клиенте (``LLMClient``; живой local-движок или
      скриптованный стаб в тестах), вариант ``BASE_VARIANT`` — роли реестра
      как есть; ``runs`` прогонов на задание (N<3 → флаг ``n_runs_lt3``,
      N<1 → ``ValueError``);
    - drift (§6.2) ПЕРЕД замером: ``t1`` или ``blocked`` → ``ProbeAborted``
      до первого прогона;
    - local-first (I5/P3): ``zone="private"`` → только local-полка; класс на
      ext-полке → ``ProbeAborted("private->ext запрещён")``;
    - held-out — ОТДЕЛЬНЫЙ набор и ОТДЕЛЬНЫЙ замер, результат в своё поле (F6);
    - needle (2a, В2-B) — ОТДЕЛЬНЫЙ файл набора (``tasks`` + ``expect_needle``):
      свой замер тем же измерителем, результат — ``needle_rate``; метрики
      golden (M1/M5/M6/M7) и ``golden_manifest`` НЕ меняются;
    - ``model_id``/``digest`` — из ``model_facts`` (Mapping-контракт Э1), иначе "";
    - В3-A 3b: ``reports_dir`` — персистенция по мере прогона: каждый
      завершённый сегмент (задание × прогон) дописывается в
      ``reports_dir/<run_id>.partial.json`` (состав СТРОГО
      ``PARTIAL_SEGMENT_KEYS`` — только метрики/скаляры, БЕЗ текстов
      document/draft/critic_fragment: приватность I5, риск 8 анализа);
      ``resume=True`` дочитывает partial и НЕ перезапускает завершённые
      сегменты (битый partial → ``ValueError``, fail-closed). ``run_id``
      покрывает golden/pricing-манифесты + режим + N — resume предполагает
      ТЕ ЖЕ наборы held-out/needle (их манифесты в run_id не входят);
      сегменты дедуплицируются по ``(task_id, run)`` глобально (без
      префикса набора — коллизия id между golden/held-out это краевой
      случай оператора). Без ``reports_dir`` — паритет F1: ничего не пишется.
    """
    runs = int(runs)
    if runs < 1:
        raise ValueError(f"probe требует runs>=1; получено {runs}")

    # ── drift ПЕРЕД замером (§6.2): калибровался под ДРУГУЮ модель / fail-closed ──
    if drift_profile is not None:
        verdict = _drift.detect(
            drift_profile, registry, model_facts, registry_class_status=None
        )
        if verdict.status == _drift.STATUS_T1 or verdict.blocked:
            raise ProbeAborted(
                f"drift {verdict.status}{' (blocked)' if verdict.blocked else ''}: "
                f"{verdict.reason}"
            )

    # ── local-first (I5/P3): private → только local; ext-класс = запрет ──
    shelf = _class_shelf(registry, model_class)
    if zone == "private" and shelf is not None and shelf != "local":
        raise ProbeAborted(
            f"private->ext запрещён: класс {model_class!r} на полке {shelf!r}"
        )
    shelf_label = shelf if shelf is not None else "local"

    # ── 2d (В2-A): parse_rate осмыслен только при critic-узле в режиме ──
    parse_rate_defined = _mode_has_critic(mode)

    golden_manifest = _hash_file(golden)
    pricing_manifest = _hash_file(PRICING_YAML)

    # ── В3-A 3b: run_id ДО замера — partial/resume известен до первого
    #    прогона (детерминизм тот же, что и в итоговом отчёте) ──
    model_id = str(_facts_get(model_facts, "model_id") or "")
    digest = str(_facts_get(model_facts, "digest") or "")
    run_id = _compute_run_id(
        model_id, digest, model_class, golden_manifest, pricing_manifest,
        mode, runs,
    )
    done: dict[tuple[str, int], dict] = {}
    on_segment: Any = None
    if reports_dir is not None:
        partial = _partial_path(reports_dir, run_id)
        if resume:
            done = load_partial(reports_dir, run_id)
        segments: list[dict] = list(done.values())

        def on_segment(seg: dict, _path: Path = partial) -> None:
            segments.append(seg)
            _write_partial(_path, run_id, segments)

    _t0 = clock()  # M7 считается по job_wall_s измерителя; clock — контракт обвязки

    golden_tasks = _load_tasks(golden)
    heldout_tasks = _load_tasks(heldout)

    # ── M1/M5: golden × N живым измерителем + Q-агрегация (conformance) ──
    q_report = cf.QReport(min_runs=runs)
    golden_m = _measure(
        golden_tasks, mode=mode, runs=runs, llm=llm, registry=registry,
        shelf=shelf_label, zone=zone, q_report=q_report,
        done=done, on_segment=on_segment,
    )
    # ── F6: held-out — отдельный набор, отдельный замер, своё поле ──
    heldout_q = cf.QReport(min_runs=runs)
    heldout_m = _measure(
        heldout_tasks, mode=mode, runs=runs, llm=llm, registry=registry,
        shelf=shelf_label, zone=zone, q_report=heldout_q,
        done=done, on_segment=on_segment,
    )

    # ── M4 (2a, В2-B): needle-набор — отдельный файл/замер; retention —
    # доля найденных needle-фактов; манифест golden и M1/M5/M6/M7 не трогаем.
    needle_rate: float | None = None
    if needle is not None:
        needle_tasks = _load_tasks(needle)
        needle_q = cf.QReport(min_runs=runs)
        needle_m = _measure(
            needle_tasks, mode=mode, runs=runs, llm=llm, registry=registry,
            shelf=shelf_label, zone=zone, q_report=needle_q,
            done=done, on_segment=on_segment,
        )
        if needle_m["needle_total"]:
            needle_rate = needle_m["needle_found"] / needle_m["needle_total"]

    scores = golden_m["scores"]
    n_runs = runs
    golden_median = float(statistics.median(scores)) if scores else 0.0
    golden_dispersion = float(max(scores) - min(scores)) if scores else 0.0
    heldout_score = (
        float(statistics.median(heldout_m["scores"])) if heldout_m["scores"] else 0.0
    )
    # M2: живой измеритель отдаёт честный verdict_parse_ok по каждому прогону
    parse_rate = (
        sum(golden_m["parse_ok"]) / len(golden_m["parse_ok"])
        if golden_m["parse_ok"] else 1.0
    )
    wall_s = float(golden_m["wall_s"])  # M7: время измерителя, не обвязки

    # M6: полка класса local (или недоступна) → 0.0; ext — по прайсу полки.
    # 2c (В2-A): есть in/out-разбивка (usage ответов сервера) → ТОЧНАЯ
    # оценка: вход × input_per_1m + выход × output_per_1m (0.30 < 1.20 USD
    # за 1M — pricing.yaml); разбивки нет (стаб без usage) → прежняя НИЖНЯЯ:
    # все токены входные.
    rub = 0.0
    if shelf not in (None, "local"):
        price = PricingRegistry(registry).price_for(shelf)
        if golden_m["tokens_in"] or golden_m["tokens_out"]:
            rub = price.cost_micro(
                golden_m["tokens_in"], golden_m["tokens_out"]
            ) / MICRO_PER_UNIT
        else:
            rub = price.cost_micro(golden_m["tokens"], 0) / MICRO_PER_UNIT

    flags: list[str] = []
    if golden_dispersion > q_report.variability_flag:
        flags.append("unstable_cell")  # M5: разброс golden-прогонов > 0.15
    if parse_rate < 1.0:
        flags.append("parse_fail")
    if n_runs < MIN_RUNS:
        flags.append("n_runs_lt3")  # контурный прогон: медиана при N<3 не по спеке
    if golden_median >= 0.999 and golden_dispersion == 0.0:
        # 2b (В2-A): score=1.0/disp=0 — ceiling-сет НЕ различает конфигурации
        # (любая проходит D6, heldout-гейт вырожден |1.0−1.0|=0); профиль с
        # флагом не применяется без явного решения оператора (approve-гейт).
        flags.append("ceiling")

    return ProbeReport(
        run_id=run_id,
        model_id=model_id,
        digest=digest,
        golden_manifest=golden_manifest,
        pricing_manifest=pricing_manifest,
        golden_median_score=golden_median,
        golden_dispersion=golden_dispersion,
        heldout_score=heldout_score,
        parse_rate=parse_rate,
        rub=rub,
        wall_s=wall_s,
        n_runs=n_runs,
        flags=tuple(flags),
        q_report=q_report,
        parse_rate_defined=parse_rate_defined,
        needle_rate=needle_rate,
    )
