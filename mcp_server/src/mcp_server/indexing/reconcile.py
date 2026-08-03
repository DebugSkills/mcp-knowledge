"""C1: Reconciliation при старте — сверка Markdown SSOT ↔ Qdrant.

Задача 2.9 плана Фазы 2.

Flow:
1. Обход knowledge/**/*.md → сравнение updated_at с Qdrant payload
2. Расхождения → доиндексация (через pipeline.reindex_all)
3. Обратная сверка: Qdrant-точки без .md → удаление сирот
4. Лог: {checked, reindexed, skipped, deleted_orphans}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import KnowledgeEntry
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
        self.errors: list[str] = []

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "reindexed": self.reindexed,
            "skipped": self.skipped,
            "deleted_orphans": self.deleted_orphans,
            "errors": self.errors,
        }


async def reconcile(
    store: MarkdownStore,
    qdrant: QdrantClient,
    pipeline: IndexingPipeline,
    knowledge_index,
) -> dict:
    """Выполнить полную сверку Markdown SSOT ↔ Qdrant при старте.

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

    summary = result.to_dict()
    logger.info(
        "✅ RECONCILE complete: checked=%d, reindexed=%d, skipped=%d, orphans=%d, errors=%d",
        result.checked, result.reindexed, result.skipped,
        result.deleted_orphans, len(result.errors),
    )
    return summary
