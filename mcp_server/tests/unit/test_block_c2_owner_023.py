"""code-2026-09-25-023 Block C-2: потребители маркера владельца в контуре 022.

Покрытие (§7.9 Block C (в)-(г), §7.10 C-commit-2, §7.12/§7.13):
- L4: ``_has_live_heavy_task`` при живом ``reconcile_task`` → True.
      RED-мутация: убрать ``reconcile_task`` из проверяемого набора →
      L4 красный (мутация, не тест — проверяется через мутационный прогон).
- L8: waiting-scan — задача ждёт лок, владелец ``reconcile`` → recovery
      НЕ восстанавливает: лок не заменён, нет ``stalled``-записи,
      ``run_quality_scan`` возвращает ``already_running`` + ``lock_holder``.
      RED-мутация: убрать owner-guard → лок заменён → FAIL.
      + уже existing 022-тесты не ломаются (харнесс owner="scan").
- L9: cancel при отсутствии скана → ``cancelled=False``.
      RED-мутация: вернуть безусловный ``cancelled=True`` → FAIL.

Харнесс переиспользуется из test_stale_running_022.py (P2-A: дефолт
``heavy_lock_owner="scan"``). L8/L9 переопределяют owner на "reconcile"/None.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp_server.progress import ImportProgressTracker
from mcp_server.quality.audit import set_store_dir as audit_set_dir
from mcp_server.quality.issues import set_store_dir
from mcp_server.tools.quality import (
    _has_live_heavy_task,
    _recover_stalled_scan,
    _scan_stall_state,
    cancel_quality_scan,
    run_quality_scan,
)


# ── Fixtures (переиспользуются из 022, но локально — без импорта) ──


@pytest.fixture
def quality_tempdir():
    """Temp directory для quality issues + audit store."""
    with tempfile.TemporaryDirectory() as tmp:
        set_store_dir(tmp)
        audit_set_dir(tmp)
        yield tmp


def _make_app_state(
    *,
    scan_lock: asyncio.Lock | None = None,
    scan_id: str | None = None,
    stall_seconds: int = 600,
    owner: str | None = "scan",
) -> SimpleNamespace:
    """Собрать app_state (аналог _make_app_state из test_stale_running_022).

    P2-A: дефолт owner="scan" — без него краснеют 7 тестов 022.
    L8/L9 переопределяют owner через параметр.
    """
    if scan_lock is None:
        scan_lock = asyncio.Lock()
    tracker = ImportProgressTracker(persist_path=None)
    state = SimpleNamespace(
        qdrant=MagicMock(),
        store=MagicMock(),
        settings=SimpleNamespace(
            KNOWLEDGE_DIR=Path("/tmp/test-knowledge-023-c2"),
            SCAN_STALL_SECONDS=stall_seconds,
            AUTO_DEDUP_ENABLED=False,
        ),
        heavy_ops_lock=scan_lock,
        scan_lock=scan_lock,
        heavy_lock_owner=owner,
        scan_progress=tracker,
        scan_id=scan_id,
        scan_task=None,
        scan_cancel_event=None,
        scan_generation=0,
        embedder=None,
        import_task=None,
        convert_task=None,
        reconcile_task=None,
    )
    return state


def _backdate_progress(tracker: ImportProgressTracker, scan_id: str, seconds_ago: float) -> None:
    """Бэкдейт updated_at записи на N секунд назад (паттерн 020)."""
    entry = tracker._data.get(scan_id)
    if entry is None:
        return
    old = datetime.fromisoformat(entry["updated_at"])
    entry["updated_at"] = (old - timedelta(seconds=seconds_ago)).isoformat()


async def _acquire_lock(lock: asyncio.Lock) -> None:
    """Залочить lock (имитация активного _bg_scan, держащего lock)."""
    await lock.acquire()


# ═══════════════════════════════════════════════════════════════
# L4: _has_live_heavy_task при живом reconcile_task → True
# ═══════════════════════════════════════════════════════════════


class TestL4ReconcileTaskInHeavySet:
    """L4: ``_has_live_heavy_task`` проверяет ``reconcile_task`` (§7.12 L4).

    Без ``reconcile_task`` в наборе orphan-ветка может решить, что
    «тяжёлых задач нет», пока идёт full-reconcile (держит heavy_ops_lock).
    """

    @pytest.mark.asyncio
    async def test_reconcile_task_alive_detected(self):
        """Живой reconcile_task → _has_live_heavy_task True (даже без import/convert)."""
        async def _long():
            await asyncio.sleep(100)

        task = asyncio.ensure_future(_long())
        try:
            state = SimpleNamespace(
                import_task=None, convert_task=None, reconcile_task=task,
            )
            assert _has_live_heavy_task(state) is True
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_reconcile_task_done_not_detected(self):
        """Завершённый reconcile_task → False (не живой)."""
        async def _quick():
            pass

        task = asyncio.ensure_future(_quick())
        await task  # завершить
        state = SimpleNamespace(
            import_task=None, convert_task=None, reconcile_task=task,
        )
        assert _has_live_heavy_task(state) is False

    @pytest.mark.asyncio
    async def test_reconcile_task_none_not_detected(self):
        """reconcile_task=None → False."""
        state = SimpleNamespace(
            import_task=None, convert_task=None, reconcile_task=None,
        )
        assert _has_live_heavy_task(state) is False


# ═══════════════════════════════════════════════════════════════
# L8: waiting-scan — owner="reconcile" → recovery НЕ восстанавливает
# ═══════════════════════════════════════════════════════════════


class TestL8WaitingScanOwnerReconcile:
    """L8 (P2-B/P2-C): waiting-scan + owner="reconcile" → diagnostic-only.

    Сценарий: скан создал running-запись, но _bg_scan ждёт лок
    (reconcile удерживает heavy_ops_lock). Через >SCAN_STALL_SECONDS
    детектор видит: lock.locked ✓ + running ✓ + age ✓ + task_alive ✓.
    Но owner="reconcile" → guard в верху _recover_stalled_scan обрывает
    recovery: лок НЕ заменён, stall() НЕ вызван, audit scan_stalled НЕ
    написан, scan_task НЕ отменён.

    run_quality_scan возвращает already_running + lock_holder="reconcile"
    БЕЗ дубль-скана (P2-C).

    RED-мутация: убрать owner-guard → recovery заменяет лок → FAIL
    (лок заменён, stall-запись есть, scan_id сброшен).
    """

    @pytest.mark.asyncio
    async def test_waiting_scan_no_recovery(self, quality_tempdir):
        """owner="reconcile" → recovery diagnostic-only: лок не заменён, нет stall."""
        state = _make_app_state(scan_id="waiting-8", owner="reconcile")
        tracker = state.scan_progress
        tracker.start("waiting-8", total=10)
        _backdate_progress(tracker, "waiting-8", seconds_ago=700)

        # Лок удержан (reconcile), scan_task жив (ждёт лок)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task
        original_lock = state.scan_lock

        # Детектор: stalled=True (age>threshold, task жив)
        st = await _scan_stall_state(state)
        assert st["stalled"] is True
        assert st["orphan"] is False  # task жив → не orphan

        # Recovery: guard owner!="scan" → diagnostic-only
        result = await _recover_stalled_scan(state, st)
        assert result["recovered"] is False
        assert "reconcile" in result["diagnostic"]
        assert result["lock_holder"] == "reconcile"

        # Лок НЕ заменён (тот же объект, всё ещё залочен)
        assert state.scan_lock is original_lock
        assert state.scan_lock.locked() is True

        # stall() НЕ вызван — запись НЕ error+stalled
        entry = tracker._data.get("waiting-8")
        if entry is not None:
            assert entry["status"] != "error"
            assert entry.get("stalled") is not True

        # audit scan_stalled НЕ написан (файл может отсутствовать — diagnostic-only
        # не пишет ничего; если есть — не содержит scan_stalled)
        from mcp_server.quality.audit import get_audit_store_path
        audit_path = get_audit_store_path()
        if audit_path.exists():
            audit_text = audit_path.read_text(encoding="utf-8")
            assert "scan_stalled" not in audit_text

        # scan_id НЕ сброшен (recovery не было)
        assert state.scan_id == "waiting-8"

        # scan_task НЕ отменён (ещё живой)
        assert not fake_task.done()

        # cleanup
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()

    @pytest.mark.asyncio
    async def test_waiting_scan_run_quality_scan_already_running(self, quality_tempdir):
        """run_quality_scan при owner="reconcile" → already_running + lock_holder, без started."""
        state = _make_app_state(scan_id="waiting-8b", owner="reconcile")
        tracker = state.scan_progress
        tracker.start("waiting-8b", total=10)
        _backdate_progress(tracker, "waiting-8b", seconds_ago=700)

        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task
        original_lock = state.scan_lock

        result = await run_quality_scan({}, state)

        # P2-C: already_running (не started), lock_holder="reconcile"
        assert result["status"] == "already_running"
        assert result["scanned"] is False
        assert result["lock_holder"] == "reconcile"
        assert result["stalled"] is True

        # Лок НЕ заменён
        assert state.scan_lock is original_lock
        assert state.scan_lock.locked() is True

        # cleanup
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()

    @pytest.mark.asyncio
    async def test_waiting_scan_orphan_owner_reconcile(self, quality_tempdir):
        """L8 orphan-вариант: owner="reconcile" + orphan (task=None) → diagnostic-only.

        Guard в верху срабатывает ДО orphan-проверки _has_live_heavy_task:
        даже если reconcile_task=None (orphan), owner!="scan" обрывает recovery.
        """
        state = _make_app_state(scan_id="orphan-8c", owner="reconcile")
        tracker = state.scan_progress
        tracker.start("orphan-8c", total=10)
        _backdate_progress(tracker, "orphan-8c", seconds_ago=700)

        await _acquire_lock(state.scan_lock)
        state.scan_task = None  # orphan
        original_lock = state.scan_lock

        st = await _scan_stall_state(state)
        assert st["stalled"] is True
        assert st["orphan"] is True

        result = await _recover_stalled_scan(state, st)
        assert result["recovered"] is False
        assert result["lock_holder"] == "reconcile"

        # Лок НЕ заменён
        assert state.scan_lock is original_lock
        assert state.scan_lock.locked() is True

        # scan_id НЕ сброшен
        assert state.scan_id == "orphan-8c"

        # cleanup
        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# L9: cancel при отсутствии скана → cancelled=False
# ═══════════════════════════════════════════════════════════════


class TestL9HonestCancel:
    """L9 (P2-4): честный cancel_quality_scan — не врёт cancelled=True.

    Сценарии:
    - лок свободен → no_scan, cancelled=False.
    - лок занят, owner="reconcile" → no_scan, cancelled=False, lock_holder.
    - лок занят, owner="scan" → cancelled=True (нормальный скан).
    """

    @pytest.mark.asyncio
    async def test_cancel_no_lock(self, quality_tempdir):
        """Лок свободен → cancelled=False, reason='no active scan'."""
        state = _make_app_state(scan_id=None, owner=None)

        result = await cancel_quality_scan({}, state)
        assert result["cancelled"] is False
        assert result["status"] == "no_scan"
        assert "no active scan" in result["reason"]

    @pytest.mark.asyncio
    async def test_cancel_owner_reconcile(self, quality_tempdir):
        """Лок занят, owner="reconcile" → cancelled=False, lock_holder."""
        state = _make_app_state(scan_id="r-9", owner="reconcile")
        await _acquire_lock(state.scan_lock)

        result = await cancel_quality_scan({}, state)
        assert result["cancelled"] is False
        assert result["status"] == "no_scan"
        assert result["lock_holder"] == "reconcile"
        assert "heavy op" in result["reason"]

        state.scan_lock.release()

    @pytest.mark.asyncio
    async def test_cancel_owner_scan_proceeds(self, quality_tempdir):
        """Лок занят, owner="scan" → cancelled=True (нормальный скан)."""
        state = _make_app_state(scan_id="active-9", owner="scan")
        await _acquire_lock(state.scan_lock)
        state.scan_cancel_event = asyncio.Event()

        result = await cancel_quality_scan({}, state)
        assert result["cancelled"] is True
        assert result["status"] == "cancelled"
        assert state.scan_cancel_event.is_set()

        state.scan_lock.release()

    @pytest.mark.asyncio
    async def test_cancel_owner_none_locked(self, quality_tempdir):
        """Лок занят, owner=None (edge: lock есть, owner не установлен) → cancelled=True.

        owner=None трактуется как «не чужой» — cancel proceeds (backward-compat:
        сценарий, где owner-инфраструктура ещё не выставила маркер, но скан
        действительно активен).
        """
        state = _make_app_state(scan_id="active-9b", owner=None)
        await _acquire_lock(state.scan_lock)
        state.scan_cancel_event = asyncio.Event()

        result = await cancel_quality_scan({}, state)
        assert result["cancelled"] is True
        assert result["status"] == "cancelled"

        state.scan_lock.release()
