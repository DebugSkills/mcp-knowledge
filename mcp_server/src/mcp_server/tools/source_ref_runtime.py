"""SourceRefIndex runtime-проводка (bibliography Ф3b2, план §3.4:166, §3.6:226).

Единая точка жизни in-memory индекса sha256 → Source-refs:
- `init_source_ref_index(store)` — startup-скан SSOT (lifespan): полный обход
  .md через MarkdownStore.reindex_scan (rglob — в run_in_executor ВНУТРИ
  store, Ф3-fix2a/P3) + parse в run_in_executor (rglob/YAML не блокируют
  event loop). Fail-safe: ошибка скана → ПУСТОЙ индекс + error-
  лог + метрика — сервер продолжает работать в деградации «блобы недоступны»,
  НИКОГДА не выдаёт их ложно (fail-closed).
- `refresh_source_ref_index(app_state)` — рескан из SSOT для write-path'ов
  структурных изменений (lifecycle deprecate/restore/merge, set_zone,
  update_source). Атомарность ядра `rescan` гарантирует отсутствие частичных
  состояний; при ошибке скана индекс ОПУСТОШАЕТСЯ (fail-closed: недоступность
  лучше ложной выдачи по устаревшему кешу — инвариант «НЕ допускай stale-index»).
- `index_add_entry` / `index_remove_id` — точечные O(1)-мутации для
  ingest (add, upsert) и delete (remove).

Политика инвалидации (robust, см. план §3.4:166):
- точечные add/remove — когда изменена ровно ОДНА запись и её SSOT-entry
  уже в руках (ingest, update_entry, delete_entry);
- rescan — структурные/каскадные изменения, где мутирует несколько записей
  или поля влияют на предикаты (status/zone/license/public_allowed):
  дешевле один атомарный ребинд из SSOT, чем N точечных правок с риском
  пропуска; частота таких операций — курирование (редко).

Потокобезопасность: как и ядро (source_ref_index.py) — async
single-threaded (config WORKERS=1 инвариант); мутации на event-loop потоке.
"""

from __future__ import annotations

import asyncio
import logging

from ..content.source_cache import invalidate_source_cache
from ..metrics import source_ref_index_errors
from .source_ref_index import SourceRefIndex, ref_from_entry

logger = logging.getLogger("mcp_knowledge.tools.source_ref_runtime")

# Имя поля на app.state (lifespan кладёт сюда индекс; хуки читают/мутируют).
SOURCE_REF_INDEX_ATTR = "source_ref_index"


async def _scan_source_entries(store) -> list:
    """Полный SSOT-скан: все Source-entries (content_type="source").

    Паттерн pipeline.reindex_blue_green (indexing/pipeline.py:468): reindex_scan
    даёт пути, каждый парсится MarkdownStore._parse_file. Парсинг всех файлов —
    в ОДНОМ run_in_executor (CPU/IO не блокирует event loop). Битые файлы
    пропускаются с warning (как list_entries/reindex).
    """
    loop = asyncio.get_running_loop()
    paths = await store.reindex_scan()

    def _parse_all() -> list:
        entries: list = []
        for path in paths:
            try:
                entry = store._parse_file(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[SOURCE_REF_INDEX] skip unparsable file %s: %s", path, exc)
                continue
            if getattr(entry.frontmatter, "content_type", None) == "source":
                entries.append(entry)
        return entries

    return await loop.run_in_executor(None, _parse_all)


async def init_source_ref_index(store) -> SourceRefIndex:
    """Построить индекс на старте (lifespan, после MarkdownStore).

    Fail-safe (план Ф3b2): падение скана НЕ роняет сервер — пустой индекс
    (деградация: blob_available_indexed=False всегда), error-лог + метрика.
    """
    index = SourceRefIndex()
    try:
        entries = await _scan_source_entries(store)
        index.rescan(entries)
        logger.info("[SOURCE_REF_INDEX] startup scan complete: %s", index.size())
    except Exception as exc:  # noqa: BLE001
        source_ref_index_errors.labels(op="startup").inc()
        logger.error(
            "[SOURCE_REF_INDEX] startup scan FAILED — serving with EMPTY index "
            "(fail-closed: blobs unavailable, never falsely granted): %s",
            exc,
            exc_info=True,
        )
        index = SourceRefIndex()
    return index


async def refresh_source_ref_index(app_state) -> dict:
    """Рескан индекса из SSOT — единая точка для write-path-хуков Ф3b2.

    Вызывается после структурных мутаций (lifecycle/set_zone/update_source).
    Индекс и store уже на app_state; отсутствие любого — no-op (хук в тестах
    или компонент не инициализирован — availability-проводка Ф4 сама решит).

    Ошибка скана → индекс опустошается (fail-closed: НЕТ stale-index после
    состоявшейся SSOT-мутации) + error-лог + метрика.
    """
    index = getattr(app_state, SOURCE_REF_INDEX_ATTR, None)
    store = getattr(app_state, "store", None)
    if index is None or store is None:
        return {"refreshed": False, "reason": "source_ref_index or store not available"}
    try:
        entries = await _scan_source_entries(store)
    except Exception as exc:  # noqa: BLE001
        source_ref_index_errors.labels(op="refresh").inc()
        logger.error(
            "[SOURCE_REF_INDEX] refresh scan FAILED after mutation — "
            "fail-closed: index EMPTIED (no stale availability): %s",
            exc,
            exc_info=True,
        )
        index.rescan([])
        return {"refreshed": False, "error": str(exc), "fail_closed": True}
    index.rescan(entries)
    # bibliography Ф4b2: SSOT Source-записи могли измениться → снапшоты
    # frontmatter в кэше цитат устарели (кэш читается ДО индекса — просто
    # сбрасываем целиком: refresh и так редкая куративная операция).
    invalidate_source_cache()
    return {"refreshed": True, **index.size()}


def index_add_entry(app_state, entry) -> bool:
    """Точечный upsert Source-ref (хук ingest/update_entry).

    Не-Source записи игнорируются (ref_from_entry → None); индекс отсутствует
    (до lifespan/в тестах) — no-op. Возвращает True, если ref добавлен.
    """
    index = getattr(app_state, SOURCE_REF_INDEX_ATTR, None)
    if index is None:
        return False
    ref = ref_from_entry(entry)
    if ref is None:
        return False
    index.add(ref)
    invalidate_source_cache(ref.source_id)
    return True


def index_remove_id(app_state, source_id: str) -> int:
    """Точечное снятие refs записи (хук delete_entry/cascade).

    Идемпотентно (0 — записи в индексе не было). Возвращает число снятых
    (sha, ref)-пар.
    """
    index = getattr(app_state, SOURCE_REF_INDEX_ATTR, None)
    if index is None or not source_id:
        return 0
    removed = index.remove(source_id)
    if removed:
        invalidate_source_cache(source_id)
    return removed
