"""В1a.1 Ф7 (arch-2026-10-08-f7-calibration): прод-проводка калибровки.

Единственная точка, где прод-конструкторы движка (``golden_run`` /
``vp_ab_pilot`` / ``f47_acceptance``) узнают об активном профиле калибровки:
реестр классов (``registry/model_classes.yaml``) даёт СЕЛЕКТОР —
``active_profile`` + ``calibrated_for.model_id``; сам факт полки читается
провайдером из ollama ``/api/tags`` (Э2-2: движок в сеть не ходит).

НЕ в pure ``api.py`` (уточнение n6): api — чистый resolver без I/O; здесь
I/O осознанное — чтение реестра/профиля и ленивый net-провайдер.

Паритет F1: нет ``active_profile`` / файла реестра / файла профиля →
``(None, None)`` → конструктор движка получает прежние дефолты → поведение
Э1 байт-в-байт (движок не правим).

Импорт-цикл: ``model_facts`` импортирует ``vp_ab_pilot`` (``OLLAMA_MODELS_URL``),
а ``vp_ab_pilot`` → ``golden_run`` (``StubMCP``). Поэтому потребители импортируют
этот модуль ЛОКАЛЬНО в функциях сборки движка, не топ-уровнем.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ai_workspace.calibration.model_facts import ModelFactsCache, facts_for
from ai_workspace.calibration.profiles import load_profile

__all__ = ["DEFAULT_PROFILES_DIR", "active_calibration"]

#: Каталог профилей по умолчанию: ``ai_workspace/calibration/profiles``
#: (рядом с ``profiles.py``; туда пишет probe, оттуда читает проводка).
DEFAULT_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

_MODEL_CLASSES_KIND = "model_classes"


def _read_model_classes(registry_path: Path | str) -> dict | None:
    """Спарсить ``model_classes.yaml``; каталог → ``<dir>/model_classes.yaml``.

    Нет файла / битый YAML / не-отображение → ``None`` (fail-soft: реестр
    необязателен для исполнения — калибровка не должна валить прогоны,
    паритет F1; движок остаётся в режиме Б).
    """
    path = Path(registry_path)
    if path.is_dir():
        path = path / f"{_MODEL_CLASSES_KIND}.yaml"
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — битый реестр ≠ сбой прогона (F1)
        return None
    return doc if isinstance(doc, dict) else None


def active_calibration(
    registry_path: Path | str,
    profiles_dir: Path | str,
    http_get: Callable[[str], dict] | None = None,
) -> tuple[dict | None, Callable[[], Any] | None]:
    """Активный профиль калибровки + провайдер фактов полки (В1a.1).

    Возврат — пара для конструктора ``ModeEngine(calibration_profile=…,
    calibration_model_facts=…)``:

    - селектор: первый класс реестра (порядок файла) с непустым
      ``active_profile``; сегодня калибруется один класс — стратегию
      нескольких выбирает Г7 (анализ §4);
    - профиль: ``load_profile(profiles_dir, active_profile)`` — гейты
      статуса/digest решает резолвер (``calibration.api``), helper не
      дублирует; файла профиля нет → ``(None, None)`` (гейт П требует
      profile — подавать один провайдер бессмысленно);
    - провайдер: zero-arg callable → ``facts_for(cls, registry, http_get,
      cache=…)``; TTL-кэш ``ModelFactsCache`` создаётся ОДИН раз здесь и
      живёт в замыкании — смысл кэша в переживании вызовов провайдера
      (движок зовёт его на каждый проход job'а, Э2-2). ``http_get=None`` →
      факты всегда ``None`` = полка не наблюдаема (поведение Э1, паритет F1).

    Реестр даёт только селектор тега (``calibrated_for.model_id``) — сам факт
    читается из ollama ``/api/tags`` (endpoint — ``DEFAULT_TAGS_ENDPOINT``
    внутри ``facts_for``), НЕ из реестра: самозамыкания нет (анализ §7 DBD).
    """
    classes = _read_model_classes(registry_path)
    if not classes:
        return (None, None)
    selector: tuple[str, str] | None = None
    for cls_name, spec in classes.items():
        if not isinstance(spec, dict):
            continue
        active = spec.get("active_profile")
        if active:
            selector = (str(cls_name), str(active))
            break
    if selector is None:
        return (None, None)
    cls, active = selector
    try:
        profile = load_profile(profiles_dir, active)
    except Exception:  # noqa: BLE001 — битый профиль ≠ сбой прогона (F1)
        profile = None
    if profile is None:
        return (None, None)
    registry = {_MODEL_CLASSES_KIND: classes}
    cache = ModelFactsCache()

    def facts_provider() -> Any:
        return facts_for(cls, registry, http_get, cache=cache)

    return (profile, facts_provider)
