"""Ф1a admin-API калибровки (arch-2026-10-09-calib-admin-ui): тонкий host-side HTTP-слой.

Тонкие обёртки над ГОТОВЫМ бэкендом ``ai_workspace/calibration/`` — ноль
дублей логики (DBD): probe-прогон и approve делегируются CLI-канонам
``tools/probe_run.main`` / ``tools/profile_approve.main`` (те же гейты,
``run_probe``/``approve_writeback``/``t1_writeback`` вызываются ОДНИМ
кодом для CLI и UI), модель/дрейф — ``model_facts``/``drift.detect``/
``profiles``/``runtime``, GPU — ``gpu.GpuContour`` + ollama ``/api/ps``.

Инварианты (план Ф1, protected trade-offs §3 анализа):

- **bind строго 127.0.0.1** (kb-console в ``network_mode: host`` ходит по
  loopback — прецедент ``MCP_SERVER_URL``; UFW-правила не нужны);
- **workers=1**: single-flight — ``asyncio.Lock``, который НЕ является
  межпроцессным ⇒ запуск СТРОГО одним процессом uvicorn (``main()``
  фиксирует ``workers=1``; многопроцессный запуск сломал бы 409-гейт);
- **CALIB_API_KEY fail-closed**: пустой ключ → отказ создания приложения
  (``create_app``) и каждого запроса; сравнение — ``secrets.compare_digest``
  (constant-time); ключ НИКОГДА не логируется и не возвращается в ответах;
- **Operator Gate на живой прогон**: ``confirm_live`` (аналог
  ``--confirm-live``); **ext-полка (₽) — ОТДЕЛЬНЫЙ флаг ``confirm_ext``,
  СТРОЖЕ CLI**, где ext+live гейтятся одним ``--confirm-live``;
- **approve fail-closed** (F-2а): ceiling без needle-доказательства /
  неполный ``calibrated_for`` → отказ (422), ничего не пишется — вся
  логика в ``profile_approve.main`` (dry-run по умолчанию, ``--confirm``
  применяет, escape-хатчи аудируются);
- **metrics-only** (приватность I5, R3): ответы собираются из ЯВНОГО
  whitelist полей, не зеркалят JSON-носители; тексты прогона
  (``document``/``draft``/``critic_fragment``) не едут ни в статус, ни в
  прогресс (partial-снапшот уже ограничен ``PARTIAL_SEGMENT_KEYS``).

Ф1b: маркеры наблюдаемости ``[CALIB-API]`` (``_log_event`` — старт/auth-отказ/
probe-start/probe-finish/approve; stdout → journald, без секретов); systemd
unit — ``ai_workspace/deploy/calib-admin-api.service``, runbook —
``docs/operations/calib-admin-api-runbook.md``, строка E5 — error-sources.md.

Ф3a (живая пара + P5): ``/calib/pair/start`` → фоновый ``variant_pair.main``
(single-flight — ОТДЕЛЬНОЕ состояние от probe), ``/calib/pair/status`` —
отчёты обоих плеч (тот же whitelist) + ``reasons`` переиспользованием
``variants.evaluate_promotion`` (критерий §7.3 — бумажка, НЕ решение);
``/calib/record`` — ИСПОЛНЕНИЕ подтверждённого оператором решения (P5:
``confirm`` обязателен, API не решает); запись — CLI-канон с ``--record
--confirm-record --resume`` на параметрах ПОСЛЕДНЕЙ пары (тот же run_id ⇒
CV7-existing резолвит те же отчёты, пересчёта нет), fail-closed отказ → 422.

Ф4a (история): ``GET /calib/reports`` — завершённые ``probe-*.json`` из
``settings.reports_dir`` (partial/битый JSON — fail-soft пропуск, не 500),
mtime DESC, элементы — whitelist ``_REPORT_FIELDS`` + ``ts`` (ISO mtime),
``limit`` clamp 1..200; ``total`` — число отчётов ДО limit (хвост для
UI); маркер ``reports-list`` (кол-во). Metrics-only (I5): тексты
прогонов не едут в историю даже из старых отчётов.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_workspace import conformance as cf
from ai_workspace.calibration import drift as drift_mod
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration import profiles as profiles_mod
from ai_workspace.calibration import runtime as runtime_mod
from ai_workspace.calibration import variants as variants_mod
from ai_workspace.calibration.model_facts import (
    fetch_local_facts,
    urllib_http_get,
)
from ai_workspace.gpu import GpuStatus, gpu_contour
from ai_workspace.tools import probe_run, profile_approve, variant_pair
from ai_workspace.tools.golden_run import AI_WORKSPACE_DIR
from ai_workspace.tools.vp_ab_pilot import OLLAMA_MODELS_URL

__all__ = [
    "BIND_HOST",
    "CALIB_API_KEY_ENV",
    "CALIB_API_PORT_ENV",
    "DEFAULT_PORT",
    "CalibSettings",
    "create_app",
    "main",
    "ps_endpoint_from_models_url",
]

#: Env-ключ аутентификации admin-API (fail-closed: пустой → отказ старта)
CALIB_API_KEY_ENV = "CALIB_API_KEY"

#: Env-порт (сам хост фиксируется 127.0.0.1 — экспозицию env не отключает)
CALIB_API_PORT_ENV = "CALIB_API_PORT"
DEFAULT_PORT = 8700

#: Bind строго loopback (kb-console host-net; UFW не нужен — план §1.2)
BIND_HOST = "127.0.0.1"

#: Носители по умолчанию — те же, что у CLI probe-run/profile-approve
DEFAULT_PROFILES_DIR: Path = profile_approve.DEFAULT_PROFILES_DIR
DEFAULT_REGISTRY_PATH: Path = profile_approve.DEFAULT_REGISTRY_PATH
DEFAULT_REPORTS_DIR: Path = AI_WORKSPACE_DIR / "calibration" / "reports"

#: Тексты прогона запрещены в ЛЮБЫХ ответах (приватность I5; R3) —
#: тест негативно проверяет отсутствие этих ключей в ответах API.
PARTIAL_TEXT_KEYS: frozenset[str] = probe_mod.PARTIAL_TEXT_KEYS

#: Whitelist метрик ProbeReport в ответах (R3: НЕ зеркало JSON-файла —
#: будущие расширения полей отчёта не должны утекать в UI автоматически)
_REPORT_FIELDS: tuple[str, ...] = (
    "run_id", "model_id", "digest",
    "golden_manifest", "pricing_manifest",
    "golden_median_score", "golden_dispersion", "heldout_score",
    "parse_rate", "parse_rate_defined", "rub", "wall_s",
    "n_runs", "flags", "needle_rate",
)


def ps_endpoint_from_models_url(models_url: str) -> str:
    """Endpoint ``/api/ps`` той же полки, что ``models_url`` (VRAM-снимок).

    Паттерн ``tags_endpoint_from_models_url`` (``model_facts.py:44-51``):
    scheme/host/port полки ``OllamaClient`` (:11435), путь — ollama
    ``/api/ps`` (загруженные в VRAM модели + ``size_vram``).
    """
    parts = urlsplit(models_url)
    return f"{parts.scheme}://{parts.netloc}/api/ps"


@dataclass(frozen=True)
class CalibSettings:
    """Инъекцируемые зависимости admin-API (тесты подменяют, прод — дефолты)."""

    api_key: str = ""
    profiles_dir: Path = DEFAULT_PROFILES_DIR
    registry_path: Path = DEFAULT_REGISTRY_PATH
    reports_dir: Path = DEFAULT_REPORTS_DIR
    http_get: Callable[[str], dict] = urllib_http_get
    ps_endpoint: str = ps_endpoint_from_models_url(OLLAMA_MODELS_URL)
    gpu_status: Callable[[], Any] | None = None
    #: прогон probe: argv → exit-код (дефолт — CLI-канон probe_run.main)
    probe_runner: Callable[[list[str]], int] | None = None
    #: approve: argv → exit-код (дефолт — CLI-канон profile_approve.main)
    approve_runner: Callable[[list[str]], int] | None = None
    #: пара base↔variant: argv → exit-код (дефолт — CLI-канон variant_pair.main)
    pair_runner: Callable[[list[str]], int] | None = None


@dataclass
class _ProbeState:
    """Single-flight состояние фонового probe (живёт в ``app.state``)."""

    #: инвариант workers=1: asyncio.Lock НЕ межпроцессный — один uvicorn
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None
    started_ts: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    error: str | None = None


@dataclass
class _PairState:
    """Single-flight состояние фоновой пары base↔variant (``app.state``).

    Отдельное состояние от probe (своя single-flight-зона, 409 независим).
    Параметры последнего старта хранятся ЗДЕСЬ и переиспользуются:
    ``/calib/pair/status`` (``reasons`` → ``evaluate_promotion``) и
    ``/calib/record`` (``--resume``: запись без пересчёта — тот же run_id).
    """

    #: инвариант workers=1: asyncio.Lock НЕ межпроцессный — один uvicorn
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None
    started_ts: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    error: str | None = None
    # параметры последней пары (для status/reason и record --resume)
    base: str | None = None
    variant: str | None = None
    model_class: str | None = None
    heldout: str | None = None
    golden: str | None = None
    needle: str | None = None
    runs: int = probe_mod.MIN_RUNS
    zone: str = "public"
    live: bool = False
    quality_floor: float = 0.0
    rub_cap: float | None = None
    wall_cap_s: float | None = None


def _utc_now() -> str:
    return profiles_mod._utc_iso(datetime.now(timezone.utc))


def _log_event(event: str, **fields: Any) -> None:
    """Единый маркер наблюдаемости ``[CALIB-API]`` (Ф1b, E5): stdout → journald.

    Строгая key-hygiene: в ``fields`` НИКОГДА не передаются ``api_key``/
    ``reason``/тексты прогона — только идентификаторы и коды исхода;
    сбор ошибок (errors_collect) получает маркер из journald (см.
    error-sources.md, service:calib-admin-api).
    """
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[CALIB-API] {event}" + (f" {parts}" if parts else ""), flush=True)


def _run_cli_capture(
    runner: Callable[[list[str]], int], argv: list[str],
) -> tuple[str, str, int]:
    """Выполнить CLI-канон в executor-потоке, захватив stdout/stderr.

    ``SystemExit`` (argparse) пойман: кандидат argv всегда валидируется
    эндпоинтом ДО запуска, проброс SystemExit убил бы фоновую задачу молча.
    """
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            code = int(runner(list(argv)))
        except SystemExit as exc:  # argparse-реджект маловероятен (см. выше)
            code = int(exc.code or 0)
    return out_buf.getvalue(), err_buf.getvalue(), code


# ── носители: чтение (обёртки без логики калибровки) ──────────────────────


def _read_classes(registry_path: Path | str) -> dict | None:
    """Плоская карта классов из реестра; битый/нет файла → None (fail-soft).

    Носитель существует в двух формах (обе легальны): прод-файл
    ``registry/model_classes.yaml`` — ПЛОСКИЙ (``{fast: {...}}``; его читают
    ``Registry``/``runtime.active_calibration``), файлы approve/t1_writeback
    CLI — обёрнутые (``{"model_classes": {...}}``; контракт
    ``profile_approve``/``drift.t1_writeback``). Здесь нормализуем обе →
    плоская карта (потребитель — ``runtime``/``drift.detect`` через
    ``{"model_classes": …}``).
    """
    path = Path(registry_path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    inner = doc.get("model_classes")
    if isinstance(inner, dict):
        return inner
    return doc


def _newest_file(directory: Path | str, pattern: str) -> Path | None:
    """Самый свежий по mtime файл шаблона (single-flight: писатель один)."""
    files = [p for p in Path(directory).glob(pattern) if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _progress_snapshot(reports_dir: Path | str) -> dict | None:
    """Прогресс из partial-снапшота: ``load_partial`` (метрики-only, I5)."""
    partial = _newest_file(reports_dir, "*.partial.json")
    if partial is None:
        return None
    run_id = partial.name[: -len(".partial.json")]
    try:
        done = probe_mod.load_partial(reports_dir, run_id)
    except ValueError as exc:  # битый partial — fail-closed маркер, не крах
        return {"run_id": run_id, "partial_error": str(exc)}
    return {"run_id": run_id, "segments_done": len(done)}


def _newest_report_public(reports_dir: Path | str, since_ts: float | None) -> dict | None:
    """Итоговый отчёт ТЕКУЩЕГО прогона (mtime >= старта) → whitelist полей."""
    report_path = _newest_file(reports_dir, "probe-*.json")
    if report_path is None:
        return None
    if since_ts is not None and report_path.stat().st_mtime < since_ts:
        return None  # отчёт прошлых прогонов — не выдаём за текущий
    try:
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    return _report_public_fields(doc)


def _report_public_fields(doc: dict) -> dict:
    """Whitelist метрик отчёта probe в ответах (R3/I5: НЕ зеркало JSON)."""
    return {key: doc[key] for key in _REPORT_FIELDS if key in doc}

def _mtime_iso(epoch: float) -> str:
    """ISO-8601 UTC mtime файла — тот же формат, что метки профилей."""
    return profiles_mod._utc_iso(datetime.fromtimestamp(epoch, tz=timezone.utc))


#: Потолок страницы истории (clamp 1..200 — UI не тянет весь каталог)
_REPORTS_LIMIT_MAX = 200


def _reports_history(reports_dir: Path | str, limit: int) -> dict:
    """История завершённых отчётов probe (Ф4a) → ``{"reports", "total"}``.

    Metrics-only (I5): каждый элемент — whitelist ``_REPORT_FIELDS`` +
    ``ts`` (ISO mtime), НЕ зеркало JSON-носителя (переиспользование
    ``_report_public_fields`` — ноль дублей whitelist). fail-soft: нет/
    битый каталог, битый JSON, partial-снапшоты — пропуск, не 500.
    Сортировка mtime DESC (свежие сверху; tie-break по имени —
    детерминизм); ``limit`` clamp 1..200; ``total`` — полное число
    завершённых отчётов ДО limit (UI знает хвост).
    """
    clamped = max(1, min(int(limit), _REPORTS_LIMIT_MAX))
    entries: list[tuple[float, str, dict]] = []
    for path in Path(reports_dir).glob("probe-*.json"):
        if path.name.endswith(".partial.json") or not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # исчез/битый — fail-soft пропуск
        if isinstance(doc, dict):
            entries.append((mtime, path.name, doc))
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    reports = [
        {**_report_public_fields(doc), "ts": _mtime_iso(mtime)}
        for mtime, _name, doc in entries[:clamped]
    ]
    return {"reports": reports, "total": len(entries)}



def _pair_report_docs(reports_dir: Path | str, since_ts: float | None) -> list[dict]:
    """JSON-отчёты плеч пары (mtime >= старта), по времени записи.

    ``variant_pair`` пишет отчёт base ДО прогона variant ⇒ порядок mtime =
    порядок плеч (``docs[0]`` — base, ``docs[1]`` — variant). partial-
    снапшоты исключаются; битый JSON пропускается (fail-soft, наблюдение).
    """
    paths = [
        p for p in Path(reports_dir).glob("probe-*.json")
        if not p.name.endswith(".partial.json") and p.is_file()
    ]
    if since_ts is not None:
        paths = [p for p in paths if p.stat().st_mtime >= since_ts]
    docs: list[dict] = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs[:2]


#: только поля критерия §7.3 — ничего лишнего в адаптер (приватность I5)
_PROMOTION_ATTRS: tuple[str, ...] = (
    "golden_median_score", "heldout_score", "golden_dispersion",
    "rub", "wall_s", "needle_rate", "flags",
)


def _promotion_view(doc: dict) -> Any:
    """Metrics-only адаптер JSON-отчёта → атрибуты ``evaluate_promotion``.

    Тот же контракт, что ``ProbeReport`` для критерия §7.3 (отсутствующее
    ``needle_rate`` → ``None`` через getattr-семантику SimpleNamespace).
    """
    return SimpleNamespace(**{k: doc[k] for k in _PROMOTION_ATTRS if k in doc})


def _pair_needle_quantum(state: _PairState) -> float | None:
    """Квант шума needle 1/(tasks×runs) — тот же расчёт, что ``variant_pair``.

    Набор нечитаем (удалён/битый) → ``None`` (строгое превосходство в
    ``evaluate_promotion``) — fail-soft для наблюдения, не для решения.
    """
    if state.needle is None:
        return None
    try:
        return 1.0 / (len(probe_mod._load_tasks(state.needle)) * state.runs)
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _class_public(spec: Any) -> dict:
    """Whitelist полей класса реестра (никаких лишних ключей наружу)."""
    if not isinstance(spec, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in ("shelf", "rule", "calibration_status", "active_profile"):
        if key in spec:
            out[key] = spec[key]
    cal = spec.get("calibrated_for")
    out["calibrated_for"] = dict(cal) if isinstance(cal, Mapping) else None
    return out


def _live_probe_hint(verdict: drift_mod.DriftResult) -> str:
    """S1-семантика (F8): T1/blocked = живой прогон ЗАПРЕЩЁН; T2/T3 = warn."""
    if verdict.status == drift_mod.STATUS_T1 or verdict.blocked:
        return "blocked"
    if verdict.status in (drift_mod.STATUS_T2, drift_mod.STATUS_T3):
        return "warn"
    return "ok"


def _default_gpu_status() -> GpuStatus:
    """Живой GPU-контур (ws-redis env ``WS_REDIS_URL``; слоты+lease)."""
    return gpu_contour().status()


# ── pydantic-тела запросов ─────────────────────────────────────────────────


class ProbeStartBody(BaseModel):
    """Параметры probe-прогона (зеркаро CLI probe-run; гейты — эндпоинтом)."""

    model_class: str = Field(min_length=1)
    heldout: str = Field(min_length=1)
    mode: str | None = None
    golden: str | None = None
    needle: str | None = None
    runs: int = Field(default=probe_mod.MIN_RUNS, ge=probe_mod.MIN_RUNS)
    zone: Literal["public", "private"] = "public"
    live: bool = False
    ext: bool = False
    confirm_live: bool = False
    confirm_ext: bool = False
    quality_floor: float | None = None
    rub_cap: float | None = None
    wall_cap_s: float | None = None
    resume: bool = False
    bump_revision: bool = False
    allow_no_facts: bool = False


class ApproveBody(BaseModel):
    """Параметры approve (зеркаро CLI profile-approve; dry-run по умолчанию)."""

    profile_id: str = Field(min_length=1)
    confirm: bool = False
    stale: bool = False
    force: bool = False
    ceiling_ok: bool = False
    reason: str | None = None


class PairStartBody(BaseModel):
    """Параметры пары base↔variant (зеркало CLI variant-pair; гейты — тут)."""

    base: str = str(variant_pair.DEFAULT_BASE_MODE)
    variant: str = str(variant_pair.DEFAULT_VARIANT_MODE)
    model_class: str = Field(min_length=1)
    heldout: str = Field(min_length=1)
    golden: str | None = None
    needle: str | None = None
    runs: int = Field(default=probe_mod.MIN_RUNS, ge=probe_mod.MIN_RUNS)
    zone: Literal["public", "private"] = "public"
    live: bool = False
    confirm_live: bool = False
    cc1_confirmed: bool = False
    quality_floor: float | None = None
    rub_cap: float | None = None
    wall_cap_s: float | None = None
    resume: bool = False


class RecordBody(BaseModel):
    """Решение P5 (promoted/rejected) — API только ИСПОЛНЯЕТ подтверждённое.

    ``variant``/``base`` — mode-YAML пути (как ``--variant``/``--base`` CLI;
    stem варианта становится id записи); отклонение от последней прогнанной
    пары → 400 (запись обязана ссылаться на её отчёты, CV7).
    """

    variant: str = Field(min_length=1)
    status: Literal["promoted", "rejected"]
    confirm: bool = False
    base: str | None = None
    model_class: str | None = None
    decided_by: str = "operator"


# ── приложение ─────────────────────────────────────────────────────────────


def create_app(settings: CalibSettings | None = None) -> FastAPI:
    """Собрать admin-API. Fail-closed: пустой ``api_key`` → отказ (ключ из
    env ``CALIB_API_KEY``, когда settings не задан явно)."""
    if settings is None:
        settings = CalibSettings(api_key=os.environ.get(CALIB_API_KEY_ENV, ""))
    if not settings.api_key.strip():
        raise RuntimeError(
            f"{CALIB_API_KEY_ENV} пуст/не задан — admin-API калибровки "
            "отказывает в старте (fail-closed)"
        )

    async def require_admin(
        x_calib_key: str | None = Header(default=None, alias="X-Calib-Key"),
    ) -> None:
        presented = x_calib_key or ""
        ok = secrets.compare_digest(
            presented.encode("utf-8"), settings.api_key.encode("utf-8")
        )
        if not ok:  # ключ в детали/маркер НЕ попадает (key-hygiene)
            _log_event("auth-refused")
            raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI(
        title="calib-admin-api",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_admin)],
    )
    app.state.calib_settings = settings
    app.state.calib_probe = _ProbeState()
    app.state.calib_pair = _PairState()
    _register_routes(app, settings)
    # Маркер старта — ЗДЕСЬ, не в main(): systemd стартует uvicorn
    # ``create_app --factory`` БЕЗ main(), иначе маркер не попадал в
    # journald (дефект observability; runbook §6). Один лог на построение.
    _log_event("start", host=BIND_HOST, port=_resolve_port(), workers=1)
    return app


def _register_routes(app: FastAPI, settings: CalibSettings) -> None:
    @app.get("/calib/model")
    def calib_model() -> dict:
        """S1 «Что сейчас?»: факты полки + активный профиль + drift T1–T3."""
        classes = _read_classes(settings.registry_path)
        registry_map: dict = {"model_classes": classes or {}}
        shelf = fetch_local_facts(settings.http_get)  # первый тег полки (инфо)

        # активный профиль + провайдер фактов — канонический helper Э1-проводки
        # (runtime.active_calibration: селектор реестра → load_profile →
        # facts_for с TTL-кэшем); здесь только имя класса для отображения
        profile, facts_provider = runtime_mod.active_calibration(
            settings.registry_path, settings.profiles_dir, settings.http_get
        )
        facts = facts_provider() if facts_provider is not None else None
        active_name: str | None = None
        active_spec: Mapping | None = None
        for name, spec in (classes or {}).items():
            if isinstance(spec, Mapping) and spec.get("active_profile"):
                active_name, active_spec = str(name), spec
                break
        verdict = drift_mod.detect(profile, registry_map, facts)
        active: dict | None = None
        if active_spec is not None:
            active = {
                "model_class": active_name,
                "profile_id": active_spec.get("active_profile"),
                "profile_status": (profile or {}).get("status"),
                "version": (profile or {}).get("version"),
                "calibrated_for": (profile or {}).get("calibrated_for"),
            }
        return {
            "shelf": (
                {"model_id": shelf.model_id, "digest": shelf.digest}
                if shelf is not None
                else None
            ),
            "active": active,
            "drift": {
                "status": verdict.status,
                "blocked": verdict.blocked,
                "reason": verdict.reason,
                "marks": list(verdict.marks),
                "live_probe": _live_probe_hint(verdict),
            },
            "classes": {
                str(name): _class_public(spec)
                for name, spec in (classes or {}).items()
            },
        }

    @app.get("/calib/gpu")
    def calib_gpu() -> dict:
        """R1-preflight: VRAM (ollama ``/api/ps``) + ws-lease GPU-слотов.

        Оба источника fail-soft (наблюдение): недоступность ≠ 5xx —
        предупреждение в UI до живого запуска решает оператор.
        """
        try:
            doc = settings.http_get(settings.ps_endpoint)
            entries = doc.get("models") if isinstance(doc, Mapping) else None
            models = [
                {
                    "name": m.get("name") or m.get("model"),
                    "digest": m.get("digest"),
                    "size_vram": m.get("size_vram"),
                    "expires_at": m.get("expires_at"),
                }
                for m in (entries or [])
                if isinstance(m, Mapping)
            ]
            ollama_ps: dict = {"available": True, "models": models}
        except Exception:  # noqa: BLE001 — полка не наблюдаема ≠ отказ API
            ollama_ps = {"available": False}
        provider = settings.gpu_status or _default_gpu_status
        try:
            status = provider()
            if isinstance(status, GpuStatus):
                status = asdict(status)
            gpu_slots: dict = {"available": True, **dict(status)}
        except Exception:  # noqa: BLE001 — ws-redis не наблюдаем ≠ отказ API
            gpu_slots = {"available": False}
        return {"ollama_ps": ollama_ps, "gpu_slots": gpu_slots}

    @app.get("/calib/reports")
    def calib_reports(limit: int = 20) -> dict:
        """Ф4a: история отчётов probe — whitelist-метрики + ts, mtime DESC.

        fail-soft (нет/битый каталог → пустой список, не 500); ``limit``
        clamp 1..200; ``total`` — число завершённых отчётов до среза.
        """
        resp = _reports_history(settings.reports_dir, limit)
        _log_event("reports-list", total=resp["total"])
        return resp

    @app.post("/calib/probe/start", status_code=202)
    async def calib_probe_start(body: ProbeStartBody) -> JSONResponse:
        """202 (фоновый прогон) | 400 (Operator Gate) | 409 (single-flight)."""
        state: _ProbeState = app.state.calib_probe

        # ── Operator Gate (аналог --confirm-live; любой реальный прогон —
        #    подтверждённый; ext-₽ — ОТДЕЛЬНЫЙ флаг, СТРОЖЕ CLI) ──
        if not body.confirm_live:
            raise HTTPException(
                status_code=400,
                detail="живой прогон — Operator Gate: требуется confirm_live "
                       "(аналог --confirm-live)",
            )
        if body.ext and not body.confirm_ext:
            raise HTTPException(
                status_code=400,
                detail="ext-полка (реальные ₽) — отдельное подтверждение "
                       "confirm_ext (строже CLI)",
            )
        path_args = (
            ("--heldout", body.heldout), ("--mode", body.mode),
            ("--golden", body.golden), ("--needle", body.needle),
        )
        argv: list[str] = []
        for flag, value in path_args:
            if value is None:
                continue
            if str(value).startswith("-"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{flag}: значение с ведущим '-' не принимается",
                )
            argv += [flag, str(value)]
        argv += [
            "--class", body.model_class,
            "--runs", str(body.runs),
            "--zone", body.zone,
            "--profiles-dir", str(settings.profiles_dir),
            "--reports-dir", str(settings.reports_dir),
            "--confirm-live",  # API-гейты уже пройдены — прогон, не dry-run
        ]
        if body.live:
            argv.append("--live")
        if body.ext:
            argv.append("--ext")
        if body.quality_floor is not None:
            argv += ["--quality-floor", str(body.quality_floor)]
        if body.rub_cap is not None:
            argv += ["--rub-cap", str(body.rub_cap)]
        if body.wall_cap_s is not None:
            argv += ["--wall-cap", str(body.wall_cap_s)]
        if body.resume:
            argv.append("--resume")
        if body.bump_revision:
            argv.append("--bump-revision")
        if body.allow_no_facts:
            argv.append("--allow-no-facts")

        # ── single-flight: check-and-set без await между проверкой и
        #    назначением (один event loop, workers=1) + asyncio.Lock в bg ──
        if state.task is not None and not state.task.done():
            raise HTTPException(
                status_code=409,
                detail="probe уже выполняется (single-flight; workers=1)",
            )
        _log_event(
            "probe-start", model_class=body.model_class, runs=body.runs,
            zone=body.zone, live=body.live, ext=body.ext,
        )
        state.task = asyncio.create_task(_probe_background(state, settings, argv))
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "poll": "/calib/probe/status"},
        )

    @app.get("/calib/probe/status")
    def calib_probe_status() -> dict:
        """Статус/прогресс/итог: partial → segments_done; итог → whitelist."""
        state: _ProbeState = app.state.calib_probe
        resp: dict[str, Any] = {
            "status": "idle",
            "run_id": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "progress": None,
            "report": None,
        }
        if state.task is None:
            return resp
        resp["progress"] = _progress_snapshot(settings.reports_dir)
        if not state.task.done():
            resp["status"] = "running"
            resp["started_at"] = state.started_at
            if isinstance(resp["progress"], dict):
                resp["run_id"] = resp["progress"].get("run_id")
            return resp
        # завершено: состояние читается СТРОГО после done() — все записи
        # ``_probe_background`` к этому моменту сделаны (гонка «прочитал
        # None до финиша задачи» исключена: файловый I/O выше — только
        # прогресс, живой state — ниже). exit 0/1 — прогон состоялся
        # (1 = рамка D6 не применена); exit 2 — гейт/live-отказ на
        # выполнении; исключение — сбой обвязки.
        resp["started_at"] = state.started_at
        resp["finished_at"] = state.finished_at
        resp["exit_code"] = state.exit_code
        resp["error"] = state.error
        resp["status"] = (
            "failed"
            if (state.error is not None or state.exit_code not in (0, 1))
            else "done"
        )
        report = _newest_report_public(
            settings.reports_dir, state.started_ts
        )
        resp["report"] = report
        if report is not None:
            resp["run_id"] = report.get("run_id")
        elif isinstance(resp["progress"], dict):
            resp["run_id"] = resp["progress"].get("run_id")
        return resp

    @app.post("/calib/approve")
    async def calib_approve(body: ApproveBody) -> dict:
        """Approve через CLI-канон: dry-run план по умолчанию, ``confirm``
        применяет двухносительно (``approve_writeback``/``t1_writeback``),
        fail-closed отказы CLI → 422 (ничего не пишется, включая аудит)."""
        for flag, value in (("--profile", body.profile_id), ("--reason", body.reason)):
            if value is not None and str(value).startswith("-"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{flag}: значение с ведущим '-' не принимается",
                )
        argv = [
            "--profile", body.profile_id,
            "--profiles-dir", str(settings.profiles_dir),
            "--registry", str(settings.registry_path),
        ]
        if body.stale:
            argv.append("--stale")
        if body.force:
            argv.append("--force")
        if body.ceiling_ok:
            argv.append("--ceiling-ok")
        if body.reason is not None:
            argv += ["--reason", body.reason]
        argv.append("--confirm" if body.confirm else "--dry-run")
        _log_event(
            "approve-start", profile_id=body.profile_id, confirm=body.confirm,
        )
        runner = settings.approve_runner or profile_approve.main
        loop = asyncio.get_running_loop()
        out, err, code = await loop.run_in_executor(
            None, _run_cli_capture, runner, argv
        )
        _log_event("approve-finish", exit=code, applied=body.confirm)
        if code == 0:
            return {
                "applied": body.confirm,
                "exit_code": code,
                "output": out[-2000:],
            }
        detail = err.strip() or "fail-closed отказ (подробности в логе)"
        if code == profile_approve._REFUSED_EXIT:
            raise HTTPException(status_code=422, detail=detail)
        raise HTTPException(
            status_code=500,
            detail=f"сбой записи, носители откачены: {detail}",
        )

    @app.post("/calib/pair/start", status_code=202)
    async def calib_pair_start(body: PairStartBody) -> JSONResponse:
        """202 (фоновая пара) | 400 (Operator Gate / ведущий '-') | 409."""
        state: _PairState = app.state.calib_pair

        # ── Operator Gate (аналог --confirm-live): API не гоняет пары
        #    «молча» — только явное подтверждение оператора ──
        if not body.confirm_live:
            raise HTTPException(
                status_code=400,
                detail="пара base↔variant — Operator Gate: требуется "
                       "confirm_live (аналог --confirm-live)",
            )
        path_args = (
            ("--base", body.base), ("--variant", body.variant),
            ("--golden", body.golden), ("--heldout", body.heldout),
            ("--needle", body.needle),
        )
        argv: list[str] = []
        for flag, value in path_args:
            if value is None:
                continue
            if str(value).startswith("-"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{flag}: значение с ведущим '-' не принимается",
                )
            argv += [flag, str(value)]
        argv += [
            "--class", body.model_class,
            "--runs", str(body.runs),
            "--zone", body.zone,
            "--reports-dir", str(settings.reports_dir),
            "--confirm-live",  # API-гейты пройдены — прогон, не dry-run
        ]
        if body.live:
            argv.append("--live")
        if body.cc1_confirmed:
            argv.append("--cc1-confirmed")
        if body.quality_floor is not None:
            argv += ["--quality-floor", str(body.quality_floor)]
        if body.rub_cap is not None:
            argv += ["--rub-cap", str(body.rub_cap)]
        if body.wall_cap_s is not None:
            argv += ["--wall-cap", str(body.wall_cap_s)]
        if body.resume:
            argv.append("--resume")

        # ── single-flight: check-and-set без await между проверкой и
        #    назначением (один event loop, workers=1) — состояние пары
        #    ОТДЕЛЬНО от probe (409 только по своей зоне) ──
        if state.task is not None and not state.task.done():
            raise HTTPException(
                status_code=409,
                detail="пара base↔variant уже выполняется "
                       "(single-flight; workers=1)",
            )
        # параметры повтора (record --resume / status reasons) — до запуска
        state.base = body.base
        state.variant = body.variant
        state.model_class = body.model_class
        state.heldout = body.heldout
        state.golden = body.golden
        state.needle = body.needle
        state.runs = body.runs
        state.zone = body.zone
        state.live = body.live
        state.quality_floor = (
            body.quality_floor
            if body.quality_floor is not None
            else cf.q_floor_for(body.zone)
        )
        state.rub_cap = body.rub_cap
        state.wall_cap_s = body.wall_cap_s
        _log_event(
            "pair-start", model_class=body.model_class, runs=body.runs,
            zone=body.zone, live=body.live,
        )
        state.task = asyncio.create_task(_pair_background(state, settings, argv))
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "poll": "/calib/pair/status"},
        )

    @app.get("/calib/pair/status")
    def calib_pair_status() -> dict:
        """Статус пары + отчёты плеч (whitelist) + reasons (§7.3)."""
        state: _PairState = app.state.calib_pair
        resp: dict[str, Any] = {
            "status": "idle",
            "run_id": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "progress": None,
            "base": None,
            "variant": None,
            "passed": None,
            "reasons": None,
        }
        if state.task is None:
            return resp
        resp["progress"] = _progress_snapshot(settings.reports_dir)
        if not state.task.done():
            resp["status"] = "running"
            resp["started_at"] = state.started_at
            if isinstance(resp["progress"], dict):
                resp["run_id"] = resp["progress"].get("run_id")
            return resp
        # завершено (записи _pair_background сделаны до done()); отчёты
        # плеч — только по mtime >= старта пары; reasons — переиспользование
        # evaluate_promotion (бумажка §7.3 для оператора, НЕ решение P5)
        resp["started_at"] = state.started_at
        resp["finished_at"] = state.finished_at
        resp["exit_code"] = state.exit_code
        resp["error"] = state.error
        resp["status"] = (
            "failed"
            if (state.error is not None or state.exit_code not in (0, 1))
            else "done"
        )
        docs = _pair_report_docs(settings.reports_dir, state.started_ts)
        if docs:
            resp["base"] = _report_public_fields(docs[0])
        if len(docs) > 1:
            resp["variant"] = _report_public_fields(docs[1])
        if isinstance(resp["variant"], dict):
            resp["run_id"] = resp["variant"].get("run_id")
        elif isinstance(resp["progress"], dict):
            resp["run_id"] = resp["progress"].get("run_id")
        if len(docs) > 1:
            verdict = variants_mod.evaluate_promotion(
                _promotion_view(docs[0]), _promotion_view(docs[1]),
                quality_floor=state.quality_floor,
                rub_cap=state.rub_cap, wall_cap_s=state.wall_cap_s,
                needle_quantum=_pair_needle_quantum(state),
            )
            resp["passed"] = verdict["passed"]
            resp["reasons"] = verdict["reasons"]
        return resp

    @app.post("/calib/record")
    async def calib_record(body: RecordBody) -> dict:
        """P5: ИСПОЛНИТЬ подтверждённое оператором решение (promoted/rejected).

        API НЕ решает — решение и ответственность оператора; ``confirm=true``
        обязателен. Запись — CLI-канон ``variant_pair.main`` с ``--record
        --confirm-record --resume`` на параметрах ПОСЛЕДНЕЙ успешной пары:
        run_id детерминирован ⇒ CV7-existing резолвит ТЕ отчёты, что видел
        оператор; полный partial ⇒ пересчёта прогонов нет. fail-closed
        отказ CLI (exit 2) → 422, носители не тронуты.
        """
        if not body.confirm:
            raise HTTPException(
                status_code=400,
                detail="запись решения — Operator Gate (P5): требуется "
                       "confirm=true (API не решает, только исполняет)",
            )
        pair: _PairState = app.state.calib_pair
        if pair.task is None or not pair.task.done():
            raise HTTPException(
                status_code=409,
                detail="нет завершённой пары base↔variant — сначала "
                       "/calib/pair/start (запись ссылается на её отчёты)",
            )
        if pair.exit_code != 0:
            raise HTTPException(
                status_code=409,
                detail=f"последняя пара завершилась с exit={pair.exit_code} "
                       "(прогон прерван) — запись возможна только по "
                       "полной паре",
            )
        base = body.base if body.base is not None else pair.base
        variant = body.variant
        model_class = (
            body.model_class if body.model_class is not None else pair.model_class
        )
        if base is None or model_class is None or pair.heldout is None:
            raise HTTPException(
                status_code=409,
                detail="параметры последней пары недоступны — повторите "
                       "/calib/pair/start",
            )
        for flag, value in (
            ("--base", base), ("--variant", variant),
            ("--class", model_class), ("--decided-by", body.decided_by),
        ):
            if str(value).startswith("-"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{flag}: значение с ведущим '-' не принимается",
                )
        if str(base) != str(pair.base) or str(variant) != str(pair.variant):
            raise HTTPException(
                status_code=400,
                detail="base/variant расходятся с последней прогнанной "
                       "парой — запись обязана ссылаться на её отчёты (CV7)",
            )
        argv = [
            "--base", str(base), "--variant", str(variant),
            "--heldout", str(pair.heldout),
            "--class", str(model_class),
            "--runs", str(pair.runs), "--zone", pair.zone,
            "--reports-dir", str(settings.reports_dir),
            "--resume", "--confirm-live",  # запись без пересчёта (3b)
            "--record", body.status, "--confirm-record",
            "--decided-by", body.decided_by,
        ]
        if pair.golden is not None:
            argv += ["--golden", str(pair.golden)]
        if pair.needle is not None:
            argv += ["--needle", str(pair.needle)]
        if pair.live:
            argv.append("--live")
        _log_event(
            "record-start", variant=Path(variant).stem, status=body.status,
        )
        runner = settings.pair_runner or variant_pair.main
        loop = asyncio.get_running_loop()
        out, err, code = await loop.run_in_executor(
            None, _run_cli_capture, runner, argv
        )
        _log_event("record-finish", exit=code, status=body.status)
        if code == 0:
            return {
                "recorded": True,
                "status": body.status,
                "output": out[-2000:],
            }
        detail = err.strip() or "fail-closed отказ (подробности в логе)"
        if code == 2:  # гейты CLI/CV0–CV7 — отказ записи, реестр не тронут
            raise HTTPException(status_code=422, detail=detail)
        raise HTTPException(status_code=500, detail=f"сбой записи: {detail}")


async def _probe_background(
    state: _ProbeState, settings: CalibSettings, argv: list[str],
) -> None:
    """Фоновый probe: sync CLI-канон в executor под asyncio.Lock.

    Инвариант: ``asyncio.Lock`` НЕ межпроцессный ⇒ процесс ОДИН
    (``main()`` запускает uvicorn с ``workers=1``).
    """
    async with state.lock:
        state.started_ts = time.time()
        state.started_at = _utc_now()
        state.finished_at = None
        state.exit_code = None
        state.stdout_tail = None
        state.stderr_tail = None
        state.error = None
        runner = settings.probe_runner or probe_run.main
        loop = asyncio.get_running_loop()
        try:
            out, err, code = await loop.run_in_executor(
                None, _run_cli_capture, runner, argv
            )
        except Exception as exc:  # noqa: BLE001 — фиксируем, не роняя task
            state.error = f"{type(exc).__name__}: {exc}"
            state.finished_at = _utc_now()
            _log_event("probe-finish", error=type(exc).__name__)
            return
        state.exit_code = code
        state.stdout_tail = out[-4000:]
        state.stderr_tail = err[-2000:]
        state.finished_at = _utc_now()
        _log_event("probe-finish", exit=code)


async def _pair_background(
    state: _PairState, settings: CalibSettings, argv: list[str],
) -> None:
    """Фоновая пара base↔variant: sync CLI-канон в executor под asyncio.Lock.

    Тот же инвариант workers=1, что ``_probe_background`` (asyncio.Lock НЕ
    межпроцессный ⇒ процесс ОДИН); состояние — ``_PairState``, отдельное
    от probe.
    """
    async with state.lock:
        state.started_ts = time.time()
        state.started_at = _utc_now()
        state.finished_at = None
        state.exit_code = None
        state.stdout_tail = None
        state.stderr_tail = None
        state.error = None
        runner = settings.pair_runner or variant_pair.main
        loop = asyncio.get_running_loop()
        try:
            out, err, code = await loop.run_in_executor(
                None, _run_cli_capture, runner, argv
            )
        except Exception as exc:  # noqa: BLE001 — фиксируем, не роняя task
            state.error = f"{type(exc).__name__}: {exc}"
            state.finished_at = _utc_now()
            _log_event("pair-finish", error=type(exc).__name__)
            return
        state.exit_code = code
        state.stdout_tail = out[-4000:]
        state.stderr_tail = err[-2000:]
        state.finished_at = _utc_now()
        _log_event("pair-finish", exit=code)


def _resolve_port() -> int:
    """Порт из env ``CALIB_API_PORT``; пусто/мусор/вне диапазона → дефолт.

    Безопасный разбор: некорректное значение в env НЕ роняет старт
    сервиса (bind и observability-маркер уходят на ``DEFAULT_PORT``).
    """
    raw = (os.environ.get(CALIB_API_PORT_ENV) or "").strip()
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def main() -> None:
    """Entrypoint uvicorn: bind 127.0.0.1, СТРОГО ``workers=1``.

    ``workers>1`` запрещён инвариантом single-flight (asyncio.Lock не
    межпроцессный): многопроцессный запуск сломал бы 409-гейт прогона.
    """
    if not os.environ.get(CALIB_API_KEY_ENV, "").strip():
        raise SystemExit(
            f"{CALIB_API_KEY_ENV} не задан — отказ старта (fail-closed)"
        )
    port = _resolve_port()  # маркер старта — в create_app (ровно один)
    import uvicorn

    uvicorn.run(create_app(), host=BIND_HOST, port=port, workers=1)


if __name__ == "__main__":
    main()
