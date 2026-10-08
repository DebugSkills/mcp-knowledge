"""Э3 Ф7: probe-runner — КОМПОЗИЦИЯ golden_run + conformance (нового харнесса НЕ заводим).
ProbeReport агрегирует QReport (M1/M5) + M2 parse_rate, M6 ₽, M7 wall_s; golden и
held-out — РАЗДЕЛЬНЫЕ поля (F6). Живой LLM не вызывается: engine_factory инъектируется.

Композиция (A8): единственный «прогон» — ``golden_run.run_golden`` (дважды: golden
и held-out); скаляры качества — из сырых ``QRow.scores`` харнесса (N прогонов на
задание уже делает харнесс: ``min_runs``; golden-YAML может его перекрыть — тогда
эффективный N честно отражается в ``ProbeReport.n_runs`` + флаг ``n_runs_lt3``).

Честные границы метрик:
- M2 parse_rate: golden_run не отдаёт verdict-parse наружу (стаб-вердикт критика
  всегда PASS и всегда парсится) → 1.0 при отсутствии данных (поле обязательное);
- M6 rub: харнесс считает ₽ только внутри per-node таблицы отчёта и наружу не
  отдаёт; local-полка без прайса by design → 0.0 (local-first probe).
"""
from __future__ import annotations

import hashlib
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_workspace import conformance as cf
from ai_workspace.calibration import drift as _drift
from ai_workspace.tools import golden_run

#: Прайс-манифест для drift T3 (тот же файл, что читает ``PricingRegistry`` харнесса)
PRICING_YAML: Path = golden_run.AI_WORKSPACE_DIR / "registry" / "pricing.yaml"

#: Спека probe-suite §143: медиана/разброс осмысленны при N>=3
MIN_RUNS: int = 3


class ProbeAborted(Exception):
    """Прогон запрещён (drift T1 / private->ext)."""


@dataclass(frozen=True)
class ProbeReport:
    """Итог probe-прогона: M1–M7 + манифесты для drift T2/T3 (дизайн §3.3, §5.1)."""

    run_id: str
    model_id: str
    digest: str
    golden_manifest: str          # sha256 golden-сета (drift T2)
    pricing_manifest: str          # sha256 pricing.yaml (drift T3)
    golden_median_score: float     # медиана per-run скаляров golden (M1, N>=3)
    golden_dispersion: float       # max−min по прогонам golden (M5)
    heldout_score: float           # ОТДЕЛЬНОЕ поле held-out набора (F6)
    parse_rate: float              # M2 (нет данных из харнесса → 1.0)
    rub: float                     # M6 (local → 0.0 by design)
    wall_s: float                  # M7, по инъектируемому clock
    n_runs: int                    # эффективный N golden-замера
    flags: tuple[str, ...] = ()    # "unstable_cell"|"parse_fail"|"n_runs_lt3"
    q_report: cf.QReport | None = None  # агрегат M1/M5 (напрямую не сериализуется)


def _hash_file(p: Path | str) -> str:
    """sha256 файла-манифеста (golden-сет / pricing)."""
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


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


def _run_scalars(q_report: cf.QReport) -> list[float]:
    """Per-run скаляры прогона: скаляр i = среднее баллов всех (задание, полка) i-го прогона.

    ``QRow.scores`` хранит сырые баллы N прогонов харнесса на строку — позиция i
    каждой строки и есть i-й прогон; медиана/разброс по этим скалярам = M1/M5.
    """
    rows = q_report.rows
    if not rows:
        return []
    n = min(len(r.scores) for r in rows)
    return [sum(r.scores[i] for r in rows) / len(rows) for i in range(n)]


def run_probe(
    *,
    mode: Path | str,
    golden: Path | str,
    heldout: Path | str,
    model_class: str,
    registry: Any,
    engine_factory: Any,
    runs: int = 3,
    zone: str = "public",
    drift_profile: dict | None = None,
    model_facts: Any = None,
    clock: Any = time.time,
) -> ProbeReport:
    """Прогнать probe-suite: drift-гейт → local-first гейт → golden×N + held-out.

    - N>=3 (``runs``); харнесс делает N прогонов на (задание, полку) сам —
      ``RunConfig.min_runs``; golden-YAML с ключом ``min_runs`` перекрывает его,
      эффективный N попадает в ``n_runs`` (флаг ``n_runs_lt3`` при N<3);
    - drift (§6.2) ПЕРЕД замером: ``t1`` или ``blocked`` → ``ProbeAborted``
      до сборки первого движка;
    - local-first (I5/P3): ``zone="private"`` → только local-полка; класс на
      ext-полке → ``ProbeAborted("private->ext запрещён")``;
    - held-out — ВТОРОЙ независимый вызов харнесса, результат в своё поле (F6);
    - ``model_id``/``digest`` — из ``model_facts`` (Mapping-контракт Э1), иначе "".
    """
    runs = int(runs)
    if runs < MIN_RUNS:
        raise ValueError(
            f"probe требует runs>={MIN_RUNS} (спека: медиана при N>=3); получено {runs}"
        )

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

    # ── local-first (I5/P3): private → только local; ext класса = запрет ──
    shelves: tuple[str, ...] = cf.SHELVES
    if zone == "private":
        shelf = _class_shelf(registry, model_class)
        if shelf is not None and shelf != "local":
            raise ProbeAborted(
                f"private->ext запрещён: класс {model_class!r} на полке {shelf!r}"
            )
        shelves = ("local",)

    golden_manifest = _hash_file(golden)
    pricing_manifest = _hash_file(PRICING_YAML)

    t0 = clock()
    # ── M1/M5: golden × N (харнесс внутри делает N прогонов на задание) ──
    golden_result = golden_run.run_golden(
        golden_run.RunConfig(
            golden=Path(golden), mode=Path(mode), min_runs=runs, shelves=shelves
        ),
        engine_factory=engine_factory,
    )
    # ── F6: held-out — отдельный набор, отдельное поле ──
    heldout_result = golden_run.run_golden(
        golden_run.RunConfig(
            golden=Path(heldout), mode=Path(mode), min_runs=runs, shelves=shelves
        ),
        engine_factory=engine_factory,
    )
    wall_s = float(clock() - t0)  # M7

    scalars = _run_scalars(golden_result.q_report)
    heldout_scalars = _run_scalars(heldout_result.q_report)
    n_runs = len(scalars)
    golden_median = float(statistics.median(scalars)) if scalars else 0.0
    golden_dispersion = float(max(scalars) - min(scalars)) if scalars else 0.0
    heldout_score = float(statistics.median(heldout_scalars)) if heldout_scalars else 0.0

    # M2/M6 — честные границы (см. докстринг модуля): данных из харнесса нет
    parse_rate = 1.0
    rub = 0.0

    model_id = str(_facts_get(model_facts, "model_id") or "")
    digest = str(_facts_get(model_facts, "digest") or "")
    run_id = "probe-" + hashlib.sha256(
        "|".join(
            (model_id, digest, model_class, golden_manifest, pricing_manifest,
             Path(mode).name, str(n_runs))
        ).encode("utf-8")
    ).hexdigest()[:12]

    flags: list[str] = []
    if golden_result.q_report.flagged:
        flags.append("unstable_cell")  # M5: вариативность строки > variability_flag
    if parse_rate < 1.0:
        flags.append("parse_fail")
    if n_runs < MIN_RUNS:
        flags.append("n_runs_lt3")  # golden-YAML перекрыл min_runs вниз — честно помечаем

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
        q_report=golden_result.q_report,
    )
