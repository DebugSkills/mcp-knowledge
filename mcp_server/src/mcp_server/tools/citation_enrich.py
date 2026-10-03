"""Read-time batch-enrichment цитатами (bibliography Ф4b2, план §3.4:189).

Проводка ядра `content/citation.py` (Ф4b1) в read-тулы:

- `enrich_results_with_citations(results, params, app_state)` — поисковая
  выдача (search_knowledge / search_by_tags): top-K → distinct source_id →
  ОДИН batch-проход чтений (без N+1: одно чтение на distinct-источник,
  TTL-кэш `content/source_cache` поверх) → citation в каждом результате;
- `citations_for_refs(refs, auth, app_state)` — get_entry: source_refs /
  source_id frontmatter → список поэлементных citation-решений.

Контракт §3.4:185-192 (НЕ ослаблять):
- `citation` добавляется ТОЛЬКО при непустом решении (None → ключа НЕТ);
- `citation_reason` — соседнее поле (НЕ внутри citation), только когда
  canonical отсутствует (`classify_reason` ≠ None; canonical есть, но
  недоступен этому auth → причины нет — это availability, не канонизация);
- auth — `params["_auth"]` (mcp_handler), прокидывается в предикаты как есть:
  зоны не ослабляются, subscriber/private/restricted → citation нет (fail-closed).

CSL в Qdrant payload НЕ пишется: enrichment только добавляет ключи в
ответ-словари, точки не трогает (§3.4:191 — правка Source без переиндексации).
"""

from __future__ import annotations

import logging

from ..content.citation import classify_reason, decide_citation
from ..content.source_cache import get_source_cache

logger = logging.getLogger("mcp_knowledge.tools.citation_enrich")


def _citation_context(app_state):
    """(exists_fn, index) для ядра цитирования; None → проводка невозможна.

    Отсутствие document_store/source_ref_index (компонент не инициализирован,
    тестовое окружение) → enrichment пропускается целиком: НЕТ citation —
    безопасная деградация (fail-closed: недоступность лучше ложной выдачи).
    """
    doc_store = getattr(app_state, "document_store", None)
    index = getattr(app_state, "source_ref_index", None)
    exists_fn = getattr(doc_store, "exists", None)
    if not callable(exists_fn) or index is None or not hasattr(index, "get"):
        return None
    return exists_fn, index


def _fm_to_dict(entry) -> dict | None:
    """Frontmatter записи → dict; None → запись нечитаема (не кэшируем miss)."""
    if entry is None:
        return None
    fm = getattr(entry, "frontmatter", None)
    if fm is None:
        return None
    dump = getattr(fm, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001 — битая запись = miss, не падение тулы
            return None
        return data if isinstance(data, dict) else None
    return fm if isinstance(fm, dict) else None


async def load_source_fm(source_id: str, app_state) -> dict | None:
    """Source-frontmatter через TTL-кэш (miss → store.read → put)."""
    cache = get_source_cache()
    fm = cache.get(source_id)
    if fm is not None:
        return fm
    entry = await app_state.store.read(source_id)
    fm = _fm_to_dict(entry)
    if fm is not None:
        cache.put(source_id, fm)
    return fm


def _locator_from_result(result: dict) -> dict | None:
    """Локатор из полей поисковой выдачи (payload-поля Ф2b2); None → нет."""
    kind = result.get("locator_kind")
    if not kind:
        return None
    locator: dict = {"kind": kind}
    if "locator_start" in result:
        locator["start"] = result["locator_start"]
    if "locator_end" in result:
        locator["end"] = result["locator_end"]
    return locator


def _decide_with_reason(source_fm: dict, locator: dict | None, auth, ctx) -> tuple[dict | None, str | None]:
    """(citation, reason): контракт ключей ответа в одном месте.

    citation None → reason тоже None (недоступный Source не диагностируем:
    fail-closed без утечки существования). citation есть ∧ canonical
    отсутствует (level ≠ "b" И classify_reason ≠ None) → reason.
    """
    exists_fn, index = ctx
    try:
        decision = decide_citation(source_fm, locator, auth, exists_fn=exists_fn, index=index)
    except Exception:  # noqa: BLE001 — битый source не роняет поиск
        logger.debug("citation_enrich: decide_citation failed", exc_info=True)
        return None, None
    if decision.citation is None:
        return None, None
    if decision.level == "b":
        return decision.citation, None
    try:
        reason = classify_reason(source_fm, exists_fn=exists_fn, index=index)
    except Exception:  # noqa: BLE001
        logger.debug("citation_enrich: classify_reason failed", exc_info=True)
        reason = None
    return decision.citation, reason


async def citations_for_refs(refs: list[tuple[str, dict | None]], auth, app_state) -> list[dict]:
    """Batch-решения по [(source_id, locator)] (get_entry-путь).

    Элемент: {source_id, citation?, citation_reason?} — те же контракты
    опциональных ключей, поэлементно. Порядок refs сохраняется; distinct
    source_id читается один раз (кэш поверх — без N+1).
    """
    ctx = _citation_context(app_state)
    if ctx is None or not refs:
        return []
    fms: dict[str, dict | None] = {}
    for source_id, _locator in refs:
        if source_id and source_id not in fms:
            fms[source_id] = await load_source_fm(source_id, app_state)
    out: list[dict] = []
    for source_id, locator in refs:
        item: dict = {"source_id": source_id}
        fm = fms.get(source_id)
        if fm is not None:
            citation, reason = _decide_with_reason(fm, locator, auth, ctx)
            if citation is not None:
                item["citation"] = citation
                if reason is not None:
                    item["citation_reason"] = reason
        out.append(item)
    return out


async def enrich_results_with_citations(results: list[dict], params: dict, app_state) -> None:
    """In-place batch-enrichment поисковой выдачи (search_knowledge/by_tags).

    Один batch-проход: distinct source_id из результатов → по одному чтению
    (TTL-кэш) → citation/citation_reason в каждом результате с source_id.
    Результаты без source_id / с недоступным Source — без ключей (НЕ null).
    """
    ctx = _citation_context(app_state)
    if ctx is None:
        return
    distinct: dict[str, None] = {}
    for result in results:
        source_id = result.get("source_id")
        if isinstance(source_id, str) and source_id:
            distinct.setdefault(source_id, None)
    if not distinct:
        return
    fms: dict[str, dict | None] = {}
    for source_id in distinct:
        fms[source_id] = await load_source_fm(source_id, app_state)
    auth = params.get("_auth")
    for result in results:
        source_id = result.get("source_id")
        fm = fms.get(source_id) if isinstance(source_id, str) else None
        if fm is None:
            continue
        citation, reason = _decide_with_reason(fm, _locator_from_result(result), auth, ctx)
        if citation is not None:
            result["citation"] = citation
            if reason is not None:
                result["citation_reason"] = reason
