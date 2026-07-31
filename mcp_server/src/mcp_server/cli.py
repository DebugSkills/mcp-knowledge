"""CLI-утилиты для обслуживания MCP Knowledge Server.

- reindex: полный переиндекс из Markdown SSOT
- dlq-replay: возврат задач из DLQ в очередь

Используются из Makefile: `make reindex`, `make dlq-replay`.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_knowledge.cli")


async def reindex():
    """Полный переиндекс Qdrant из Markdown SSOT.

    Использование: python -m mcp_server.cli reindex
    """
    from .config import settings
    from .storage import MarkdownStore, QdrantClient
    from .embedding import EmbeddingManager
    from .indexing import IndexingPipeline, MarkdownChunker

    logger.info("=== REINDEX: полная перестройка Qdrant из Markdown SSOT ===")

    store = MarkdownStore()
    qdrant = QdrantClient()
    qdrant.ensure_collection(force_recreate=False)

    embedder = EmbeddingManager()
    await embedder.initialize()

    chunker = MarkdownChunker()
    pipeline = IndexingPipeline(store, qdrant, embedder, chunker)
    await pipeline.start()

    try:
        result = await pipeline.reindex_all()
        logger.info("REINDEX завершён: %s", result)
        return result
    finally:
        await pipeline.stop()
        qdrant.close()


async def dlq_replay():
    """Возврат задач из DLQ-директории в индекс.

    Читает JSON-файлы из data/dlq/ и пытается повторно проиндексировать.

    Использование: python -m mcp_server.cli dlq-replay
    """
    from .config import settings
    from .storage import MarkdownStore, QdrantClient
    from .embedding import EmbeddingManager
    from .indexing import IndexingPipeline, MarkdownChunker

    dlq_dir = Path(settings.DLQ_DIR)
    if not dlq_dir.exists():
        logger.info("DLQ-директория пуста: %s", dlq_dir)
        return {"replayed": 0}

    dlq_files = list(dlq_dir.glob("*.json"))
    if not dlq_files:
        logger.info("Нет задач в DLQ")
        return {"replayed": 0}

    logger.info("=== DLQ REPLAY: %d задач ===", len(dlq_files))

    store = MarkdownStore()
    qdrant = QdrantClient()
    qdrant.ensure_collection(force_recreate=False)

    embedder = EmbeddingManager()
    await embedder.initialize()

    chunker = MarkdownChunker()
    pipeline = IndexingPipeline(store, qdrant, embedder, chunker)
    await pipeline.start()

    replayed = 0
    failed = 0

    try:
        for dlq_file in dlq_files:
            try:
                data = json.loads(dlq_file.read_text(encoding="utf-8"))
                knowledge_id = data.get("knowledge_id")
                if not knowledge_id:
                    logger.warning("DLQ-файл без knowledge_id: %s", dlq_file)
                    continue

                entry = await store.read(knowledge_id)
                if entry is None:
                    logger.warning("Запись %s не найдена в SSOT — удаляю DLQ", knowledge_id)
                    dlq_file.unlink()
                    continue

                # Повторная индексация
                chunks = chunker.chunk(
                    knowledge_id=entry.frontmatter.knowledge_id,
                    content=entry.content,
                )
                if chunks:
                    texts = [ch.content for ch in chunks]
                    loop = asyncio.get_running_loop()
                    vectors = await loop.run_in_executor(
                        None, embedder.embed_sync, texts
                    )
                    # Собираем Qdrant points
                    from .storage.schema import build_payload_point
                    import uuid
                    fm = entry.frontmatter
                    points = []
                    for ch, vector in zip(chunks, vectors):
                        point = build_payload_point(
                            point_id=str(uuid.uuid4()),
                            vector=vector,
                            knowledge_id=fm.knowledge_id,
                            chunk_id=ch.chunk_id,
                            content=ch.content,
                            domain=fm.domain,
                            subject=fm.subject,
                            project=fm.project,
                            tags=fm.tags,
                            cross_subjects=fm.cross_subjects,
                            section_header=ch.section_header,
                            chunk_index=ch.chunk_index,
                            updated_at=fm.updated_at.isoformat(),
                        )
                        points.append(point)
                    qdrant.upsert_points(points)

                dlq_file.unlink()  # Успешно — удаляем DLQ-файл
                replayed += 1
                logger.info("DLQ replay OK: %s", knowledge_id)

            except Exception as e:
                logger.error("DLQ replay failed for %s: %s", dlq_file, e)
                failed += 1

    finally:
        await pipeline.stop()
        qdrant.close()

    result = {"replayed": replayed, "failed": failed}
    logger.info("DLQ REPLAY завершён: %s", result)
    return result


def main():
    """Точка входа: python -m mcp_server.cli <command>"""
    if len(sys.argv) < 2:
        print("Usage: python -m mcp_server.cli <reindex|dlq-replay>")
        sys.exit(1)

    command = sys.argv[1]
    if command == "reindex":
        asyncio.run(reindex())
    elif command == "dlq-replay":
        asyncio.run(dlq_replay())
    else:
        print(f"Неизвестная команда: {command}")
        print("Доступные команды: reindex, dlq-replay")
        sys.exit(1)


if __name__ == "__main__":
    main()
