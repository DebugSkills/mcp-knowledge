"""Метрики-ядро ws-контура (Ф6 TODO 4а/К4): инкременты ``ws:metrics:*``.

trace_id: arch-2026-10-05-ai-workspace. Подписчик node-событий
(``wiring.make_on_node_usage``) ПОСЛЕ эмита в ``ws:quota:events``
дополнительно инкрементирует Redis-хэши метрик — best-effort, без нового
стрима. Дизайн TODO 2: данные едут в событии (``tokens_estimated`` ставит
движок в ``engine._observe_node_usage``), агрегация — у подписчика; движок
в Redis напрямую не пишет.

Схема ключей (SSOT этого модуля; 4б читает её для ``/metrics``):

- ``ws:metrics:node:{kind}:{model_class}:{shelf}:{role}`` — HASH-счётчики
  (``HINCRBY``): ``calls`` +1 на каждое node-событие; ``cached`` +1 при
  cache-hit; ``tokens`` +``event["tokens"]`` (0 → поле не трогаем);
- ``ws:metrics:usage_fallback_total`` — HASH, поле ``count``: +1 когда
  событие несёт ``tokens_estimated=True`` (узел реально оценил токены
  ``chars/4`` — usage шлюза недоступен; кэш/tool-step не считаются).

Cardinality bounded (решение зафиксировано в плане REV.2 ДО кода): лейблы
СТРОГО ``{kind, model_class, shelf, role}``; ``node_id``/``job_id`` НЕ
лейблятся. Домены лейблов конечны (``kind`` — виды узлов движка,
``model_class`` — реестр классов, ``shelf`` — ``local``/``ext``, ``role`` —
роли режимов), ``None``/пусто → плейсхолдер ``none`` (tool-step без
роли/класса). Защита от мусора в значениях: символы вне
``[A-Za-z0-9_.-]`` → ``_``, обрезка до 64 символов.

Best-effort: деградация Redis (как и в ``prio.emit_event``) не валит эмит
узла/``submit`` — отказ глотается с warning (инкременты ПОСЛЕ эмита:
событие важнее счётчиков).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "METRICS_PREFIX",
    "USAGE_FALLBACK_KEY",
    "incr_node_metrics",
    "node_metrics_key",
]

logger = logging.getLogger("ai_workspace.scheduler.metrics")

METRICS_PREFIX = "ws:metrics:"
"""Префикс всех ключей метрик ws-контура (SCAN-маска 4б: ``ws:metrics:*``)."""

USAGE_FALLBACK_KEY = f"{METRICS_PREFIX}usage_fallback_total"
"""Глобальный (без лейблов) счётчик fallback-оценок токенов ``chars/4``."""

_LABEL_NONE = "none"
"""Плейсхолдер отсутствующего лейбла (tool-step: role/model_class = None)."""

_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_LABEL_MAX = 64
"""Кардинальность/гигиена ключей: один лейбл ≤ 64 симв., мусор → ``_``."""


def _label(value: Any) -> str:
    """Нормализация одного лейбла: ``None``/пусто → ``none``; прочие
    символы заменяются, длина ограничена (см. докстроку модуля)."""
    if value is None:
        return _LABEL_NONE
    text = str(value).strip()
    if not text:
        return _LABEL_NONE
    return _LABEL_UNSAFE.sub("_", text)[:_LABEL_MAX]


def node_metrics_key(
    *, kind: Any, model_class: Any, shelf: Any, role: Any
) -> str:
    """Ключ HASH-счётчиков узлов по 4 лейблам (порядок фиксирован схемой)."""
    return (
        f"{METRICS_PREFIX}node:{_label(kind)}:{_label(model_class)}"
        f":{_label(shelf)}:{_label(role)}"
    )


def incr_node_metrics(client: Any, event: Mapping[str, Any]) -> None:
    """Инкрементировать метрики-ядро по одному node-событию (best-effort).

    ``event`` — словарь события ``on_node_usage`` (контракт ``EVENT_KEYS``
    test_node_usage); читаются ``kind/model_class/shelf/role/cached/tokens/
    tokens_estimated``, недостающие ключи трактуются как отсутствующие
    (старые эмиттеры без ``tokens_estimated`` не роняют и не инкрементируют
    fallback). Отказ Redis — warning и выход: наблюдение не валит узел.
    """
    try:
        key = node_metrics_key(
            kind=event.get("kind"),
            model_class=event.get("model_class"),
            shelf=event.get("shelf"),
            role=event.get("role"),
        )
        client.hincrby(key, "calls", 1)
        if event.get("cached"):
            client.hincrby(key, "cached", 1)
        tokens = event.get("tokens")
        if tokens:
            client.hincrby(key, "tokens", int(tokens))
        if event.get("tokens_estimated"):
            client.hincrby(USAGE_FALLBACK_KEY, "count", 1)
    except Exception:  # best-effort: метрики не валят эмит/submit
        logger.warning(
            "metrics: инкремент ws:metrics:* не прошёл для узла %r "
            "(best-effort, игнор)",
            event.get("node"), exc_info=True,
        )
