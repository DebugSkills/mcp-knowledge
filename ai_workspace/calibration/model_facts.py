"""Э2-2 Ф7 (arch-2026-10-08-f7-calibration): факты полки модели + TTL-кэш.

Факты полки — ``model_id`` (ollama-тег, мутабелен) + ``digest`` (иммутабельный
digest полки, дизайн §5.1): единственное наблюдаемое основание drift-детекта
T1 (§6.1). Движок в сеть не ходит — факты поставляет инъектируемый провайдер
``http_get(url) -> dict`` (ollama ``/api/tags``), обёрнутый в TTL-кэш с
last-known-fallback (§6.2): сетевой сбой ≠ drift — возвращается последний
известный факт + флаг ``last_known`` (warning-событие на стороне вызывающего).

``ModelFacts.get`` — Mapping-совместимый доступ: резолвер Э1
(``calibration.api``) читает факты через ``.get("model_id")/.get("digest")``,
dataclass проходит гейт T1 без конверсии.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_TAGS_ENDPOINT",
    "ModelFacts",
    "ModelFactsCache",
    "facts_for",
    "fetch_local_facts",
]

#: Штатный endpoint списка моделей локальной полки (ollama /api/tags)
DEFAULT_TAGS_ENDPOINT = "http://localhost:11434/api/tags"

#: Ключи тега в записях /api/tags (ollama отдаёт name, совместимо — model)
_TAG_KEYS: tuple[str, ...] = ("name", "model")


@dataclass(frozen=True)
class ModelFacts:
    """Наблюдаемые факты полки: тег модели + иммутабельный digest (§5.1)."""

    model_id: str
    digest: str

    def get(self, key: str, default: Any = None) -> Any:
        """Mapping-доступ (``model_id``/``digest``) — контракт ``resolve`` Э1."""
        if key == "model_id":
            return self.model_id
        if key == "digest":
            return self.digest
        return default


def fetch_local_facts(
    http_get: Callable[[str], dict],
    endpoint: str = DEFAULT_TAGS_ENDPOINT,
    *,
    model_id: str | None = None,
) -> ModelFacts | None:
    """Спросить ollama ``/api/tags`` и собрать факты тега модели.

    Ответ: ``{"models": [{"name": <tag>, "digest": <sha256:...>}, ...]}``.
    ``model_id`` — какой тег выбирать из списка (нет совпавшего тега,
    битый ответ, ошибка сети — ``None``); без фильтра берётся первый тег
    полки. Ошибка сети здесь НЕ drift (§6.2): решение принимает вызывающий
    через кэш (``ModelFactsCache.resolve_or_cached``).
    """
    try:
        doc = http_get(endpoint)
    except Exception:  # noqa: BLE001 — сеть недоступна ≠ drift (§6.2)
        return None
    models = doc.get("models") if isinstance(doc, Mapping) else None
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        tag = next((entry[key] for key in _TAG_KEYS if entry.get(key)), None)
        digest = entry.get("digest")
        if not tag or not digest:
            continue
        if model_id is None or str(tag) == str(model_id):
            return ModelFacts(model_id=str(tag), digest=str(digest))
    return None


class ModelFactsCache:
    """TTL-кэш фактов полки + last-known-fallback (сбой сети ≠ drift, §6.2)."""

    def __init__(
        self, ttl_s: float = 300.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._fresh: dict[str, tuple[float, ModelFacts]] = {}
        self._last_known: dict[str, ModelFacts] = {}

    def get(self, key: str) -> ModelFacts | None:
        """Свежий (в пределах TTL) факт по ключу; просрочен/нет — ``None``."""
        entry = self._fresh.get(key)
        if entry is None:
            return None
        ts, facts = entry
        if self._clock() - ts > self.ttl_s:
            return None
        return facts

    def put(self, key: str, facts: ModelFacts) -> None:
        """Запомнить факт: свежая копия (TTL) и last-known (переживает TTL)."""
        now = self._clock()
        self._fresh[key] = (now, facts)
        self._last_known[key] = facts

    def resolve_or_cached(
        self, key: str, fetch: Callable[[], ModelFacts | None]
    ) -> tuple[ModelFacts | None, bool]:
        """Факт по ключу: свежий кэш → ``fetch``; сбой сети → последний известный.

        Возврат ``(facts, last_known)``: ``last_known=True`` — факт взят из
        last-known-памяти после СБОЯ ``fetch`` (сетевой сбой ≠ drift, §6.2;
        caller emits warning). ``fetch`` вернул ``None`` (тега на полке нет) —
        честный ``None`` без fallback: модель пропала — не замалчиваем.
        Сбой без истории — ``(None, True)``: верификация невозможна, дрейф
        не заявляется.
        """
        cached = self.get(key)
        if cached is not None:
            return cached, False
        try:
            facts = fetch()
        except Exception:  # noqa: BLE001 — сбой сети/парса ≠ drift (§6.2)
            last = self._last_known.get(key)
            return (last, True) if last is not None else (None, True)
        if facts is not None:
            self.put(key, facts)
        return facts, False


def facts_for(
    model_class: str,
    registry: Any,
    http_get: Callable[[str], dict] | None = None,
    cache: ModelFactsCache | None = None,
    endpoint: str = DEFAULT_TAGS_ENDPOINT,
) -> ModelFacts | None:
    """Факты полки для класса модели по реестру (§6.2).

    ``local`` → ollama-факты тега из ``calibrated_for.model_id`` (через кэш,
    если задан); ``ext`` → model_id+версия из конфигурации класса (внешняя
    полка ollama-тегом не наблюдаема — SSOT-значение реестра/конфига);
    прочее (local-only, неизвестный класс, нет ``calibrated_for.model_id``) —
    ``None``: верифицировать нечего.
    """
    try:
        classes = registry.get("model_classes") or {}
    except Exception:  # noqa: BLE001 — реестр недоступен: верифицировать нечем
        return None
    spec = classes.get(model_class) if hasattr(classes, "get") else None
    if not isinstance(spec, Mapping):
        return None
    cal = spec.get("calibrated_for")
    cal = cal if isinstance(cal, Mapping) else {}
    target = cal.get("model_id")
    shelf = spec.get("shelf")
    if shelf == "local":
        if not target or http_get is None:
            return None
        if cache is None:
            return fetch_local_facts(http_get, endpoint, model_id=str(target))
        facts, _last_known = cache.resolve_or_cached(
            f"local:{target}",
            lambda: fetch_local_facts(http_get, endpoint, model_id=str(target)),
        )
        return facts
    if shelf == "ext":
        # ext-полка не наблюдаема ollama-тегом: model_id+версия — из конфигурации
        # класса (model_version | calibrated_for.digest реестра).
        digest = spec.get("model_version") or cal.get("digest")
        if not target:
            return None
        return ModelFacts(model_id=str(target), digest=str(digest or ""))
    return None
