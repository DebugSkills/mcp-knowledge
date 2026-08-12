#!/usr/bin/env python3
# ruff: noqa: B023, BLE001
"""Заполнить sequence_number в Qdrant payload для существующих секций книг.

Одноразовая миграция (после Фаз 1-2 schema+pipeline).
Идемпотентен: set_payload перезаписывает то же значение.

CLI:
    python3 scripts/backfill_sequence_payload.py [--dry-run] [--batch-size 500]

Фильтр: parent_knowledge_id EXISTS (все секции, независимо от content_type).
ВСЕ секции попадают: content_type=book/pdf/None у секций, root=collection.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Добавить mcp_server/src в PYTHONPATH
_project_root = Path(__file__).resolve().parents[1]
_src_path = _project_root / "mcp_server" / "src"
sys.path.insert(0, str(_src_path))

from mcp_server.config import settings
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.storage.schema import COLLECTION_ALIAS

logger = logging.getLogger("backfill_sequence_payload")

# Qdrant client
from qdrant_client import QdrantClient

BATCH_SIZE_DEFAULT = 500


async def _backfill(
    qdrant: QdrantClient,
    store: MarkdownStore,
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE_DEFAULT,
) -> dict:
    """Основная логика backfill: scroll → store.read → set_payload."""
    stats = {
        "scrolled": 0,
        "skipped_no_entry": 0,
        "skipped_no_seq": 0,
        "updated": 0,
        "failed": 0,
        "elapsed_sec": 0.0,
    }

    t_start = time.monotonic()
    seen: set[str] = set()  # dedupe по knowledge_id
    batch: list[tuple[str, int]] = []  # (point_id, sequence_number) — point.id = UUID точки

    # Scroll: все точки с parent_knowledge_id EXISTS
    offset = None
    scroll_limit = 1000

    # NH-iter3-6: client-side фильтр parent_knowledge_id is not None
    # (Qdrant не поддерживает "EXISTS" напрямую в scroll-filter).
    loop = asyncio.get_running_loop()

    while True:
        def _scroll() -> tuple:
            return qdrant.scroll(
                collection_name=COLLECTION_ALIAS,
                limit=scroll_limit,
                offset=offset,
                with_payload=["knowledge_id", "parent_knowledge_id"],
                with_vectors=False,
            )

        points, next_offset = await loop.run_in_executor(None, _scroll)
        if not points:
            break

        for point in points:
            payload = point.payload or {}
            parent_id = payload.get("parent_knowledge_id")
            if not parent_id:
                continue  # root/standalone — пропускаем

            kid = payload.get("knowledge_id", "")
            if not kid or kid in seen:
                continue
            seen.add(kid)

            stats["scrolled"] += 1

            # Читаем sequence_number из SSOT
            try:
                entry = await store.read(kid)
            except Exception as exc:
                logger.warning("skip: store.read failed for %s: %s", kid, exc)
                stats["skipped_no_entry"] += 1
                continue

            if entry is None:
                logger.warning("skip: entry not found for %s (orphaned in Qdrant?)", kid)
                stats["skipped_no_entry"] += 1
                continue

            seq = getattr(entry.frontmatter, "sequence_number", None)
            if seq is None:
                logger.warning("skip: no sequence_number in frontmatter for %s", kid)
                stats["skipped_no_seq"] += 1
                continue

            # ВАЖНО: set_payload принимает point.id (UUID), НЕ knowledge_id
            if not dry_run:
                batch.append((str(point.id), seq))

            if len(batch) >= batch_size:
                if not dry_run and batch:
                    await _flush_batch(qdrant, batch, stats, loop)
                batch.clear()

        if next_offset is None:
            break
        offset = next_offset

    # Flush оставшегося батча
    if not dry_run and batch:
        await _flush_batch(qdrant, batch, stats, loop)

    stats["elapsed_sec"] = round(time.monotonic() - t_start, 2)
    return stats


async def _flush_batch(
    qdrant: QdrantClient,
    batch: list[tuple[str, int]],
    stats: dict,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Применить set_payload для батча (per-point: у каждой секции свой sequence)."""
    try:
        def _set_payload() -> None:
            for point_id, seq in batch:
                qdrant.set_payload(
                    collection_name=COLLECTION_ALIAS,
                    payload={"sequence_number": seq},
                    points=[point_id],
                )

        await loop.run_in_executor(None, _set_payload)
        stats["updated"] += len(batch)
        logger.info("backfill batch: %d points updated", len(batch))
    except Exception as exc:
        logger.error("backfill batch failed: %s", exc)
        stats["failed"] += len(batch)


def _print_report(stats: dict, dry_run: bool) -> None:
    """Вывести отчёт о backfill."""
    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"\n{'='*60}")
    print(f"  Backfill sequence_number → Qdrant payload  [{mode}]")
    print(f"{'='*60}")
    print(f"  Scrolled (sections):          {stats['scrolled']:>6}")
    print(f"  Skipped (store.read failed):  {stats['skipped_no_entry']:>6}")
    print(f"  Skipped (no sequence_number): {stats['skipped_no_seq']:>6}")
    if not dry_run:
        print(f"  Updated (set_payload):        {stats['updated']:>6}")
        print(f"  Failed (batch error):         {stats['failed']:>6}")
    print(f"  Elapsed:                      {stats['elapsed_sec']:.2f}s")
    print(f"{'='*60}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill sequence_number in Qdrant payload for book sections"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff without calling set_payload",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE_DEFAULT,
        help=f"Batch size for set_payload (default: {BATCH_SIZE_DEFAULT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    qdrant_url = settings.QDRANT_URL
    logger.info("connecting to Qdrant: %s", qdrant_url)
    qdrant = QdrantClient(url=qdrant_url, prefer_grpc=False, timeout=60)

    # MarkdownStore — read-only (initialize/close не существуют: __init__ делает всё)
    knowledge_dir = settings.KNOWLEDGE_DIR
    store = MarkdownStore(knowledge_dir)

    stats = await _backfill(qdrant, store, dry_run=args.dry_run, batch_size=args.batch_size)
    _print_report(stats, args.dry_run)

    qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
