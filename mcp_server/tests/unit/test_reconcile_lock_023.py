"""code-2026-09-25-023 Block C-1a: heavy_ops_lock для full-reindex — L1/L2/L3/L7/L10.

Протокол §7.9(а): bounded wait + identity-check (P1-3) + compare-and-clear owner.
Харнесс переиспользуется из test_reconcile_incremental.py (FakeStore/FakeQdrant/
_make_entry/_make_mock_pipeline/_make_knowledge_index — P3-6).

Покрытие:
- L1: full-ветка при свободном локе → reindex_all вызван ПОД локом.
- L2: full-ветка, лок занят дольше таймаута → deferred, reindex_all НЕ вызван.
- L3: incremental (≤K) при занятом локе → index_missing без ожидания лока.
- L7: owner=="reconcile" во время full-reconcile; после — None (compare-and-clear).
- L10: phantom-lock waiter (P1-3) — лок заменён во время ожидания → identity-check
  ловит расхождение → deferred, reindex_all НЕ вызван, старый лок освобождён.

RED-мутации описаны в каждом тесте (наблюдаемые).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from unit.test_reconcile_incremental import (
    FakeQdrant,
    FakeStore,
    _make_entry,
    _make_knowledge_index,
    _make_mock_pipeline,
)

from mcp_server.indexing.reconcile import reconcile


# ── Helpers ───────────────────────────────────────────────────


def _make_missing(n: int) -> tuple[list[Path], dict[Path, object]]:
    """Создать n missing-записей (каждая отсутствует в Qdrant)."""
    paths: list[Path] = []
    entries: dict[Path, object] = {}
    for i in range(n):
        kid = f"kid-lock-{i:02d}"
        p = Path(f"/tmp/knowledge/test/demo/{kid}.md")
        paths.append(p)
        entries[p] = _make_entry(kid)
    return paths, entries


def _owner_namespace() -> tuple[SimpleNamespace, object, object, object]:
    """Собрать owner-ручки над локальным namespace (без Starlette)."""
    state = SimpleNamespace(heavy_ops_lock=None, heavy_lock_owner=None)

    def get_heavy_lock():
        return state.heavy_ops_lock

    def set_lock_owner(v):
        state.heavy_lock_owner = v

    def get_lock_owner():
        return state.heavy_lock_owner

    return state, get_heavy_lock, set_lock_owner, get_lock_owner


# ── L1: full-ветка при свободном локе ─────────────────────────


async def test_l1_full_free_lock_reindex_under_lock(monkeypatch):
    """L1: full-ветка (>K) при свободном локе → reindex_all вызван ПОД локом.

    RED-мутация: убрать acquire (proceed=True без лока) → spy видит
    ``lock.locked()==False`` в момент вызова → FAIL.
    """
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50
    )

    paths, entries = _make_missing(51)  # >K → full
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    state, get_heavy_lock, set_lock_owner, get_lock_owner = _owner_namespace()
    lock = asyncio.Lock()
    state.heavy_ops_lock = lock

    # Spy: проверяем lock.locked() в момент вызова reindex_all
    lock_locked_during = []
    orig_ra = pipeline.reindex_all

    async def _spy_ra():
        lock_locked_during.append(lock.locked())
        return await orig_ra()

    pipeline.reindex_all = _spy_ra

    result = await reconcile(
        store, qdrant, pipeline, _make_knowledge_index(),
        skip_reindex=False, skip_orphan_detection=True,
        get_heavy_lock=get_heavy_lock,
        set_lock_owner=set_lock_owner,
        get_lock_owner=get_lock_owner,
    )

    assert result["mode"] == "full"
    assert len(lock_locked_during) == 1
    assert lock_locked_during[0] is True  # reindex_all вызван ПОД локом
    assert not lock.locked()  # лок освобождён после
    assert state.heavy_lock_owner is None  # compare-and-clear


# ── L2: full-ветка, лок занят → deferred ──────────────────────


async def test_l2_full_busy_lock_deferred(monkeypatch):
    """L2: full-ветка, лок занят дольше таймаута → deferred, reindex_all НЕ вызван.

    RED-мутация: убрать bounded-wait (вечное ожидание) → harness оборачивает
    reconcile в ``asyncio.wait_for(..., 5s)`` → TimeoutError → FAIL.
    Альтернативная мутация: заменить defer на fail-open → reindex_all вызван
    → FAIL.
    """
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_LOCK_WAIT_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50
    )

    paths, entries = _make_missing(51)  # >K → full
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    state, get_heavy_lock, set_lock_owner, get_lock_owner = _owner_namespace()
    lock = asyncio.Lock()
    state.heavy_ops_lock = lock

    # Занять лок (имитация активного скана)
    await lock.acquire()
    try:
        ra_calls = []
        orig_ra = pipeline.reindex_all

        async def _spy_ra():
            ra_calls.append(1)
            return await orig_ra()

        pipeline.reindex_all = _spy_ra

        # Safety-net: если bounded-wait сломан → зависнет → TimeoutError
        result = await asyncio.wait_for(
            reconcile(
                store, qdrant, pipeline, _make_knowledge_index(),
                skip_reindex=False, skip_orphan_detection=True,
                get_heavy_lock=get_heavy_lock,
                set_lock_owner=set_lock_owner,
                get_lock_owner=get_lock_owner,
            ),
            timeout=5,
        )

        assert result["mode"] == "deferred"
        assert len(ra_calls) == 0  # reindex_all НЕ вызван
        assert result["errors"] == []
        assert state.heavy_lock_owner is None  # owner не ставился
    finally:
        if lock.locked():
            lock.release()


# ── L3: incremental (≤K) при занятом локе ─────────────────────


async def test_l3_incremental_busy_lock_no_wait(monkeypatch):
    """L3: incremental (≤K) при занятом локе → index_missing без ожидания лока.

    RED-мутация: начать брать лок в incremental → timeout → harness
    ``asyncio.wait_for(..., 5s)`` → FAIL.
    """
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_LOCK_WAIT_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50
    )

    paths, entries = _make_missing(1)  # ≤K → incremental
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    state, get_heavy_lock, set_lock_owner, get_lock_owner = _owner_namespace()
    lock = asyncio.Lock()
    state.heavy_ops_lock = lock

    # Занять лок
    await lock.acquire()
    try:
        im_calls = []
        orig_im = pipeline.index_missing

        async def _spy_im(paths_arg):
            im_calls.append(len(paths_arg))
            return await orig_im(paths_arg)

        pipeline.index_missing = _spy_im

        # Safety-net: если incremental начал ждать лок → TimeoutError
        result = await asyncio.wait_for(
            reconcile(
                store, qdrant, pipeline, _make_knowledge_index(),
                skip_reindex=False, skip_orphan_detection=True,
                get_heavy_lock=get_heavy_lock,
                set_lock_owner=set_lock_owner,
                get_lock_owner=get_lock_owner,
            ),
            timeout=5,
        )

        assert result["mode"] == "incremental"
        assert len(im_calls) == 1  # index_missing отработал
        assert result["reindexed"] == 1
    finally:
        if lock.locked():
            lock.release()


# ── L7: owner set/clear during full-reconcile ─────────────────


async def test_l7_owner_set_and_cleared(monkeypatch):
    """L7: во время full-reconcile owner=="reconcile"; после — None.

    RED-мутация: убрать установку/сброс owner → spy видит ``None`` во время
    вызова (вместо "reconcile") и/или ``"reconcile"`` после → FAIL.
    """
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50
    )

    paths, entries = _make_missing(51)  # >K → full
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    state, get_heavy_lock, set_lock_owner, get_lock_owner = _owner_namespace()
    lock = asyncio.Lock()
    state.heavy_ops_lock = lock

    # Spy: проверяем owner + lock.locked() в момент вызова reindex_all
    owner_during = []
    lock_locked_during = []
    orig_ra = pipeline.reindex_all

    async def _spy_ra():
        owner_during.append(state.heavy_lock_owner)
        lock_locked_during.append(lock.locked())
        return await orig_ra()

    pipeline.reindex_all = _spy_ra

    result = await reconcile(
        store, qdrant, pipeline, _make_knowledge_index(),
        skip_reindex=False, skip_orphan_detection=True,
        get_heavy_lock=get_heavy_lock,
        set_lock_owner=set_lock_owner,
        get_lock_owner=get_lock_owner,
    )

    assert result["mode"] == "full"
    assert owner_during == ["reconcile"]  # owner set ПЕРЕД reindex_all
    assert lock_locked_during == [True]  # лок удерживается
    assert state.heavy_lock_owner is None  # compare-and-clear после finally
    assert not lock.locked()  # лок освобождён


# ── L10: phantom-lock waiter (P1-3) ───────────────────────────


async def test_l10_phantom_lock_waiter(monkeypatch):
    """L10 (P1-3): лок заменён во время ожидания → identity-check ловит расхождение.

    Сценарий: reconcile ждёт на lock O (удержан «зависшим сканом»); recovery 022
    заменяет ``state.heavy_ops_lock = N``; O освобождается → waiter резолвится
    на СТАРОМ O → identity-check ``O is not N`` → release O, deferred.

    Синхронизация (P3-в): тест ждёт регистрации waiter'а на O (``_waiters``
    непуст) ДО замены лока — иначе сценарий вырождается.

    RED-мутация: убрать identity-check после acquire → reconcile берёт фантомный
    O и вызывает ``reindex_all`` → spy ловит вызов при ``lock is not
    get_heavy_lock()`` → FAIL.
    """
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    monkeypatch.setattr(
        "mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50
    )

    paths, entries = _make_missing(51)  # >K → full
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    state, get_heavy_lock, set_lock_owner, get_lock_owner = _owner_namespace()

    # Lock O — удержан «зависшим сканом»
    lock_O = asyncio.Lock()
    state.heavy_ops_lock = lock_O
    await lock_O.acquire()

    ra_calls = []
    orig_ra = pipeline.reindex_all

    async def _spy_ra():
        ra_calls.append(1)
        return await orig_ra()

    pipeline.reindex_all = _spy_ra

    # Запустить reconcile как задачу — будет ждать на O
    reconcile_task = asyncio.create_task(reconcile(
        store, qdrant, pipeline, _make_knowledge_index(),
        skip_reindex=False, skip_orphan_detection=True,
        get_heavy_lock=get_heavy_lock,
        set_lock_owner=set_lock_owner,
        get_lock_owner=get_lock_owner,
    ))

    try:
        # P3-в: дождаться регистрации waiter'а на O ДО замены лока
        waiter_registered = False
        for _ in range(200):
            await asyncio.sleep(0)
            if getattr(lock_O, "_waiters", None):
                waiter_registered = True
                break
        assert waiter_registered, "waiter не зарегистрировался на O (флак-риск)"

        # Заменить лок на N (recovery 022)
        lock_N = asyncio.Lock()
        state.heavy_ops_lock = lock_N

        # Освободить O → waiter резолвится на СТАРОМ O
        lock_O.release()

        # Дождаться завершения reconcile
        result = await asyncio.wait_for(reconcile_task, timeout=5)
    except BaseException:
        reconcile_task.cancel()
        raise

    # Asserts
    assert result["mode"] == "deferred"
    assert len(ra_calls) == 0  # reindex_all НЕ вызван
    assert state.heavy_lock_owner != "reconcile"  # owner не ставился
    assert state.heavy_lock_owner is None  # compare-and-clear (None → None)
    # O освобождён (identity-check вызвал release)
    assert not lock_O.locked()
    # N не тронут
    assert not lock_N.locked()
