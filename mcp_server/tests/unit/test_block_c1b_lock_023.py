"""L6/L11/L11b — Block C-1b трассы code-2026-09-25-023.

Проверяют owner-инфраструктуру `heavy_ops_lock` вне reconcile:
- L6a/L6b: admin-reindex — bounded wait (busy → error) и identity-check
  (лок заменён во время ожидания → error, P1-3).
- L11: writer phantom-lock (P1-4) — `_bg_import`, ожидая старый лок O,
  при замене O→N обязан НЕ работать: release O, запись назад в очередь
  `status="queued"`, owner не выставлен.
- L11b: `_bg_convert` — тот же протокол, честная ошибка `status="error"`.

Наблюдаемые RED-мутации (проверены прогоном):
- L6a ← снять bounded wait (acquire без таймаута) — тест висит/красный.
- L6b ← убрать identity-check → admin работает на фантомном O (proceeds).
- L11 ← убрать identity-check в `_bg_import` → статус уходит из "queued".
- L11b ← убрать identity-check в `_bg_convert` → нет error-записи.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from mcp_server.tools import content as C
from mcp_server.tools.admin import reindex


def _state(**kw) -> SimpleNamespace:
    st = SimpleNamespace(
        heavy_ops_lock=asyncio.Lock(),
        heavy_lock_owner=None,
        pipeline=MagicMock(),
        knowledge_index=MagicMock(),
        qdrant=MagicMock(),
        store=MagicMock(),
        import_task=None,
        import_cancel_event=None,
        import_progress=None,
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


async def _wait_waiter(lock: asyncio.Lock) -> bool:
    """Дождаться регистрации waiter'а на локе (защита от флака, P3-в)."""
    for _ in range(200):
        await asyncio.sleep(0)
        if getattr(lock, "_waiters", None):
            return True
    return False


# ── L6: admin-reindex под локом ────────────────────────────────


async def test_l6a_admin_busy_lock_error(monkeypatch):
    """L6a: лок занят дольше таймаута → status=error, heavy_ops_lock busy."""
    monkeypatch.setattr(
        "mcp_server.tools.admin.settings.RECONCILE_LOCK_WAIT_SECONDS", 0.05
    )
    st = _state()
    await st.heavy_ops_lock.acquire()  # лок удерживает «кто-то»

    res = await reindex({}, st)

    assert res["status"] == "error"
    assert res["error"] == "heavy_ops_lock busy"


async def test_l6b_admin_phantom_lock_error(monkeypatch):
    """L6b (P1-3): лок заменён пока admin ждал → identity-check → error."""
    monkeypatch.setattr(
        "mcp_server.tools.admin.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    st = _state()
    lock_O = st.heavy_ops_lock
    await lock_O.acquire()

    task = asyncio.create_task(reindex({}, st))
    assert await _wait_waiter(lock_O), "waiter не зарегистрировался на O (флак-риск)"

    st.heavy_ops_lock = asyncio.Lock()  # recovery 022 заменил O→N
    lock_O.release()

    res = await task

    assert res["status"] == "error"
    assert res["error"] == "heavy lock replaced during wait"
    assert not lock_O.locked()
    assert st.heavy_lock_owner is None


# ── L11/L11b: writer phantom-lock (P1-4) ───────────────────────


def _seed_queue(import_id: str) -> dict:
    """Запись очереди.

    `operation_type="convert"` — чтобы outer-finally `_start_next_import`
    не продвигал запись (продвигает только import-операции) и не менял
    статус побочно: тест проверяет ровно phantom-lock-путь.
    """
    rec = {"import_id": import_id, "status": "queued", "operation_type": "convert"}
    C._import_queue.clear()
    C._import_queue.append(rec)
    return rec


async def test_l11_import_phantom_lock_requeue(monkeypatch):
    """L11 (P1-4): import ждёт O, лок заменён → release O, статус back to queued."""
    monkeypatch.setattr(
        "mcp_server.tools.content.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    st = _state()
    lock_O = st.heavy_ops_lock
    await lock_O.acquire()
    rec = _seed_queue("imp-l11")

    task = asyncio.create_task(
        C._bg_import(
            "imp-l11", {"_source_path": "/tmp/kilo/none-l11.pdf"}, st,
            asyncio.Event(), lock_O,
        )
    )
    assert await _wait_waiter(lock_O), "waiter не зарегистрировался на O (флак-риск)"

    st.heavy_ops_lock = asyncio.Lock()  # O→N
    lock_O.release()
    await task

    assert rec["status"] == "queued"  # запись не ушла в работу
    assert st.heavy_lock_owner is None  # owner не выставлялся
    assert not lock_O.locked()  # фантомный O освобождён


async def test_l11b_convert_phantom_lock_error(monkeypatch):
    """L11b (P1-4): convert при замене лока → release O, status=error."""
    monkeypatch.setattr(
        "mcp_server.tools.content.settings.RECONCILE_LOCK_WAIT_SECONDS", 5
    )
    st = _state()
    lock_O = st.heavy_ops_lock
    await lock_O.acquire()
    rec = _seed_queue("cv-l11b")

    task = asyncio.create_task(
        C._bg_convert(
            "cv-l11b", "/tmp/kilo/none-l11b.pdf", st, asyncio.Event(), lock_O,
        )
    )
    assert await _wait_waiter(lock_O), "waiter не зарегистрировался на O (флак-риск)"

    st.heavy_ops_lock = asyncio.Lock()  # O→N
    lock_O.release()
    await task

    assert rec["status"] == "error"
    assert "heavy lock replaced" in rec["error"]
    assert st.heavy_lock_owner is None
    assert not lock_O.locked()
