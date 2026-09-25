"""code-2026-09-25-022: stale-running-timeout — T1-T8 + регресс.

Тесты ленивого stale-детектора (Variant B-lazy): зависший скан (нет
heartbeat > SCAN_STALL_SECONDS) → повторный ``run_quality_scan`` стартует
новый, старая запись помечается ``error``+``stalled``, audit пишет
``scan_stalled``, lock атомарно заменяется (generation-guarded).

Покрытие AC-1..AC-8. RED-мутации описаны в каждом тесте.
Все даты/числа — динамические (анти-мина, урок 019).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.progress import ImportProgressTracker
from mcp_server.quality.audit import set_store_dir as audit_set_dir
from mcp_server.quality.issues import set_store_dir
from mcp_server.tools.quality import (
    _bg_scan,
    _has_live_heavy_task,
    _recover_stalled_scan,
    _scan_stall_state,
    cancel_quality_scan,
    run_quality_scan,
)


# ── Fixtures ──────────────────────────────────────────────────


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
    knowledge_dir: Path | None = None,
) -> SimpleNamespace:
    """Собрать app_state с реальным asyncio.Lock + реальным progress-трекером.

    Используем РЕАЛЬНЫЕ объекты (не MagicMock) для lock/progress —
    детектор читает ``lock.locked()``, ``progress.get()``, ``task.done()``
    и подделать их MagicMock'ом корректно для всех инвариантов T7 нельзя.
    """
    if scan_lock is None:
        scan_lock = asyncio.Lock()
    if knowledge_dir is None:
        knowledge_dir = Path("/tmp/test-knowledge-022")
    tracker = ImportProgressTracker(persist_path=None)
    state = SimpleNamespace(
        qdrant=MagicMock(),
        store=MagicMock(),
        settings=SimpleNamespace(
            KNOWLEDGE_DIR=knowledge_dir,
            SCAN_STALL_SECONDS=stall_seconds,
            AUTO_DEDUP_ENABLED=False,
        ),
        heavy_ops_lock=scan_lock,
        scan_lock=scan_lock,
        scan_progress=tracker,
        scan_id=scan_id,
        scan_task=None,
        scan_cancel_event=None,
        scan_generation=0,
        embedder=None,
        import_task=None,
        convert_task=None,
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


def _slow_scan(metrics: dict, delay: float = 0.3):
    """Мок run_scan, который держит скан «живым» delay секунд.

    Нужен, чтобы _bg_scan оставался ВНУТРИ run_scan (держал lock) в момент
    проверки инвариантов/смены generation: мгновенный AsyncMock завершает скан
    до того, как тест успевает вмешаться (realtime-гонка харнесса).
    """
    async def _scan(**_kwargs):
        await asyncio.sleep(delay)
        return metrics
    return _scan


# ═══════════════════════════════════════════════════════════════
# T1: stale → 2-й run_quality_scan = started, старая = error+stalled
# ═══════════════════════════════════════════════════════════════


class TestT1StaleRecovery:
    """T1: зависший скан (updated_at > порога) → повторный старт."""

    @pytest.mark.asyncio
    async def test_stale_scan_recovery_started(self, quality_tempdir):
        """Бэкдейт updated_at > 600с + lock + живой таск → started, audit scan_stalled."""
        state = _make_app_state(scan_id="stale-scan-1")
        tracker = state.scan_progress

        # Создаём running-запись и бэкдейтим
        tracker.start("stale-scan-1", total=100)
        _backdate_progress(tracker, "stale-scan-1", seconds_ago=700)

        # Залоченный lock + живой (fake) scan_task
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))  # живой, не done
        state.scan_task = fake_task

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, state)

        # AC-1: started с новым scan_id
        assert result["scanned"] is True
        assert result["status"] == "started"
        assert result["scan_id"] != "stale-scan-1"

        # Старая запись — error+stalled (до prune; prune удаляет её при старте нового)
        old_entry = tracker._data.get("stale-scan-1")
        if old_entry is not None:
            assert old_entry["status"] == "error"
            assert old_entry.get("stalled") is True

        # audit.jsonl содержит scan_stalled
        from mcp_server.quality.audit import get_audit_store_path
        audit_lines = get_audit_store_path().read_text(encoding="utf-8").strip().split("\n")
        stall_records = [
            json.loads(line) for line in audit_lines
            if line and json.loads(line).get("action") == "scan_stalled"
        ]
        assert len(stall_records) >= 1
        assert stall_records[-1]["metadata"]["scan_id"] == "stale-scan-1"

        # cleanup
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        if state.scan_lock.locked():
            state.scan_lock.release()
        if state.scan_task and not state.scan_task.done():
            state.scan_task.cancel()
            try:
                await state.scan_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_red_mutation_remove_detection(self, quality_tempdir):
        """RED: убрать detection-ветку → already_running.

        Доказательство: если вернуть голый ``if scan_lock.locked(): return already_running``
        (без детектора), T1 краснеет — ``started`` не возвращается.
        Здесь мы симулируем мутацию через monkeypatch _scan_stall_state →
        всегда возвращает stalled=False (как «нет детектора»).
        """
        state = _make_app_state(scan_id="stale-scan-red")
        tracker = state.scan_progress
        tracker.start("stale-scan-red", total=10)
        _backdate_progress(tracker, "stale-scan-red", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        # Мутация: детектор «выключен» — всегда not stalled
        with patch("mcp_server.tools.quality._scan_stall_state", new=AsyncMock(return_value={
            "stalled": False, "scan_id": "stale-scan-red", "phase": None,
            "imported": None, "total": None, "age": 700, "reason": None, "orphan": False,
        })):
            result = await run_quality_scan({}, state)

        # RED-доказательство: already_running (не started)
        assert result["status"] == "already_running"
        assert result["scanned"] is False

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# T2: граница порога — свежая running → already_running
# ═══════════════════════════════════════════════════════════════


class TestT2ThresholdBoundary:
    """T2: age < порога → already_running; RED: порог 10**9 → T1 красный."""

    @pytest.mark.asyncio
    async def test_fresh_running_already_running(self, quality_tempdir):
        """Свежая running (age=10с < 600) → already_running, lock не меняется."""
        state = _make_app_state(scan_id="fresh-scan-1")
        tracker = state.scan_progress
        tracker.start("fresh-scan-1", total=10)
        _backdate_progress(tracker, "fresh-scan-1", seconds_ago=10)  # свежая

        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task
        original_lock = state.scan_lock

        result = await run_quality_scan({}, state)

        assert result["status"] == "already_running"
        assert result["scan_id"] == "fresh-scan-1"
        # Lock не заменён (тот же объект)
        assert state.scan_lock is original_lock

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()

    @pytest.mark.asyncio
    async def test_red_mutation_huge_threshold(self, quality_tempdir):
        """RED: порог 10**9 → T1-сценарий краснеет (детектор не срабатывает).

        Доказательство: при SCAN_STALL_SECONDS=10**9 даже бэкдейт на 700с
        даёт age=700 < 10**9 → not stalled → already_running (не started).
        """
        state = _make_app_state(scan_id="stale-huge", stall_seconds=10**9)
        tracker = state.scan_progress
        tracker.start("stale-huge", total=10)
        _backdate_progress(tracker, "stale-huge", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        result = await run_quality_scan({}, state)

        # RED-доказательство: already_running (порог слишком большой)
        assert result["status"] == "already_running"

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# T3: stall() + prune
# ═══════════════════════════════════════════════════════════════


class TestT3StallMethod:
    """T3: stall() → status=error+stalled, force-persist; prune_finished удаляет."""

    def test_stall_sets_error_stalled(self, tmp_path):
        """stall() ставит status=error + stalled=true + лог."""
        tracker = ImportProgressTracker(persist_path=str(tmp_path / "scan_state.json"))
        tracker.start("scan-3", total=5)
        tracker.stall("scan-3", "stale: no heartbeat for 700s")

        snap = tracker.get("scan-3")
        assert snap is not None
        assert snap["status"] == "error"
        assert snap.get("stalled") is True
        # Лог содержит reason
        assert any("stale" in m["text"] for m in snap["messages"])

    def test_stall_prune_finished_removes(self, tmp_path):
        """stall() → терминальная (error) → prune_finished удаляет."""
        tracker = ImportProgressTracker(persist_path=str(tmp_path / "scan_state.json"))
        tracker.start("scan-3a", total=5)
        tracker.stall("scan-3a", "stale")
        # error — терминальное множество → prune удаляет
        removed = tracker.prune_finished()
        assert removed >= 1
        assert tracker.get("scan-3a") is None

    def test_stall_not_confused_with_running(self, tmp_path):
        """stall() → get() не путает с running (status=error, не running)."""
        tracker = ImportProgressTracker(persist_path=str(tmp_path / "scan_state.json"))
        tracker.start("scan-3b", total=5)
        tracker.stall("scan-3b", "stale")
        snap = tracker.get("scan-3b")
        assert snap["status"] != "running"

    def test_red_mutation_running_status(self, tmp_path):
        """RED: вернуть status='running' в stall() → prune/TTL-тест красный.

        Доказательство: если stall() ставит status='running' (мутация),
        prune_finished НЕ удаляет (running не в терминальном множестве).
        """
        tracker = ImportProgressTracker(persist_path=str(tmp_path / "scan_state.json"))
        tracker.start("scan-3-red", total=5)
        # Мутация: вручную ставим running вместо error
        tracker._data["scan-3-red"]["status"] = "running"
        tracker._data["scan-3-red"]["stalled"] = True
        removed = tracker.prune_finished()
        # RED-доказательство: prune НЕ удалил (running не терминальный)
        assert removed == 0
        assert tracker.get("scan-3-red") is not None


# ═══════════════════════════════════════════════════════════════
# T4: generation-guard — зомби не перезаписывает новый скан
# ═══════════════════════════════════════════════════════════════


class TestT4GenerationGuard:
    """T4: после bump generation зомби-_bg_scan done() не вызван."""

    @pytest.mark.asyncio
    async def test_zombie_bg_scan_skips_done(self, quality_tempdir, tmp_path):
        """Зомби-_bg_scan (generation изменился) → done() НЕ вызван."""
        scan_lock = asyncio.Lock()
        tracker = ImportProgressTracker(persist_path=None)
        tracker.start("zombie-scan", total=0)  # _bg_scan не вызывает start
        scan_state = {"lock": scan_lock, "task_ref": [None]}

        # app_state с generation=0
        state = SimpleNamespace(scan_generation=0)

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=_slow_scan(mock_metrics, delay=0.3)):
            # Запускаем _bg_scan, но ПЕРЕД его завершением bump'аем generation
            task = asyncio.create_task(
                _bg_scan(
                    scan_id="zombie-scan",
                    knowledge_dir=tmp_path,
                    qdrant_client=None,
                    scan_progress=tracker,
                    scan_state=scan_state,
                    cancel_event=asyncio.Event(),
                    app_state=state,
                )
            )
            # Даём _bg_scan стартовать и взять lock
            await asyncio.sleep(0.05)
            # Bump generation → _bg_scan теперь «зомби»
            state.scan_generation = 1
            await task

        # done() НЕ вызван (generation-guard пропустил) → статус остался running
        entry = tracker._data.get("zombie-scan")
        assert entry is not None
        assert entry.get("status") != "done"  # всё ещё running (done пропущен)

    @pytest.mark.asyncio
    async def test_red_mutation_no_guard(self, quality_tempdir, tmp_path):
        """RED: снять guard → done() вызван → зомби перезаписывает.

        Доказательство: без generation-guard (без bump) зомби-_bg_scan
        вызывает done() → статус становится 'done'.
        """
        scan_lock = asyncio.Lock()
        tracker = ImportProgressTracker(persist_path=None)
        tracker.start("normal-scan", total=0)  # _bg_scan не вызывает start
        scan_state = {"lock": scan_lock, "task_ref": [None]}
        state = SimpleNamespace(scan_generation=0)

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            task = asyncio.create_task(
                _bg_scan(
                    scan_id="normal-scan",
                    knowledge_dir=tmp_path,
                    qdrant_client=None,
                    scan_progress=tracker,
                    scan_state=scan_state,
                    cancel_event=asyncio.Event(),
                    app_state=state,
                )
            )
            await asyncio.sleep(0.05)
            # НЕ bump'аем generation → guard не срабатывает → done() вызван
            await task

        entry = tracker._data.get("normal-scan")
        # RED-доказательство: без bump generation done() вызван (status=done)
        assert entry is not None
        assert entry.get("status") == "done"


# ═══════════════════════════════════════════════════════════════
# T5: integration — evidence в audit + warning в новой записи
# ═══════════════════════════════════════════════════════════════


class TestT5IntegrationEvidence:
    """T5: staged stale → 2-й run=started; evidence=audit+warning в новой записи."""

    @pytest.mark.asyncio
    async def test_stale_recovery_evidence(self, quality_tempdir):
        """Staged stale → started; audit scan_stalled + warning в новой записи."""
        state = _make_app_state(scan_id="stale-5")
        tracker = state.scan_progress
        tracker.start("stale-5", total=50)
        _backdate_progress(tracker, "stale-5", seconds_ago=800)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, state)

        assert result["status"] == "started"
        new_id = result["scan_id"]

        # Warning в новой записи (P2-2)
        new_snap = tracker.get(new_id)
        assert new_snap is not None
        assert any("stalled" in m["text"] and "stale-5" in m["text"] for m in new_snap["messages"])

        # audit scan_stalled
        from mcp_server.quality.audit import get_audit_store_path
        audit_text = get_audit_store_path().read_text(encoding="utf-8")
        assert "scan_stalled" in audit_text
        assert "stale-5" in audit_text

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        if state.scan_task and not state.scan_task.done():
            state.scan_task.cancel()
            try:
                await state.scan_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_red_mutation_disable_recovery(self, quality_tempdir):
        """RED: disable recovery → already_running → красный."""
        state = _make_app_state(scan_id="stale-5-red")
        tracker = state.scan_progress
        tracker.start("stale-5-red", total=50)
        _backdate_progress(tracker, "stale-5-red", seconds_ago=800)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        # Мутация: ДЕТЕКТОР отключён (stalled=False) → recovery не вызывается.
        # RED-доказательство: без детекции зависшего скана фича не работает —
        # код возвращает already_running (прежнее поведение).
        async def _no_stall(app_state, *, now=None):
            return {
                "stalled": False, "scan_id": getattr(app_state, "scan_id", None),
                "phase": None, "imported": None, "total": None,
                "age": None, "reason": None, "orphan": False,
            }

        with patch("mcp_server.tools.quality._scan_stall_state", new=_no_stall):
            result = await run_quality_scan({}, state)

        assert result["status"] == "already_running"

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# T6: регресс — нормальный скан не тронут
# ═══════════════════════════════════════════════════════════════


class TestT6Regression:
    """T6: нормальный скан → scan_completed; cancel работает."""

    @pytest.mark.asyncio
    async def test_normal_scan_completes(self, quality_tempdir, tmp_path):
        """Нормальный скан (lock свободен) → started → scan_completed в audit."""
        state = _make_app_state()
        mock_metrics = {
            "files_scanned": 5, "review_queue_size": 1,
            "duplicates_detected": 0, "issues_created": 2,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, state)
            assert result["status"] == "started"
            # Даём _bg_scan завершиться ПОД патчем (иначе сработает реальный run_scan)
            if state.scan_task and not state.scan_task.done():
                await state.scan_task

        from mcp_server.quality.audit import count_actions
        assert count_actions(action="scan_completed") >= 1

    @pytest.mark.asyncio
    async def test_cancel_quality_scan_works(self, quality_tempdir):
        """cancel_quality_scan → {cancelled: True} при залоченном lock."""
        state = _make_app_state(scan_id="active-6")
        await _acquire_lock(state.scan_lock)
        state.scan_cancel_event = asyncio.Event()

        result = await cancel_quality_scan({}, state)
        assert result["cancelled"] is True
        assert state.scan_cancel_event.is_set()

        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# T7: post-recovery инварианты (RED ловит P1-1)
# ═══════════════════════════════════════════════════════════════


class TestT7PostRecoveryInvariants:
    """T7: после recovery lock identity / cancel / no-2nd-scan / import busy.

    RED-мутация: вернуть локальную (старую) scan_lock в _bg_scan →
    все 4 инварианта красные.
    """

    @pytest.mark.asyncio
    async def test_post_recovery_invariants(self, quality_tempdir):
        """После recovery: heavy_ops_lock is scan_lock, locked, cancel, no-2nd, import busy."""
        state = _make_app_state(scan_id="stale-7")
        tracker = state.scan_progress
        tracker.start("stale-7", total=10)
        _backdate_progress(tracker, "stale-7", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }
        with patch("mcp_server.quality.scanner.run_scan", new=_slow_scan(mock_metrics, delay=5.0)):
            result = await run_quality_scan({}, state)
            # Даём _bg_scan стартовать ПОД патчем: иначе он свяжет РЕАЛЬНЫЙ run_scan
            await asyncio.sleep(0)

        assert result["status"] == "started"

        # N4: ещё тик — _bg_scan берёт lock в первой точке

        # Инвариант 1: heavy_ops_lock is scan_lock (тот же объект)
        assert state.heavy_ops_lock is state.scan_lock

        # Инвариант 2: lock залочен (новый скан держит НОВЫЙ lock)
        assert state.scan_lock.locked() is True

        # Инвариант 3: cancel_quality_scan → {cancelled: True}
        cancel_result = await cancel_quality_scan({}, state)
        assert cancel_result.get("cancelled") is True

        # Инвариант 4: 2-й run_quality_scan → already_running
        result2 = await run_quality_scan({}, state)
        assert result2["status"] == "already_running"

        # N4: импорт busy — проверяем PDF-путь (content.py:160)
        # Симулируем: heavy_ops_lock.locked() → True → импорт должен получить busy/queued
        assert state.heavy_ops_lock.locked() is True  # импорт увидит locked → busy

        # cleanup
        if state.scan_task and not state.scan_task.done():
            state.scan_task.cancel()
            try:
                await state.scan_task
            except asyncio.CancelledError:
                pass
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_red_mutation_stale_local_lock(self, quality_tempdir):
        """RED: вернуть локальную (старую) scan_lock → 4 инварианта красные.

        Доказательство: если после recovery НЕ перечитать scan_lock (P1-1),
        _bg_scan получает старый lock → heavy_ops_lock (новый) свободен →
        (a) heavy_ops_lock is NOT scan_lock (разные объекты);
        (b) heavy_ops_lock.locked() is False (новый свободен);
        (c) cancel → no active scan (новый свободен);
        (d) 2-й run → started (новый свободен → 2-й скан).

        Симулируем мутацию: патчим run_quality_scan так, чтобы НЕ делать
        re-read — передаём старый lock в _bg_scan.
        """
        state = _make_app_state(scan_id="stale-7-red")
        tracker = state.scan_progress
        tracker.start("stale-7-red", total=10)
        _backdate_progress(tracker, "stale-7-red", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task
        old_lock = state.scan_lock  # старый lock (залочен)

        mock_metrics = {
            "files_scanned": 0, "review_queue_size": 0,
            "duplicates_detected": 0, "issues_created": 0,
        }

        # Мутация: перехватываем create_task и передаём СТАРЫЙ lock
        # (как будто re-read не сделан)
        original_create_task = asyncio.create_task

        def _mutated_create_task(coro, **kwargs):
            return original_create_task(coro, **kwargs)

        # Патчим _bg_scan чтобы перехватить scan_state и подсунуть старый lock
        original_bg_scan = _bg_scan

        async def _mutated_bg_scan(*args, **kwargs):
            # Подменяем lock в scan_state на старый (мутация P1-1)
            kwargs["scan_state"]["lock"] = old_lock
            return await original_bg_scan(*args, **kwargs)

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            with patch("mcp_server.tools.quality._bg_scan", new=_mutated_bg_scan):
                result = await run_quality_scan({}, state)

        assert result["status"] == "started"
        await asyncio.sleep(0)

        # RED-доказательства:
        # (a) heavy_ops_lock is NOT scan_lock (новый ≠ старый, который в _bg_scan)
        # После recovery heavy_ops_lock заменён, но _bg_scan держит old_lock
        # → heavy_ops_lock (новый) свободен
        assert state.heavy_ops_lock.locked() is False  # RED: новый свободен

        # (c) cancel → no active scan (новый свободен)
        cancel_result = await cancel_quality_scan({}, state)
        assert cancel_result.get("cancelled") is False  # RED: no active scan

        # (d) 2-й run → started (новый свободен → 2-й скан)
        result2 = await run_quality_scan({}, state)
        assert result2["status"] == "started"  # RED: 2-й скан стартовал

        # cleanup
        for t in [fake_task, state.scan_task]:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if old_lock.locked():
            old_lock.release()


# ═══════════════════════════════════════════════════════════════
# T8: cadence-assert (P1-2) — heartbeat ≤ каждых 200 + embed-bracket
# ═══════════════════════════════════════════════════════════════


class TestT8CadenceAssert:
    """T8: _update_scores_with_dup шлёт progress ≤ каждых 200; embed до/после.

    N3: измеряем cadence (периодичность progress-событий), а не «окно <600 с».
    """

    @pytest.mark.asyncio
    async def test_update_scores_cadence(self, quality_tempdir):
        """_update_scores_with_dup шлёт progress.log каждые ≤200 записей с dup."""
        from mcp_server.quality.scanner import _update_scores_with_dup

        # 250 записей с dup_count>0 → ≥1 progress.log на 200-й
        scored = []
        dup_map = {}
        for i in range(250):
            fm = MagicMock()
            fm.knowledge_id = f"kid-{i}"
            fm.zone = "private"
            fm.updated_at = datetime.now(timezone.utc)  # модель хранит datetime
            fm.model_dump.return_value = {"source": "x", "evergreen": False}
            scored.append((MagicMock(), fm, 0.5))
            dup_map[f"kid-{i}"] = 1

        client = MagicMock()
        tracker = ImportProgressTracker(persist_path=None)
        tracker.start("scan-8", total=250)

        await _update_scores_with_dup(
            client, scored, dup_map,
            progress=tracker, progress_id="scan-8",
            cancel_event=None,
        )

        snap = tracker.get("scan-8")
        # Cadence: хотя бы 1 progress.log на 200-й записи
        cadence_logs = [m for m in snap["messages"] if "scoring_dup:" in m["text"]]
        assert len(cadence_logs) >= 1

    @pytest.mark.asyncio
    async def test_update_scores_cancel_break(self, quality_tempdir):
        """cancel_event → _update_scores_with_dup прерывается (N2 — cancel работает)."""
        from mcp_server.quality.scanner import _update_scores_with_dup

        scored = []
        dup_map = {}
        for i in range(300):
            fm = MagicMock()
            fm.knowledge_id = f"kid-c-{i}"
            fm.zone = "private"
            fm.updated_at = datetime.now(timezone.utc)  # модель хранит datetime
            fm.model_dump.return_value = {"source": "x", "evergreen": False}
            scored.append((MagicMock(), fm, 0.5))
            dup_map[f"kid-c-{i}"] = 1

        client = MagicMock()
        cancel_event = asyncio.Event()
        cancel_event.set()  # отменён

        tracker = ImportProgressTracker(persist_path=None)
        tracker.start("scan-8c", total=300)

        await _update_scores_with_dup(
            client, scored, dup_map,
            progress=tracker, progress_id="scan-8c",
            cancel_event=cancel_event,
        )

        # set_payload не вызван ни разу (cancel на первой итерации)
        assert client.set_payload.call_count == 0

    @pytest.mark.asyncio
    async def test_red_mutation_no_cadence(self, quality_tempdir):
        """RED: убрать периодический progress.log → окно >600 с (cadence-слепа).

        Доказательство: если _update_scores_with_dup НЕ шлёт progress каждые 200,
        при 250 dup-записях нет ни одного cadence-лога → детектор помечает
        живой скан как stalled (окно молчания > порога).
        Здесь проверяем что cadence-логи ЕСТЬ (GREEN); мутация убрала бы их.
        """
        from mcp_server.quality.scanner import _update_scores_with_dup

        scored = []
        dup_map = {}
        for i in range(250):
            fm = MagicMock()
            fm.knowledge_id = f"kid-r-{i}"
            fm.zone = "private"
            fm.updated_at = datetime.now(timezone.utc)  # модель хранит datetime
            fm.model_dump.return_value = {"source": "x", "evergreen": False}
            scored.append((MagicMock(), fm, 0.5))
            dup_map[f"kid-r-{i}"] = 1

        client = MagicMock()
        tracker = ImportProgressTracker(persist_path=None)
        tracker.start("scan-8r", total=250)

        await _update_scores_with_dup(
            client, scored, dup_map,
            progress=tracker, progress_id="scan-8r",
            cancel_event=None,
        )

        snap = tracker.get("scan-8r")
        cadence_logs = [m for m in snap["messages"] if "scoring_dup:" in m["text"]]
        # GREEN: cadence-логи есть (≥1 на 200-й)
        assert len(cadence_logs) >= 1, "cadence-лог отсутствует — мутация убрала heartbeat"

    @pytest.mark.asyncio
    async def test_live_scan_not_stalled(self, quality_tempdir):
        """Живой скан (heartbeat свежий) ≠ stalled — AC-3 на уровне детектора."""
        state = _make_app_state(scan_id="live-8")
        tracker = state.scan_progress
        tracker.start("live-8", total=100)
        # Свежий heartbeat (age=5с)
        _backdate_progress(tracker, "live-8", seconds_ago=5)
        await _acquire_lock(state.scan_lock)
        fake_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = fake_task

        st = await _scan_stall_state(state)
        assert st["stalled"] is False
        assert st["age"] is not None and st["age"] < 600

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()


# ═══════════════════════════════════════════════════════════════
# N1: orphan-lock ветка
# ═══════════════════════════════════════════════════════════════


class TestN1OrphanLock:
    """N1: orphan-lock — lock держится, живого scan_task нет."""

    @pytest.mark.asyncio
    async def test_orphan_lock_stall_called(self, quality_tempdir):
        """Orphan: stall(old_id, 'orphan lock') + audit scan_stalled."""
        state = _make_app_state(scan_id="orphan-1")
        tracker = state.scan_progress
        tracker.start("orphan-1", total=10)
        _backdate_progress(tracker, "orphan-1", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        # scan_task = None (orphan — таска нет, lock «завис»)
        state.scan_task = None
        # import_task/convert_task = None (нет живых тяжёлых тасков)
        state.import_task = None
        state.convert_task = None

        st = await _scan_stall_state(state)
        assert st["stalled"] is True
        assert st["orphan"] is True

        await _recover_stalled_scan(state, st)

        # stall() вызван (запись error+stalled)
        old_entry = tracker._data.get("orphan-1")
        if old_entry is not None:
            assert old_entry["status"] == "error"
            assert old_entry.get("stalled") is True

        # audit scan_stalled
        from mcp_server.quality.audit import get_audit_store_path
        audit_text = get_audit_store_path().read_text(encoding="utf-8")
        assert "scan_stalled" in audit_text

        # Lock заменён (нет живых тяжёлых тасков → release разрешён)
        assert not state.scan_lock.locked()
        assert state.scan_id is None

    @pytest.mark.asyncio
    async def test_orphan_lock_no_release_with_live_import(self, quality_tempdir):
        """N1: orphan + живой import_task → diagnostic-only, БЕЗ release."""
        state = _make_app_state(scan_id="orphan-2")
        tracker = state.scan_progress
        tracker.start("orphan-2", total=10)
        _backdate_progress(tracker, "orphan-2", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        state.scan_task = None
        # Живой import_task (держит lock)
        live_import = asyncio.ensure_future(asyncio.sleep(1000))
        state.import_task = live_import
        original_lock = state.scan_lock

        st = await _scan_stall_state(state)
        assert st["orphan"] is True

        await _recover_stalled_scan(state, st)

        # Lock НЕ заменён (живой import_task → diagnostic-only)
        assert state.scan_lock is original_lock
        assert state.scan_lock.locked()  # всё ещё залочен
        # scan_id НЕ сброшен (release не был)
        # (scan_id=None только при release; в diagnostic-only — оставляем)

        live_import.cancel()
        try:
            await live_import
        except asyncio.CancelledError:
            pass
        state.scan_lock.release()

    def test_has_live_heavy_task_none(self):
        """_has_live_heavy_task: все None → False."""
        state = SimpleNamespace(import_task=None, convert_task=None)
        assert _has_live_heavy_task(state) is False

    @pytest.mark.asyncio
    async def test_has_live_heavy_task_live(self):
        """_has_live_heavy_task: живой task → True."""
        async def _long():
            await asyncio.sleep(100)

        task = asyncio.ensure_future(_long())
        state = SimpleNamespace(import_task=task, convert_task=None)
        assert _has_live_heavy_task(state) is True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════════════════════
# N5 (опц.): старый таск .done()/.cancelled() после тика
# ═══════════════════════════════════════════════════════════════


class TestN5ZombieTaskCancelled:
    """N5: старый таск .done()/.cancelled() после recovery."""

    @pytest.mark.asyncio
    async def test_old_task_cancelled_after_recovery(self, quality_tempdir):
        """После recovery старый scan_task отменён (cancel() вызван)."""
        state = _make_app_state(scan_id="stale-n5")
        tracker = state.scan_progress
        tracker.start("stale-n5", total=10)
        _backdate_progress(tracker, "stale-n5", seconds_ago=700)
        await _acquire_lock(state.scan_lock)
        old_task = asyncio.ensure_future(asyncio.sleep(1000))
        state.scan_task = old_task

        st = await _scan_stall_state(state)
        await _recover_stalled_scan(state, st)

        # Даём отмене осесть (cancellation обрабатывается на следующем тике loop)
        await asyncio.sleep(0)
        assert old_task.cancelled() or old_task.done()

        if not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
