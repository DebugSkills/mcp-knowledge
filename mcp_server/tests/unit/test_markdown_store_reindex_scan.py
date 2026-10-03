"""Ф3-fix2a (P3): MarkdownStore.reindex_scan — rglob вне event loop.

Критик Ф3 (P3, rglob-honesty): source_ref_runtime.py:4-7 декларирует
«rglob/YAML не блокируют event loop», но MarkdownStore.reindex_scan
(markdown_store.py:291-298) выполнял СИНХРОННЫЙ rglob на потоке event loop
(startup-скан / refresh source_ref_index, полный reindex). Фикс (а) —
предпочтительный: блокирующий FS-обход вынесен в run_in_executor внутри
reindex_scan (все вызывающие уже await-ят его — прозрачное исправление).
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from mcp_server.storage.markdown_store import MarkdownStore


class TestReindexScanExecutor:
    async def test_filters_trash_and_underscore_and_sorts(self, tmp_path):
        """Контракт сохранён: .trash/ и _-префикс исключаются, результат
        отсортирован (мутация фильтров/сортировки роняет тест)."""
        (tmp_path / "b.md").write_text("x", encoding="utf-8")
        sub = tmp_path / "dom"
        sub.mkdir()
        (sub / "a.md").write_text("x", encoding="utf-8")
        trash = tmp_path / ".trash"
        trash.mkdir()
        (trash / "deleted.md").write_text("x", encoding="utf-8")
        (tmp_path / "_index.md").write_text("x", encoding="utf-8")

        store = MarkdownStore(tmp_path)
        paths = await store.reindex_scan()

        rel = sorted(str(p.relative_to(tmp_path)) for p in paths)
        assert rel == ["b.md", str(Path("dom") / "a.md")]

    async def test_rglob_runs_off_event_loop_thread(self, tmp_path):
        """Non-blocking: блокирующий rglob исполняется НЕ на потоке event
        loop (в executor). Мутация «вернуть sync rglob на loop» роняет
        тест — spy зафиксирует MainThread (pytest-asyncio крутит loop в
        главном потоке)."""
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        store = MarkdownStore(tmp_path)

        seen_threads: list[threading.Thread] = []
        real_rglob = Path.rglob

        def _spy(self, pattern):
            seen_threads.append(threading.current_thread())
            return real_rglob(self, pattern)

        with patch.object(Path, "rglob", _spy):
            paths = await store.reindex_scan()

        assert [p.name for p in paths] == ["a.md"]
        assert seen_threads, "rglob must be invoked"
        loop_thread = threading.current_thread()
        offenders = [t.name for t in seen_threads if t is loop_thread]
        assert not offenders, (
            "rglob executed on event-loop thread — blocking scan regression "
            f"(P3): {offenders}"
        )
