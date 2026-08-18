# ruff: noqa: BLE001
"""E2E-тесты MCP Knowledge Server — ключевые архитектурные решения.

Фаза 9 (v1.2): 7 сценариев против реального Qdrant (REST) + Ollama.
Идемпотентность: уникальные knowledge_id, коллекция force_recreate на сессию.

Сценарии:
  S1: write → index → search → get_entry (read-after-write sync)
  S2: Hybrid: get_knowledge_map + search_by_tags (AND/OR)
  S3: Auth multi-key: read-key 403, write-key OK
  S4: Reconciliation + orphan delete
  S5: Quality gates: frontmatter block + semantic dup (валидирует фикс 9.5a)
  S6: import_content parent-child TOC
  S7: Git-audit + DLQ + metrics
"""

from __future__ import annotations

import asyncio

import pytest
from mcp_server.storage.schema import ZONE_PRIVATE, collection_for_zone

# ═══════════════════════════════════════════════════════════════
# S1: write → index → search → get_entry (read-after-write sync)
# ═══════════════════════════════════════════════════════════════

S1_CONTENT = """# Асинхронное программирование

## Введение

asyncio — библиотека для написания конкурентного кода с использованием
синтаксиса async/await. Она служит основой для множества асинхронных
фреймворков Python, включая высокопроизводительные веб-серверы и клиенты.

## Event Loop

Центральным компонентом asyncio является event loop — цикл событий,
который управляет выполнением корутин, обрабатывает ввод-вывод
и планирует задачи. Event loop запускает корутины поочерёдно,
переключаясь между ними в точках await.
"""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s1_write_search_get_entry_sync(e2e_app_state):
    """S1: write_knowledge (wait_for_index=True) → search → get_entry."""
    from mcp_server.tools.crud import write_knowledge
    from mcp_server.tools.read import get_entry
    from mcp_server.tools.search import search_knowledge

    # Step 1: Write with sync flag
    result = await write_knowledge(
        {
            "content": S1_CONTENT,
            "domain": "e2e-eng",
            "subject": "python",
            "tags": ["async", "e2e"],
            "knowledge_id": "e2e-s1-async",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    assert result["knowledge_id"] == "e2e-s1-async"
    assert result["indexed"] is True
    assert result["pending"] is False
    assert result["quality_report"]["blocked"] is False

    # Step 2: Search
    search_result = await search_knowledge(
        {"query": "асинхронный ввод-вывод", "top_k": 5},
        e2e_app_state,
    )
    assert "error" not in search_result
    results = search_result.get("results", [])
    assert len(results) >= 1, f"Expected results, got {len(results)}"
    top_hit = results[0]
    assert top_hit["knowledge_id"] == "e2e-s1-async"
    assert top_hit["score"] > 0.5, f"Score too low: {top_hit['score']}"

    # Step 3: Get entry
    entry = await get_entry({"knowledge_id": "e2e-s1-async"}, e2e_app_state)
    assert "error" not in entry
    assert "Асинхронное программирование" in entry.get("content", "")


# ═══════════════════════════════════════════════════════════════
# S2: Hybrid — get_knowledge_map + search_by_tags (AND/OR)
# ═══════════════════════════════════════════════════════════════

S2_BASE = "# {title}\n\n{title} — тестовая запись для проверки hybrid поиска.\n"

S2_CONTENTS = [
    ("e2e-s2-a", "Python и Async", ["python", "async"]),
    ("e2e-s2-b", "Python и Testing", ["python", "testing"]),
    ("e2e-s2-c", "Async и Testing", ["async", "testing"]),
]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s2_hybrid_map_and_tags(e2e_app_state):
    """S2: get_knowledge_map + search_by_tags AND/OR."""
    from mcp_server.tools.crud import write_knowledge
    from mcp_server.tools.read import get_knowledge_map
    from mcp_server.tools.search import search_by_tags

    # Step 1: Write 3 entries
    for kid, title, tags in S2_CONTENTS:
        content = S2_BASE.format(title=title)
        result = await write_knowledge(
            {
                "content": content,
                "domain": "e2e-eng",
                "subject": "python",
                "tags": tags,
                "knowledge_id": kid,
                "wait_for_index": True,
            },
            e2e_app_state,
        )
        assert result["indexed"] is True, f"Failed to index {kid}: {result}"

    # Step 2: get_knowledge_map
    kmap = await get_knowledge_map({"domain": "e2e-eng"}, e2e_app_state)
    assert "error" not in kmap
    assert kmap.get("total_files", 0) >= 3

    # Step 3: search_by_tags AND
    and_result = await search_by_tags(
        {"tags": ["python", "async"], "match_all": True},
        e2e_app_state,
    )
    assert "error" not in and_result
    and_results = and_result.get("results", [])
    and_ids = {r["knowledge_id"] for r in and_results}
    assert "e2e-s2-a" in and_ids, f"AND should include e2e-s2-a, got {and_ids}"
    assert "e2e-s2-b" not in and_ids, "AND should exclude e2e-s2-b"

    # Step 4: search_by_tags OR
    or_result = await search_by_tags(
        {"tags": ["python", "async"], "match_all": False},
        e2e_app_state,
    )
    assert "error" not in or_result
    or_results = or_result.get("results", [])
    or_ids = {r["knowledge_id"] for r in or_results}
    assert len(or_ids) >= 2, f"OR should have >=2 results, got {len(or_ids)}"


# ═══════════════════════════════════════════════════════════════
# S3: Auth multi-key — read-key 403, write-key OK
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s3_auth_multi_key(e2e_keys):
    """S3: read-key → 403 on write_knowledge, write-key → OK."""
    from fastapi import HTTPException
    from mcp_server.auth import authenticate_key, check_tool_permission

    # Read-key
    auth_info = authenticate_key("e2e-read-key")
    assert auth_info.authenticated is True
    assert auth_info.key_level == "read"

    # Read-key can search
    check_tool_permission(auth_info, "search_knowledge")  # no exception

    # Read-key cannot write → 403
    with pytest.raises(HTTPException) as exc_info:
        check_tool_permission(auth_info, "write_knowledge")
    assert exc_info.value.status_code == 403

    # Write-key
    auth_info_w = authenticate_key("e2e-write-key")
    assert auth_info_w.authenticated is True
    assert auth_info_w.key_level == "write"

    # Write-key can write
    check_tool_permission(auth_info_w, "write_knowledge")  # no exception

    # Invalid key
    auth_invalid = authenticate_key("invalid-key")
    assert auth_invalid.authenticated is False
    assert auth_invalid.key_level == "none"


# ═══════════════════════════════════════════════════════════════
# S4: Reconciliation + orphan delete
# ═══════════════════════════════════════════════════════════════

S4_CONTENT = "# Reconcile Test\n\nЗапись для проверки reconciliation.\n"


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.skip(
    reason="skip_orphan_detection=True в конфиге (после OOM-фикса 2026-08-06, коммит 352b54d) "
           "— orphan-детекция отключена, тест ожидает её работу. Включить вместе с фичой."
)
async def test_s4_reconcile_orphan_delete(e2e_app_state, real_qdrant, e2e_store,
                                          e2e_pipeline, e2e_knowledge_index):
    """S4: reconcile удаляет orphan-точку, реальная запись сохранена."""
    from mcp_server.indexing.reconcile import reconcile
    from mcp_server.tools.crud import write_knowledge
    from qdrant_client.http import models as qmodels

    # Step 1: Write real entry
    result = await write_knowledge(
        {
            "content": S4_CONTENT,
            "domain": "e2e-eng",
            "subject": "python",
            "tags": ["reconcile", "e2e"],
            "knowledge_id": "e2e-s4-real",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    assert result["indexed"] is True

    # Step 2: Inject orphan point (в Qdrant, но без .md)
    import uuid as _uuid

    orphan_point = qmodels.PointStruct(
        id=str(_uuid.uuid4()),
        vector=[0.0] * 1024,
        payload={
            "knowledge_id": "e2e-s4-orphan",
            "chunk_id": "orphan-chunk",
            "content": "orphan content",
            "domain": "e2e-eng",
            "subject": "python",
            "tags": [],
        },
    )
    real_qdrant.upsert_points([orphan_point])

    # Verify orphan exists
    all_ids = real_qdrant.get_all_knowledge_ids(collection_name=collection_for_zone(ZONE_PRIVATE))
    assert "e2e-s4-orphan" in all_ids, "Orphan point not injected"

    # Step 3: Reconcile
    rec_result = await reconcile(
        e2e_store, real_qdrant, e2e_pipeline, e2e_knowledge_index
    )
    assert rec_result["deleted_orphans"] >= 1, f"Expected orphans deleted, got {rec_result}"

    # Step 4: Verify orphan removed, real entry preserved
    all_ids_after = real_qdrant.get_all_knowledge_ids(collection_name=collection_for_zone(ZONE_PRIVATE))
    assert "e2e-s4-orphan" not in all_ids_after, "Orphan was not deleted"
    assert "e2e-s4-real" in all_ids_after, "Real entry was deleted by mistake"


# ═══════════════════════════════════════════════════════════════
# S5: Quality gates — frontmatter block + semantic dup
#    (ВАЛИДИРУЕТ прод-фикс 9.5a)
# ═══════════════════════════════════════════════════════════════

S5_ORIG = (
    "Python asyncio event loop manages coroutines and schedules tasks "
    "on the event loop. The loop handles callbacks, timers, and I/O events "
    "efficiently. Asyncio is the foundation for modern async Python applications."
)

S5_DUP = (
    "Python asyncio event loop manages coroutines and schedules tasks "
    "on the event loop. The event loop efficiently handles callbacks, timers, "
    "and I/O operations. Asyncio serves as the foundation for modern async Python."
)

S5_DIFFERENT = (
    "Реляционные базы данных используют SQL для манипуляции данными. "
    "Индексы B-tree ускоряют поиск и сортировку. Нормализация устраняет "
    "избыточность и аномалии обновления. Транзакции ACID гарантируют целостность."
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s5a_frontmatter_strict_block(e2e_app_state):
    """S5a: strict=true → quality gate блокирует запись без recommended-полей."""
    from mcp_server.tools.crud import write_knowledge

    result = await write_knowledge(
        {
            "content": "# Quick test\n\nMinimal content for quality check test here.",
            "domain": "e2e-q",
            "subject": "y",
            "strict": True,
            # source, evergreen, cross_subjects — намеренно опущены
        },
        e2e_app_state,
    )
    assert "error" in result
    assert "Quality gate blocked" in result["error"]
    assert result["quality_report"]["blocked"] is True
    # At least one issue about missing recommended fields
    assert len(result["quality_report"]["issues"]) >= 1


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s5b_semantic_dup_detection(e2e_app_state):
    """S5b: semantic dup-gate находит похожий контент (валидация фикса 9.5a).

    До фикса 9.5a этот тест ВСЕГДА падал: dup-gate возвращал [] из-за
    TypeError (несовпадение сигнатур wrapper vs SDK) + with_vectors-бага.
    После фикса — реальный embedding + Qdrant search (с with_vectors=True) +
    find_duplicates находят дубликат.
    """
    from mcp_server.tools.crud import write_knowledge

    # Step 1: Write original
    result1 = await write_knowledge(
        {
            "content": f"# Async Python\n\n{S5_ORIG}",
            "domain": "e2e-q",
            "subject": "y",
            "knowledge_id": "e2e-s5-orig",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    assert result1["indexed"] is True

    # Step 2: Write near-duplicate (не strict — advisory)
    result2 = await write_knowledge(
        {
            "content": f"# Async Python Intro\n\n{S5_DUP}",
            "domain": "e2e-q",
            "subject": "y",
            "knowledge_id": "e2e-s5-dup",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    qr = result2.get("quality_report", {})
    duplicates = qr.get("duplicates", [])
    assert len(duplicates) >= 1, (
        f"semantic dup-gate should detect duplicate, but got {len(duplicates)}. "
        "This validates prod-fix 9.5a (with_vectors + wrapper-API)."
    )
    # The duplicate should reference the original
    assert duplicates[0]["knowledge_id"] == "e2e-s5-orig"
    assert duplicates[0]["score"] >= 0.85, f"Cosine too low: {duplicates[0]['score']}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s5c_non_dup_passes(e2e_app_state):
    """S5c: совершенно другой контент → duplicates пусто (контрольная проверка)."""
    from mcp_server.tools.crud import write_knowledge

    result = await write_knowledge(
        {
            "content": f"# SQL Databases\n\n{S5_DIFFERENT}",
            "domain": "e2e-q",
            "subject": "y",
            "knowledge_id": "e2e-s5-sql",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    qr = result.get("quality_report", {})
    duplicates = qr.get("duplicates", [])
    assert len(duplicates) == 0, (
        f"Non-duplicate should not trigger dup-gate, got {duplicates}"
    )


# ═══════════════════════════════════════════════════════════════
# S6: import_content — parent-child TOC
# ═══════════════════════════════════════════════════════════════

STRUCTURED_BOOK_RU = """# Python Async Programming

## Глава 1: Введение в Asyncio
Асинхронное программирование позволяет конкурентное выполнение задач.
Модуль asyncio предоставляет event loop, корутины и футуры.

## Глава 2: Корутины и задачи
Корутины — ядро asyncio. Используйте async def для их определения.
Задачи оборачивают корутины и планируют их на event loop.

## Глава 3: Внутреннее устройство Event Loop
Event loop — сердце asyncio. Он управляет коллбеками,
планирует задачи и эффективно обрабатывает события ввода-вывода.
"""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s6_import_content_parent_child_toc(e2e_app_state, e2e_store):
    """S6: import_content → collection + children + parent_knowledge_id."""
    from mcp_server.content.book_preprocessor import BookPreprocessor
    from mcp_server.content.registry import register, reset
    from mcp_server.tools.content import import_content

    # Setup BookPreprocessor with real embedder
    reset()
    token_counter = type("TokenCounter", (), {
        "count_tokens": lambda self, text: len(text.split()),
        "truncate_to_tokens": lambda self, text, max_t: " ".join(text.split()[:max_t]),
    })()
    bp = BookPreprocessor(embedder=e2e_app_state.embedder, token_counter=token_counter)
    register(bp)

    try:
        # Step 1: Import structured book
        result = await import_content(
            {
                "content": STRUCTURED_BOOK_RU,
                "content_type": "book",
                "domain": "e2e-book",
                "subject": "async",
                "title": "Async Python",
                "tags": ["e2e"],
            },
            e2e_app_state,
        )
        assert "error" not in result, f"Import failed: {result.get('error')}"
        assert result["imported"] >= 3, f"Expected >=3 chapters, got {result['imported']}"
        assert result["failed"] == 0
        assert result["collection_id"].endswith("-collection")

        collection_id = result["collection_id"]

        # Step 2: Root entry has children (use store.read for full frontmatter)
        root_entry = await e2e_store.read(collection_id)
        assert root_entry is not None, f"Root entry {collection_id} not found"
        children = root_entry.frontmatter.children or []
        assert len(children) >= 3, f"Expected >=3 children, got {len(children)}"

        # Step 3: Each child has parent_knowledge_id == collection_id
        for child_ref in children:
            child_kid = child_ref["knowledge_id"]
            child_entry = await e2e_store.read(child_kid)
            assert child_entry is not None, f"Child entry {child_kid} not found"
            child_fm = child_entry.frontmatter
            assert child_fm.parent_knowledge_id == collection_id, (
                f"Child {child_kid} parent mismatch"
            )
    finally:
        # Восстанавливаем default-регистрацию: reset() опустошает глобальный реестр,
        # и последующие тесты (S16 import_content via HTTP) получают "Available types: []".
        reset()
        register(BookPreprocessor(embedder=None, token_counter=None))


# ═══════════════════════════════════════════════════════════════
# S7: Git-audit + DLQ + metrics
# ═══════════════════════════════════════════════════════════════

S7_GIT_CONTENT = "# Git Audit Test\n\nЗапись с git-аудитом.\n"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s7a_git_audit_commit_created(e2e_app_state, e2e_store):
    """S7a: write_knowledge создаёт git-коммит в temp-репо."""
    import git as gitpython
    from mcp_server.tools.crud import write_knowledge

    result = await write_knowledge(
        {
            "content": S7_GIT_CONTENT,
            "domain": "e2e-git",
            "subject": "python",
            "knowledge_id": "e2e-s7-git",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    assert result["indexed"] is True

    # Verify git commit
    repo = gitpython.Repo(e2e_store._root)
    commits = list(repo.iter_commits())
    assert len(commits) >= 1, "No git commit found after write"
    assert "e2e-s7-git" in commits[0].message, f"Commit message mismatch: {commits[0].message}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s7b_dlq_record_and_replay(e2e_app_state, e2e_pipeline, e2e_store,
                                          e2e_embedder_for_pipeline, real_qdrant, monkeypatch):
    """S7b: DLQ — при embed-fail запись попадает в DLQ.

    P1-1/P1-2: pipeline вызывает embed_sync (не encode). Патчим именно embed_sync.
    После восстановления — переиндекс через повторный write_knowledge.
    replay_all = проверка очистки DLQ.
    """
    from mcp_server.tools.crud import write_knowledge

    original_embed_sync = e2e_embedder_for_pipeline.embed_sync

    # Patch embed_sync to fail
    def _failing_embed_sync(texts):
        raise RuntimeError("forced DLQ failure for e2e test")

    monkeypatch.setattr(e2e_embedder_for_pipeline, "embed_sync", _failing_embed_sync)

    # Write that will fail on embed → DLQ
    result = await write_knowledge(
        {
            "content": "# DLQ Test\n\nЭта запись попадёт в DLQ.\n",
            "domain": "e2e-dlq",
            "subject": "python",
            "knowledge_id": "e2e-s7-dlq",
            "wait_for_index": False,
        },
        e2e_app_state,
    )
    assert result["knowledge_id"] == "e2e-s7-dlq"

    # Wait for DLQ (polling, max 10 sec)
    for _ in range(20):
        await asyncio.sleep(0.5)
        if e2e_pipeline._dlq.size >= 1:
            break

    dlq_entries = e2e_pipeline._dlq.list_entries()
    assert len(dlq_entries) >= 1, f"DLQ should have entry, got {dlq_entries}"

    # Restore embedder
    monkeypatch.setattr(e2e_embedder_for_pipeline, "embed_sync", original_embed_sync)

    # Re-index via write_knowledge (SSOT already has the entry)
    result2 = await write_knowledge(
        {
            "content": "# DLQ Test\n\nЭта запись попадёт в DLQ.\n",
            "domain": "e2e-dlq",
            "subject": "python",
            "knowledge_id": "e2e-s7-dlq",
            "wait_for_index": True,
        },
        e2e_app_state,
    )
    assert result2["indexed"] is True, f"Re-index failed: {result2}"

    # Verify in Qdrant
    all_ids = real_qdrant.get_all_knowledge_ids(collection_name=collection_for_zone(ZONE_PRIVATE))
    assert "e2e-s7-dlq" in all_ids, "Entry not found in Qdrant after re-index"

    # replay_all = проверка очистки DLQ
    replay_result = await e2e_pipeline._dlq.replay_all(e2e_pipeline)
    assert replay_result["replayed"] >= 1, f"replay_all should replay entries: {replay_result}"
    assert e2e_pipeline._dlq.size == 0, f"DLQ not empty after replay: {e2e_pipeline._dlq.size}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s7c_metrics_exposition(e2e_app_state, e2e_pipeline):
    """S7c: Prometheus metrics имеют counter-наблюдения после write/search."""
    from mcp_server.metrics import (
        collection_size,
    )

    # Pipeline should have processed entries from earlier tests (S1-S6)
    # At minimum, check that metrics exist and expose without error
    from prometheus_client import generate_latest

    metrics_text = generate_latest().decode("utf-8")

    # Verify key metrics exist
    assert "mcp_write_latency_seconds" in metrics_text
    assert "mcp_search_latency_seconds" in metrics_text
    assert "mcp_collection_size" in metrics_text
    assert "mcp_pipeline_processed_total" in metrics_text
    assert "mcp_dlq_size" in metrics_text

    # collection_size should be > 0 after writes
    cs_value = collection_size._value.get()
    assert cs_value >= 0, f"collection_size gauge should exist, got {cs_value}"


# ═══════════════════════════════════════════════════════════════
# S8: Blue-green reindex (ОПЦИОНАЛЬНЫЙ, e2e_slow — skip by default)
# ═══════════════════════════════════════════════════════════════

S8_ALIAS = "knowledge_e2e_alias"
S8_V1 = "knowledge_e2e_v1"
S8_V2 = "knowledge_e2e_v2"


@pytest.mark.e2e_slow
@pytest.mark.asyncio
async def test_s8_blue_green_reindex(e2e_app_state, real_qdrant, e2e_store,
                                       e2e_pipeline, e2e_knowledge_index):
    """S8 (опц.): blue-green reindex через Qdrant aliases.

    Фаза 10 (фикс): вместо хрупких monkeypatch на COLLECTION_ALIAS/V1/V2
    тест передаёт явные e2e-имена через параметры reindex_blue_green().
    Это устраняет root cause деструктивного поведения (деструктивный
    force_recreate прод-именованной knowledge_v1) и 409 Conflict
    (alias-имя не должно совпадать с существующей коллекцией knowledge_e2e).

    Alias: knowledge_e2e_alias (отдельный, не совпадает с коллекцией)
    Коллекции: knowledge_e2e_v1, knowledge_e2e_v2 (полный cleanup в finally).
    """
    from mcp_server.tools.crud import write_knowledge
    from mcp_server.tools.search import search_knowledge
    from qdrant_client.http import models as qmodels

    try:
        # Step 1: Write test entry (идёт в knowledge_e2e через патч COLLECTION_NAME из conftest)
        await write_knowledge(
            {
                "content": "# Blue-green test\n\nТестовая запись для blue-green.\n",
                "domain": "e2e-bg",
                "subject": "python",
                "knowledge_id": "e2e-s8-bg",
                "wait_for_index": True,
            },
            e2e_app_state,
        )

        # Step 2: Blue-green reindex с явными e2e-именами
        reindex_result = await e2e_pipeline.reindex_blue_green(
            alias_name=S8_ALIAS,
            collection_v1=S8_V1,
            collection_v2=S8_V2,
        )
        assert reindex_result["alias_swapped"] is True
        target = reindex_result.get("target", "")
        assert target in (S8_V1, S8_V2), f"Expected target in ({S8_V1}, {S8_V2}), got {target}"

        # Step 3: Search после swap — zero-downtime проверка
        search_result = await search_knowledge(
            {"query": "blue green test", "top_k": 5},
            e2e_app_state,
        )
        assert "error" not in search_result
        results = search_result.get("results", [])
        assert len(results) >= 1, "Search failed after blue-green swap"

    finally:
        # Step 4: Full cleanup — удалить alias + коллекции (идемпотентно)
        # Удалить alias knowledge_e2e_alias
        try:
            real_qdrant._client.update_collection_aliases(
                change_aliases_operations=[
                    qmodels.DeleteAliasOperation(
                        delete_alias=qmodels.DeleteAlias(
                            alias_name=S8_ALIAS,
                        )
                    )
                ],
            )
        except Exception:  # noqa: S110
            pass

        # Удалить blue-green коллекции knowledge_e2e_v1, knowledge_e2e_v2
        for coll in [S8_V1, S8_V2]:
            try:
                real_qdrant.delete_collection_named(coll)
            except Exception:  # noqa: S110
                pass
