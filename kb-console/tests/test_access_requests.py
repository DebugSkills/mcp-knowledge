"""Тесты SQLite-стора заявок (036 Ф1a, план §2): схема/миграции, dupe,
cap, prune+events, LIKE-экранирование, права, ПДн-гигиена логов."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from kb_console.core.access_requests import (
    TERMINAL_STATUSES,
    AccessRequestError,
    AccessRequestStore,
    compute_dupe_hash,
)

_T0 = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _store(tmp_path, **kw) -> AccessRequestStore:
    return AccessRequestStore(str(tmp_path / "console" / "access_requests.db"), **kw)


def _valid_kwargs() -> dict:
    return {
        "fio": "Иван Иванович Иванов",
        "department": "Отдел разработки",
        "phone": "+7 900 000-00-00",
        "email": "ivan@example.com",
        "work_summary": "Нужен доступ для ревью документации",
    }


class TestSchemaMigration:
    def test_creates_schema_idempotently(self, tmp_path):
        store = _store(tmp_path)
        store.migrate()
        store.migrate()  # второй вызов — no-op
        with sqlite3.connect(store._path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert version == 1
        assert {"access_requests", "access_request_events"} <= tables

    def test_future_version_fail_fast_no_corruption(self, tmp_path):
        store = _store(tmp_path)
        store.migrate()
        with sqlite3.connect(store._path) as conn:
            conn.execute("PRAGMA user_version=99")
        with pytest.raises(RuntimeError, match="новой версией"):
            store.migrate()
        # без порчи: версия не сброшена
        with sqlite3.connect(store._path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 99

    def test_journal_mode_delete(self, tmp_path):
        store = _store(tmp_path)
        store.migrate()
        with sqlite3.connect(store._path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


class TestAppend:
    def test_append_and_get(self, tmp_path):
        store = _store(tmp_path)
        req = store.append(**_valid_kwargs())
        assert req.id.startswith("req_")
        assert req.status == "new"
        assert req.fio == "Иван Иванович Иванов"
        got = store.get(req.id)
        assert got.id == req.id
        events = store.events(req.id)
        assert events[0]["event"] == "created"

    def test_duplicate_within_10min_409(self, tmp_path):
        store = _store(tmp_path)
        store.append(**_valid_kwargs())
        with pytest.raises(AccessRequestError) as ei:
            store.append(**_valid_kwargs())
        assert ei.value.code == "duplicate"

    def test_duplicate_outside_window_allowed(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        store.append(**_valid_kwargs())
        t["now"] = _T0 + timedelta(minutes=11)  # clock-шов: окно истекло
        store.append(**_valid_kwargs())
        assert store.count() == 2

    def test_dupe_hash_normalizes_case_and_spaces(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        store.append(**_valid_kwargs())
        t["now"] = _T0 + timedelta(minutes=1)
        kw = _valid_kwargs()
        kw["fio"] = "  иван   иванович ИВАНОВ "  # та же норм-форма
        with pytest.raises(AccessRequestError) as ei:
            store.append(**kw)
        assert ei.value.code == "duplicate"

    def test_cap_500_after_prune_503(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, cap=3, clock=lambda: t["now"])
        ids = []
        for i in range(3):
            kw = _valid_kwargs()
            kw["fio"] = f"Фамилия {i}"
            kw["phone"] = f"+7 900 000-00-0{i}"
            req = store.append(**kw)
            ids.append(req)
            t["now"] += timedelta(seconds=1)
        # терминальная заявка уходит prune-ом при append → место освобождается
        store.set_status(ids[0].id, new_status="rejected", actor="admin")
        t["now"] += timedelta(days=200)
        kw = _valid_kwargs()
        kw["fio"] = "Новая Фамилия"
        kw["phone"] = "+7 900 111-11-11"
        req = store.append(**kw)
        assert store.count() == 3
        # cap по ВСЕМ живым: 3 записи → 4-я уже не влезает
        kw2 = _valid_kwargs()
        kw2["fio"] = "Ещё Одна"
        kw2["phone"] = "+7 900 222-22-22"
        t["now"] += timedelta(seconds=1)
        with pytest.raises(AccessRequestError) as ei:
            store.append(**kw2)
        assert ei.value.code == "cap_exceeded"
        assert store.count() == 3


class TestPrune:
    def test_prune_terminal_and_events_after_180d(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        kw = _valid_kwargs()
        req = store.append(**kw)
        t["now"] += timedelta(seconds=1)
        store.set_status(req.id, new_status="access_granted", actor="admin")
        assert len(store.events(req.id)) == 2
        t["now"] += timedelta(days=181)
        removed = store.prune()
        assert removed == 1
        with pytest.raises(AccessRequestError):
            store.get(req.id)
        assert store.events(req.id) == []  # events-таблица почищена тем же проходом

    def test_new_not_pruned(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        req = store.append(**_valid_kwargs())
        t["now"] += timedelta(days=400)
        assert store.prune() == 0
        assert store.get(req.id).status == "new"

    def test_in_progress_not_pruned(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        req = store.append(**_valid_kwargs())
        store.set_status(req.id, new_status="in_progress", actor="admin")
        t["now"] += timedelta(days=400)
        assert store.prune() == 0


class TestListFilters:
    def _seed(self, tmp_path):
        t = {"now": _T0}
        store = _store(tmp_path, clock=lambda: t["now"])
        r1 = store.append(fio="Иван Иванов", department="A", phone="+79001", email="a@x.io", work_summary="w1")
        t["now"] += timedelta(days=1)
        r2 = store.append(fio="Пётр Петров", department="B", phone="+79002", email="b@x.io", work_summary="w2")
        store.set_status(r2.id, new_status="rejected", actor="admin")
        return store, r1, r2

    def test_filter_status(self, tmp_path):
        store, r1, r2 = self._seed(tmp_path)
        assert [r.id for r in store.list(status="new")] == [r1.id]
        assert [r.id for r in store.list(status="rejected")] == [r2.id]

    def test_filter_period(self, tmp_path):
        store, r1, r2 = self._seed(tmp_path)
        got = store.list(period_from="2026-09-29")
        assert [r.id for r in got] == [r2.id]
        got = store.list(period_to="2026-09-28T23:59:59")
        assert [r.id for r in got] == [r1.id]

    def test_fio_like_wildcards_escaped(self, tmp_path):
        store, r1, r2 = self._seed(tmp_path)
        # буквальный '%' не разворачивается в «матчить всё»
        assert store.list(fio_query="%") == []
        # буквальный '_' не матчит односимвольным wildcard
        assert store.list(fio_query="Иван_Иванов") == []
        # реальная подстрока матчится
        assert [r.id for r in store.list(fio_query="Иван")] == [r1.id]
        assert [r.id for r in store.list(fio_query="Пётр")] == [r2.id]


class TestStatusWorkflow:
    def test_set_status_writes_event_and_fields(self, tmp_path):
        store = _store(tmp_path)
        req = store.append(**_valid_kwargs())
        updated = store.set_status(
            req.id, new_status="access_granted", actor="admin", note="выдан доступ"
        )
        assert updated.status == "access_granted"
        assert updated.decision_note == "выдан доступ"
        assert updated.decided_by == "admin"
        ev = store.events(req.id)
        assert ev[-1]["event"] == "status_change"
        assert ev[-1]["old_status"] == "new"
        assert ev[-1]["new_status"] == "access_granted"

    def test_bad_status_rejected(self, tmp_path):
        store = _store(tmp_path)
        req = store.append(**_valid_kwargs())
        with pytest.raises(AccessRequestError):
            store.set_status(req.id, new_status="deleted", actor="admin")

    def test_not_found(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(AccessRequestError):
            store.get("req_missing")
        with pytest.raises(AccessRequestError):
            store.set_status("req_missing", new_status="new", actor="admin")


class TestHygiene:
    def test_db_file_permissions_0600(self, tmp_path):
        import os
        import stat

        store = _store(tmp_path)
        store.migrate()
        mode = stat.S_IMODE(os.stat(store._path).st_mode)
        assert mode == 0o600

    def test_pd_not_in_error_messages(self, tmp_path):
        """ПДн-гигиена: доменные ошибки не содержат значений полей."""
        store = _store(tmp_path, cap=1)
        store.append(**_valid_kwargs())
        kw = _valid_kwargs()
        kw["fio"], kw["phone"] = "Секретная Фамилия", "+7 987"
        with pytest.raises(AccessRequestError) as ei:
            store.append(**kw)
        assert "Секретная Фамилия" not in str(ei.value)
        assert "+7 987" not in str(ei.value)
        assert ei.value.code == "cap_exceeded"

    def test_storage_error_masks_details(self, tmp_path, monkeypatch):
        """Сбой sqlite → AccessRequestError с type(exc).__name__, без полей."""
        store = _store(tmp_path)
        store.migrate()

        real_connect = sqlite3.connect

        class _BrokenConn:
            """Обёртка: C-тип Connection не позволяет подменить execute."""

            def __init__(self, *a, **kw):
                self._c = real_connect(*a, **kw)

            def execute(self, sql, *a, **kw):
                if sql.startswith("INSERT INTO access_requests"):
                    raise sqlite3.OperationalError(
                        "disk io error НА ФИО Иван Иванов"
                    )
                return self._c.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._c, name)

        monkeypatch.setattr(
            "kb_console.core.access_requests.sqlite3.connect", _BrokenConn
        )
        with pytest.raises(AccessRequestError) as ei:
            store.append(**_valid_kwargs())
        assert ei.value.code == "storage_error"
        assert "OperationalError" in str(ei.value)
        assert "Иван Иванов" not in str(ei.value)  # ПДн не всплывают


def test_compute_dupe_hash_stable():
    assert compute_dupe_hash("Иван", "+7900") == compute_dupe_hash(" иван ", "+7900")
    assert compute_dupe_hash("Иван", "+7900") != compute_dupe_hash("Иван", "+7901")
    assert all(s in TERMINAL_STATUSES for s in ("access_granted", "rejected"))
