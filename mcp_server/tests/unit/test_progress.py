"""Unit tests: ImportProgressTracker — in-memory import progress store.

Фаза 13.9: live import progress (Variant A — progress bar + log panel).
"""

from __future__ import annotations

import time

import pytest
from mcp_server.progress import ImportProgressTracker


class TestImportProgressTracker:
    """Тесты ImportProgressTracker: start, section_done, log, done, error, get, TTL, robustness."""

    # ── start / get ──────────────────────────────────────────

    def test_start_creates_entry_with_total(self):
        """start создаёт запись с total и статусом running."""
        t = ImportProgressTracker()
        t.start("abc", total=100, meta={"file": "test.md"})
        snap = t.get("abc")
        assert snap is not None
        assert snap["import_id"] == "abc"
        assert snap["status"] == "running"
        assert snap["total"] == 100
        assert snap["imported"] == 0
        assert snap["failed"] == 0
        assert snap["phase"] == "decomposing"
        assert isinstance(snap["started_at"], str)
        assert isinstance(snap["messages"], list)
        assert len(snap["messages"]) == 0

    def test_get_unknown_returns_none(self):
        """get для неизвестного import_id возвращает None."""
        t = ImportProgressTracker()
        assert t.get("nonexistent") is None

    def test_get_returns_snapshot_not_mutable_ref(self):
        """get возвращает копию, а не ссылку на внутренний dict."""
        t = ImportProgressTracker()
        t.start("x", total=10)
        snap = t.get("x")
        snap["imported"] = 999
        assert t.get("x")["imported"] == 0  # оригинал не изменился

    # ── section_done / section_failed ────────────────────────

    def test_section_done_increments_imported(self):
        """section_done увеличивает imported и пушит message."""
        t = ImportProgressTracker()
        t.start("abc", total=10)
        t.section_done("abc", sequence=1, title="Intro")
        t.section_done("abc", sequence=2, title="Chapter 1")
        snap = t.get("abc")
        assert snap["imported"] == 2
        assert snap["status"] == "running"

    def test_section_failed_increments_failed(self):
        """section_failed увеличивает failed и логирует warning."""
        t = ImportProgressTracker()
        t.start("abc", total=5)
        t.section_failed("abc", sequence=1, title="Bad", error="Parse error")
        snap = t.get("abc")
        assert snap["failed"] == 1
        assert any("Parse error" in m["text"] for m in snap["messages"])

    # ── log ─────────────────────────────────────────────────

    def test_log_appends_message(self):
        """log добавляет запись в messages."""
        t = ImportProgressTracker()
        t.start("abc", total=10)
        t.log("abc", "info", "import_content: 10/100 sections written, git commit")
        snap = t.get("abc")
        assert len(snap["messages"]) == 1
        msg = snap["messages"][0]
        assert msg["level"] == "info"
        assert "10/100" in msg["text"]
        assert isinstance(msg["t"], str)  # HH:MM:SS

    def test_log_respects_max_messages(self):
        """log обрезает messages до max_messages (самые новые)."""
        t = ImportProgressTracker(max_messages=3)
        t.start("abc", total=10)
        for i in range(5):
            t.log("abc", "info", f"msg {i}")
        snap = t.get("abc")
        assert len(snap["messages"]) == 3
        # Должны остаться самые новые: msg 2, 3, 4
        texts = [m["text"] for m in snap["messages"]]
        assert "msg 0" not in texts
        assert "msg 4" in texts

    # ── set_phase ────────────────────────────────────────────

    def test_set_phase_updates_phase(self):
        """set_phase обновляет phase и опционально логирует."""
        t = ImportProgressTracker()
        t.start("abc", total=5)
        t.set_phase("abc", "writing", text="Writing sections...")
        snap = t.get("abc")
        assert snap["phase"] == "writing"
        assert any("Writing sections" in m["text"] for m in snap["messages"])

    # ── done / error ────────────────────────────────────────

    def test_done_sets_status_and_summary(self):
        """done переводит статус в done и сохраняет summary."""
        t = ImportProgressTracker()
        t.start("abc", total=5)
        t.section_done("abc", 1, "ok")
        t.done("abc", summary={"collection_id": "coll-1", "imported": 1})
        snap = t.get("abc")
        assert snap["status"] == "done"

    def test_error_sets_status_error(self):
        """error переводит статус в error и логирует."""
        t = ImportProgressTracker()
        t.start("abc", total=5)
        t.error("abc", error="Connection lost")
        snap = t.get("abc")
        assert snap["status"] == "error"
        assert any("Connection lost" in m["text"] for m in snap["messages"])

    # ── TTL prune ───────────────────────────────────────────

    def test_get_prunes_expired_entries(self):
        """get удаляет done/error записи старше ttl_seconds (ttl=0 для мгновенного истечения)."""
        t = ImportProgressTracker(ttl_seconds=0)
        t.start("expired", total=1)
        t.done("expired", summary={})  # помечаем done — только так ttl сработает
        time.sleep(0.01)  # гарантируем, что ttl истёк
        assert t.get("expired") is None  # просроченная done-запись удалена

    # ── Robustness: mutators never raise ─────────────────────

    def test_mutators_never_raise_on_bad_id(self):
        """Все мутаторы — best-effort: не должны кидать исключения на плохих id."""
        t = ImportProgressTracker()
        # Начинаем без start (нет записи)
        t.section_done("no-such", 1, "X")       # не должно упасть
        t.section_failed("no-such", 1, "X", "E")  # не должно упасть
        t.log("no-such", "info", "msg")           # не должно упасть
        t.set_phase("no-such", "writing")          # не должно упасть
        t.done("no-such", {})                      # не должно упасть
        t.error("no-such", "err")                  # не должно упасть
        # None id
        t.start(None, total=10)  # type: ignore[arg-type] — не должно упасть
        # Всё ок — дошли до assert
        assert True

    def test_start_with_none_id_does_not_create_entry(self):
        """start с None id не создаёт запись."""
        t = ImportProgressTracker()
        t.start(None, total=10)  # type: ignore[arg-type]
        assert t.get(None) is None  # type: ignore[arg-type]

    # ── Edge cases ──────────────────────────────────────────

    def test_multiple_imports_independent(self):
        """Два параллельных импорта не мешают друг другу."""
        t = ImportProgressTracker()
        t.start("a", total=10)
        t.start("b", total=20)
        t.section_done("a", 1, "A1")
        t.section_done("b", 1, "B1")
        assert t.get("a")["imported"] == 1
        assert t.get("b")["imported"] == 1
        assert t.get("a")["total"] == 10
        assert t.get("b")["total"] == 20

    def test_empty_messages_on_fresh_start(self):
        """Свежая запись имеет пустой список messages."""
        t = ImportProgressTracker()
        t.start("x", total=5)
        snap = t.get("x")
        assert snap["messages"] == []

    def test_updated_at_changes_on_mutation(self):
        """updated_at обновляется при каждом мутаторе."""
        t = ImportProgressTracker()
        t.start("x", total=5)
        ts1 = t.get("x")["updated_at"]
        t.section_done("x", 1, "hi")
        ts2 = t.get("x")["updated_at"]
        assert ts2 != ts1

    def test_default_ttl_is_600(self):
        """TTL по умолчанию — 600 секунд."""
        t = ImportProgressTracker()
        assert t._ttl_seconds == 600

    def test_default_max_messages_is_50(self):
        """max_messages по умолчанию — 50."""
        t = ImportProgressTracker()
        assert t._max_messages == 50

    # ── Task 2: TTL running vs done/error ─────────────────────

    def test_progress_ttl_running_not_deleted(self):
        """running-запись старше TTL НЕ удаляется (Task 2 fix)."""
        t = ImportProgressTracker(ttl_seconds=0)
        t.start("running-job", total=10)
        time.sleep(0.01)  # ttl истёк
        snap = t.get("running-job")
        assert snap is not None
        assert snap["status"] == "running"

    def test_progress_ttl_done_deleted(self):
        """done-запись старше TTL удаляется."""
        t = ImportProgressTracker(ttl_seconds=0)
        t.start("done-job", total=10)
        t.done("done-job", summary={})
        time.sleep(0.01)  # ttl истёк
        assert t.get("done-job") is None

    def test_progress_ttl_error_deleted(self):
        """error-запись старше TTL удаляется."""
        t = ImportProgressTracker(ttl_seconds=0)
        t.start("error-job", total=10)
        t.error("error-job", "fail")
        time.sleep(0.01)  # ttl истёк
        assert t.get("error-job") is None

    # ── 13.19: prune_finished ───────────────────────────────

    def test_prune_finished_removes_done_keeps_running(self):
        """prune_finished удаляет done/error записи, оставляет running, возвращает счётчик."""
        t = ImportProgressTracker()
        t.start("scan-old-done", total=5)
        t.done("scan-old-done", summary={})
        t.start("scan-old-error", total=5)
        t.error("scan-old-error", "fail")
        t.start("scan-current", total=10)  # running

        removed = t.prune_finished()
        assert removed == 2  # done + error удалены
        assert t.get("scan-old-done") is None
        assert t.get("scan-old-error") is None
        assert t.get("scan-current") is not None
        assert t.get("scan-current")["status"] == "running"

    def test_prune_finished_keep_id_preserves_specific(self):
        """keep_id сохраняет указанную запись даже если она done/error."""
        t = ImportProgressTracker()
        t.start("scan-keep", total=5)
        t.done("scan-keep", summary={})
        t.start("scan-delete", total=5)
        t.done("scan-delete", summary={})

        removed = t.prune_finished(keep_id="scan-keep")
        assert removed == 1  # только scan-delete удалён
        assert t.get("scan-keep") is not None  # сохранён
        assert t.get("scan-delete") is None

    def test_prune_finished_empty_store_returns_zero(self):
        """prune_finished на пустом store возвращает 0."""
        t = ImportProgressTracker()
        assert t.prune_finished() == 0

    def test_prune_finished_all_running_returns_zero(self):
        """Все записи running → prune_finished возвращает 0, ничего не удаляет."""
        t = ImportProgressTracker()
        t.start("scan-a", total=5)
        t.start("scan-b", total=10)

        removed = t.prune_finished()
        assert removed == 0
        assert t.get("scan-a") is not None
        assert t.get("scan-b") is not None

    def test_prune_finished_never_raises(self):
        """prune_finished — best-effort: не кидает исключений."""
        t = ImportProgressTracker()
        # Повреждаем внутренние данные
        t._data["corrupt"] = None  # type: ignore[dict-item]
        # Не должно упасть
        try:
            t.prune_finished()
        except Exception:  # noqa: BLE001
            pytest.fail("prune_finished should never raise")

    # ── 13.27: Дисковая персистентность ─────────────────────

    def test_persist_roundtrip_survives_new_tracker(self, tmp_path):
        """Снапшот записывается на диск; новый трекер восстанавливает done-состояние."""
        path = tmp_path / "scan_state.json"
        t1 = ImportProgressTracker(persist_path=path)
        t1.start("scan-x", total=10)
        t1.section_done("scan-x", sequence=1, title="S1")
        t1.done("scan-x", summary={"metrics": {"files_scanned": 5}})

        # Новый "процесс" (рестарт сервера) — другой инстанс, тот же файл
        t2 = ImportProgressTracker(persist_path=path)
        loaded = t2.load()
        assert "scan-x" in loaded
        entry = t2.get("scan-x")
        assert entry is not None
        assert entry["status"] == "done"
        assert entry["imported"] == 1
        assert entry["summary"]["metrics"]["files_scanned"] == 5

    def test_persist_running_entry_survives(self, tmp_path):
        """running-запись (прерванный скан) восстанавливается после рестарта."""
        path = tmp_path / "scan_state.json"
        t1 = ImportProgressTracker(persist_path=path)
        t1.start("scan-y", total=10)
        t1.set_phase("scan-y", "scoring", "Вычисление scores...")

        t2 = ImportProgressTracker(persist_path=path)
        loaded = t2.load()
        entry = loaded.get("scan-y")
        assert entry is not None
        assert entry["status"] == "running"
        assert entry["phase"] == "scoring"

    def test_persist_throttle_skips_frequent_updates(self, tmp_path):
        """Промежуточные апдейты внутри persist_every не переписывают файл."""
        path = tmp_path / "scan_state.json"
        t = ImportProgressTracker(persist_path=path, persist_every=60.0)
        t.start("scan-z", total=10)
        # start записал файл; section_done в пределах throttle — файл не трогаем
        with open(path, encoding="utf-8") as f:
            before = f.read()
        t.section_done("scan-z", sequence=1, title="S1")
        with open(path, encoding="utf-8") as f:
            after = f.read()
        assert after == before  # throttle сработал — файл не изменился

        # force-запись терминального состояния — сразу
        t.done("scan-z", summary={})
        with open(path, encoding="utf-8") as f:
            final = f.read()
        assert '"status": "done"' in final

    def test_persist_start_after_prune_force_write_not_throttled(self, tmp_path):
        """S25-баг (13.27): start() сразу после prune_finished (force-запись)
        НЕ должен быть пропущен throttle — иначе новый скан не попадает
        на диск и не переживает рестарт."""
        path = tmp_path / "scan_state.json"
        t = ImportProgressTracker(persist_path=path, persist_every=60.0)
        t.start("scan-old", total=5)
        t.done("scan-old", summary={})  # завершённая запись
        assert t.prune_finished() == 1  # force-запись, ставит _last_persist=now

        t.start("scan-new", total=0)  # сразу после force-записи (throttle-окно)
        on_disk = ImportProgressTracker(persist_path=path).load()
        assert "scan-new" in on_disk, f"new scan lost after prune force-write: {on_disk}"
        assert on_disk["scan-new"]["status"] == "running"

    def test_persist_disabled_without_path(self, tmp_path):
        """Без persist_path никаких файлов не создаётся (обратная совместимость)."""
        t = ImportProgressTracker()
        t.start("scan-w", total=1)
        t.done("scan-w", summary={})
        assert t.load() == {}

    def test_persist_never_raises_on_bad_data(self, tmp_path):
        """persist/load — best-effort: битый файл не роняет трекер."""
        path = tmp_path / "scan_state.json"
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        t = ImportProgressTracker(persist_path=path)
        assert t.load() == {}
        # И запись тоже не падает
        t.start("scan-v", total=1)
        t.done("scan-v", summary={})
