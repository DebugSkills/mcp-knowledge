# ruff: noqa: BLE001, S110
"""C1: Reconciliation при старте — сверка Markdown SSOT ↔ Qdrant.

Задача 2.9 плана Фазы 2.

Flow:
1. Обход knowledge/**/*.md → сравнение updated_at с Qdrant payload
2. Расхождения → доиндексация (через pipeline.reindex_all)
3. Обратная сверка: Qdrant-точки без .md → удаление сирот
4. Фаза 5: parent-child orphan detection (child без parent, collection incomplete)
5. Лог: {checked, reindexed, skipped, deleted_orphans, orphaned_detected}
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..storage.markdown_store import MarkdownStore
from ..storage.qdrant_client import QdrantClient
from .pipeline import IndexingPipeline

logger = logging.getLogger("mcp_knowledge.reconcile")


class ReconcileResult:
    """Результат reconciliation."""

    def __init__(self):
        self.checked: int = 0
        self.reindexed: int = 0
        self.skipped: int = 0
        self.deleted_orphans: int = 0
        self.orphaned_detected: int = 0  # Фаза 5: parent-child orphans
        self.errors: list[str] = []

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "reindexed": self.reindexed,
            "skipped": self.skipped,
            "deleted_orphans": self.deleted_orphans,
            "orphaned_detected": self.orphaned_detected,
            "errors": self.errors,
        }


async def reconcile(
    store: MarkdownStore,
    qdrant: QdrantClient,
    pipeline: IndexingPipeline,
    knowledge_index,
    skip_reindex: bool = False,
) -> dict:
    """Выполнить полную сверку Markdown SSOT ↔ Qdrant при старте.

    Args:
        skip_reindex: True при недоступном embed (degraded-режим) — пропустить
            доиндексацию missing-записей (иначе reindex_all падает на каждом
            файле и блокирует старт сервера / уводит в рестарт-цикл).

    Returns:
        dict с результатами: {checked, reindexed, skipped, deleted_orphans, errors}
    """
    result = ReconcileResult()
    logger.info("🔍 RECONCILE: starting Markdown↔Qdrant consistency check")

    # ── Шаг 1: Прямая сверка — Markdown → Qdrant ──────────────────
    md_paths = await store.reindex_scan()
    qdrant_ids = qdrant.get_all_knowledge_ids()

    missing_in_qdrant: list[Path] = []

    for path in md_paths:
        result.checked += 1
        try:
            entry = store._parse_file(path)
            kid = entry.frontmatter.knowledge_id

            if kid not in qdrant_ids:
                # Запись есть в Markdown, но отсутствует в Qdrant
                missing_in_qdrant.append(path)
                logger.info("RECONCILE: %s not in Qdrant — will reindex", kid)
            else:
                # Проверяем updated_at (если доступен в payload)
                result.skipped += 1
        except Exception as e:
            msg = f"Failed to parse {path}: {e}"
            result.errors.append(msg)
            logger.warning("RECONCILE: %s", msg)

    # Доиндексация отсутствующих
    if missing_in_qdrant:
        if skip_reindex:
            # Degraded (Ollama недоступна): не блокируем старт reindex-циклом —
            # файлы доиндексируются после запуска Ollama (ленивый retry embed
            # или следующий старт сервера).
            logger.warning(
                "RECONCILE: %d entries missing in Qdrant — reindex SKIPPED "
                "(embedding недоступна, degraded-режим)",
                len(missing_in_qdrant),
            )
            result.skipped += len(missing_in_qdrant)
        else:
            logger.info("RECONCILE: %d entries missing in Qdrant — reindexing", len(missing_in_qdrant))
            try:
                reindex_result = await pipeline.reindex_all()
                result.reindexed = reindex_result.get("total_docs", len(missing_in_qdrant))
            except Exception as e:
                msg = f"Reindex failed: {e}"
                result.errors.append(msg)
                logger.error("RECONCILE: %s", msg)

    # ── Шаг 2: Обратная сверка — Qdrant → Markdown ──────────────────
    md_ids = set()
    for path in md_paths:
        try:
            entry = store._parse_file(path)
            md_ids.add(entry.frontmatter.knowledge_id)
        except Exception:
            pass

    orphan_ids = qdrant_ids - md_ids
    if orphan_ids:
        logger.info("RECONCILE: %d orphan points in Qdrant — deleting", len(orphan_ids))
        loop = asyncio.get_running_loop()
        for kid in orphan_ids:
            try:
                await loop.run_in_executor(None, qdrant.delete_by_knowledge_id, kid)
                result.deleted_orphans += 1
            except Exception as e:
                msg = f"Failed to delete orphan {kid}: {e}"
                result.errors.append(msg)
                logger.warning("RECONCILE: %s", msg)

    # ── Шаг 3: Перестройка INDEX.gen.yaml ───────────────────────────
    try:
        knowledge_index.rebuild_all()
        logger.info("RECONCILE: INDEX.gen.yaml rebuilt")
    except Exception as e:
        msg = f"INDEX rebuild failed: {e}"
        result.errors.append(msg)
        logger.warning("RECONCILE: %s", msg)

    # ── Шаг 4: Parent-child orphan detection (Фаза 5) ────────────────
    if skip_reindex:
        # Degraded: children не в Qdrant (embed недоступен) → тысячи ложных
        # "orphaned" issues + минуты старта. Диагностика имеет смысл только
        # при полной индексации.
        logger.warning("RECONCILE: parent-child orphan detection SKIPPED (degraded)")
    else:
        await _detect_parent_child_orphans(store, md_paths, result)

    summary = result.to_dict()
    logger.info(
        "✅ RECONCILE complete: checked=%d, reindexed=%d, skipped=%d, "
        "orphans=%d, orphaned_detected=%d, errors=%d",
        result.checked, result.reindexed, result.skipped,
        result.deleted_orphans, result.orphaned_detected, len(result.errors),
    )
    return summary


async def _detect_parent_child_orphans(
    store: MarkdownStore,
    md_paths: list[Path],
    result: ReconcileResult,
) -> None:
    """Фаза 5 §6.5: обнаружение parent-child orphan-записей.

    - Child с parent_knowledge_id, где parent отсутствует → issue "orphaned"
    - Collection с incomplete children (child из children[] удалён) → issue "orphaned"
    """
    try:
        from mcp_server.quality.issues import create_issue_async
    except ImportError:
        logger.warning("RECONCILE: create_issue_async not available — skip orphan detection")
        return

    # Парсим все .md и строим карту knowledge_id → frontmatter
    entries: dict[str, dict] = {}
    for path in md_paths:
        try:
            entry = store._parse_file(path)
            fm = entry.frontmatter
            kid = fm.knowledge_id
            entries[kid] = {
                "parent_knowledge_id": getattr(fm, "parent_knowledge_id", None),
                "content_type": getattr(fm, "content_type", None),
                "children": getattr(fm, "children", None),
            }
        except Exception:
            pass

    # Проверка 1: child без parent
    for kid, info in entries.items():
        parent_id = info.get("parent_knowledge_id")
        if parent_id and parent_id not in entries:
            logger.warning("RECONCILE: orphan child %s (parent %s not found)", kid, parent_id)
            try:
                await create_issue_async(
                    "orphaned",
                    kid,
                    "warn",
                    f"Parent '{parent_id}' not found — child is orphaned",
                )
            except Exception as e:
                logger.debug("RECONCILE: failed to create orphan issue for %s: %s", kid, e)
            result.orphaned_detected += 1

    # Проверка 2: collection с incomplete children
    for kid, info in entries.items():
        if info.get("content_type") != "collection":
            continue
        children = info.get("children")
        if not children:
            continue
        for child_ref in children:
            if isinstance(child_ref, dict):
                child_id = child_ref.get("knowledge_id")
                if child_id and child_id not in entries:
                    logger.warning(
                        "RECONCILE: collection %s has missing child %s", kid, child_id
                    )
                    try:
                        await create_issue_async(
                            "orphaned",
                            kid,
                            "warn",
                            f"Collection has missing child: {child_id}",
                        )
                    except Exception as e:
                        logger.debug(
                            "RECONCILE: failed to create orphan issue for %s: %s", kid, e
                        )
                    result.orphaned_detected += 1

    if result.orphaned_detected > 0:
        logger.info("RECONCILE: %d parent-child orphan issues detected", result.orphaned_detected)
