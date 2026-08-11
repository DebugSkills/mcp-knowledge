"""Unit-тесты для quality tools: list_quality_issues, resolve_quality_issue, run_quality_scan, review_queue.

Фаза 13 (v1.0): закрывает gap-анализ — quality tools (list_quality_issues, resolve_quality_issue, run_quality_scan)
были 0 coverage; review_queue unit-only (scroll() missing в QdrantClient-обёртке).

Фаза 13.14: +review_queue_books (агрегация книг), resolve_quality_issue cascade, delete_entry cascade,
search_knowledge exclude_deprecated.

Все тесты не требуют Qdrant/Ollama — используют tempdir + mocks.
"""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.quality.issues import create_issue, set_store_dir
from mcp_server.tools.quality import (
    _bg_scan,
    _cascade_set_payload,
    cancel_quality_scan,
    list_quality_issues,
    resolve_quality_issue,
    review_queue,
    review_queue_books,
    run_quality_scan,
)

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def quality_tempdir():
    """Temp directory для quality issues store."""
    with tempfile.TemporaryDirectory() as tmp:
        set_store_dir(tmp)
        yield tmp


@pytest.fixture
def mock_app_state():
    """Minimal app_state mock для quality tools."""
    state = MagicMock()
    state.qdrant = MagicMock()
    state.store = MagicMock()
    return state


@pytest.fixture
def mock_app_state_with_settings():
    """app_state mock с settings.knowledge_dir + 13.15 scan state."""
    state = MagicMock()
    state.qdrant = MagicMock()
    state.store = MagicMock()
    state.settings = SimpleNamespace(KNOWLEDGE_DIR="/tmp/test-knowledge")
    # 13.15: scan_lock должен быть разлочен (locked() → False)
    state.scan_lock = MagicMock()
    state.scan_lock.locked = MagicMock(return_value=False)
    state.scan_progress = MagicMock()
    state.scan_id = None
    state.scan_task = None
    return state


# ═══════════════════════════════════════════════════════════════
# Step 5: list_quality_issues
# ═══════════════════════════════════════════════════════════════


class TestListQualityIssues:
    """list_quality_issues — фильтрация по types/status, пагинация через limit."""

    async def test_list_all_open_issues(self, quality_tempdir, mock_app_state):
        """Создаём 2 issues → list_quality_issues(status="open") → оба в результате."""
        create_issue("duplicate", "kid-1", "warn", "Duplicate of kid-2")
        create_issue("missing_field", "kid-3", "info", "Missing tags in frontmatter")

        result = await list_quality_issues({"types": None, "status": "open", "limit": 50}, mock_app_state)

        assert "error" not in result
        assert result["total"] == 2
        assert len(result["issues"]) == 2
        issue_ids = {i["issue_id"] for i in result["issues"]}
        assert len(issue_ids) == 2

    async def test_filter_by_type(self, quality_tempdir, mock_app_state):
        """Фильтр types=["duplicate"] → только duplicate-issues."""
        create_issue("duplicate", "kid-1", "warn", "Dup issue")
        create_issue("missing_field", "kid-2", "info", "Missing field")

        result = await list_quality_issues(
            {"types": ["duplicate"], "status": "open", "limit": 50},
            mock_app_state,
        )

        assert result["total"] == 1
        assert result["issues"][0]["type"] == "duplicate"

    async def test_filter_by_status_resolved(self, quality_tempdir, mock_app_state):
        """Создаём issue → resolve → list(status="resolved") → 1."""
        issue = create_issue("duplicate", "kid-1", "warn", "Resolved issue")
        from mcp_server.quality.issues import update_issue_status
        update_issue_status(issue.issue_id, "resolved", "Fixed")

        result = await list_quality_issues(
            {"types": None, "status": "resolved", "limit": 50},
            mock_app_state,
        )

        assert result["total"] == 1
        assert result["issues"][0]["status"] == "resolved"

    async def test_filter_by_type_and_status_combined(self, quality_tempdir, mock_app_state):
        """Комбинированный фильтр: type=duplicate + status=resolved."""
        issue1 = create_issue("duplicate", "kid-1", "warn", "Dup 1")
        _issue2 = create_issue("duplicate", "kid-2", "warn", "Dup 2")
        create_issue("missing_field", "kid-3", "info", "Missing")

        from mcp_server.quality.issues import update_issue_status
        update_issue_status(issue1.issue_id, "resolved", "Fixed")

        result = await list_quality_issues(
            {"types": ["duplicate"], "status": "resolved", "limit": 50},
            mock_app_state,
        )

        assert result["total"] == 1
        assert result["issues"][0]["issue_id"] == issue1.issue_id

    async def test_empty_store_returns_empty_list(self, quality_tempdir, mock_app_state):
        """Пустой store → пустой результат."""
        result = await list_quality_issues({"status": "open"}, mock_app_state)
        assert result["total"] == 0
        assert result["issues"] == []

    async def test_respects_limit(self, quality_tempdir, mock_app_state):
        """limit=1 → только 1 issue."""
        for i in range(3):
            create_issue("duplicate", f"kid-{i}", "warn", f"Issue {i}")

        result = await list_quality_issues({"limit": 1, "status": "open"}, mock_app_state)
        assert result["total"] == 1
        assert len(result["issues"]) == 1


# ═══════════════════════════════════════════════════════════════
# Step 6: resolve_quality_issue
# ═══════════════════════════════════════════════════════════════


class TestResolveQualityIssue:
    """resolve_quality_issue — action=resolve/ignore/deprecate, валидация, side_effects."""

    async def test_resolve_action_sets_status_resolved(self, quality_tempdir, mock_app_state):
        """action=resolve → status="resolved"."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test resolve")
        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "resolve", "reason": "Fixed"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["status"] == "resolved"
        assert result["issue_id"] == issue.issue_id

    async def test_ignore_action_sets_status_ignored(self, quality_tempdir, mock_app_state):
        """action=ignore → status="ignored"."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test ignore")
        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "ignore", "reason": "False positive"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["status"] == "ignored"

    async def test_invalid_action_returns_error(self, quality_tempdir, mock_app_state):
        """Невалидный action → error."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test invalid")
        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "delete", "reason": "Bad"},
            mock_app_state,
        )

        assert result["resolved"] is False
        assert "error" in result
        assert "Invalid action" in result["error"]

    async def test_missing_issue_id_returns_error(self, quality_tempdir, mock_app_state):
        """Пустой issue_id → error."""
        result = await resolve_quality_issue(
            {"issue_id": "", "action": "resolve"},
            mock_app_state,
        )
        assert result["resolved"] is False
        assert "error" in result

    async def test_deprecate_action_with_mock_qdrant(self, quality_tempdir, mock_app_state):
        """action=deprecate → вызывает qdrant_client.set_payload."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test deprecate")
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "deprecate", "reason": "Obsolete"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["status"] == "resolved"
        # set_payload должен быть вызван
        mock_app_state.qdrant.set_payload.assert_called_once()
        # side_effects содержит упоминание deprecated
        assert len(result["side_effects"]) >= 1
        assert any("deprecated" in se.lower() for se in result["side_effects"])

    async def test_merge_action_requires_target_id(self, quality_tempdir, mock_app_state):
        """action=merge без target_id → error."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test merge")
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "merge", "reason": "Dup"},
            mock_app_state,
        )

        assert result["resolved"] is False
        assert "target_id" in result.get("error", "").lower()

    async def test_merge_action_with_target_id(self, quality_tempdir, mock_app_state):
        """action=merge с target_id → success."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test merge ok")
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {
                "issue_id": issue.issue_id,
                "action": "merge",
                "target_id": "kid-2",
                "reason": "Merged",
            },
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["status"] == "resolved"
        assert len(result["side_effects"]) >= 1

    async def test_nonexistent_issue_returns_error(self, quality_tempdir, mock_app_state):
        """Несуществующий issue_id для deprecate → error."""
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"issue_id": "iss_nonexistent12345", "action": "deprecate", "reason": "Test"},
            mock_app_state,
        )

        assert result["resolved"] is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════════
# Step 7: run_quality_scan
# ═══════════════════════════════════════════════════════════════


class TestRunQualityScan:
    """run_quality_scan (13.15) — новый контракт: фоновая задача, мгновенный ответ.

    Новый контракт:
        {"scanned": true, "status": "started", "scan_id": "..."} — скан запущен
        {"scanned": false, "status": "already_running", "scan_id": "..."} — уже идёт
        {"scanned": false, "status": "error", "error": "..."} — сбой
    """

    @pytest.mark.asyncio
    async def test_run_scan_returns_started_with_scan_id(self, mock_app_state_with_settings):
        """mock run_scan → return {"scanned": true, "status": "started", "scan_id": "..."}."""
        mock_metrics = {
            "files_scanned": 10,
            "review_queue_size": 3,
            "duplicates_detected": 2,
            "issues_created": 5,
        }

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, mock_app_state_with_settings)

        assert result["scanned"] is True
        assert result["status"] == "started"
        assert "scan_id" in result
        assert len(result["scan_id"]) == 16  # uuid4 hex[:16]
        # 13.15: НЕТ inline metrics — metrics только через progress poll
        assert "metrics" not in result

    @pytest.mark.asyncio
    async def test_run_scan_creates_background_task(self, mock_app_state_with_settings):
        """run_quality_scan → create_task → scan_task установлена."""
        mock_metrics = {"files_scanned": 2, "review_queue_size": 1, "duplicates_detected": 0, "issues_created": 1}

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({"domain": "engineering"}, mock_app_state_with_settings)

        assert result["scanned"] is True
        # Проверяем, что scan_task был установлен
        # (в тестовом окружении create_task выполнится в текущем event loop)
        task = mock_app_state_with_settings.scan_task
        assert task is not None, "scan_task should be set by run_quality_scan"
        # 13.15: отменяем фоновую задачу — иначе «Task was destroyed but it is pending»
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_run_scan_already_running(self, mock_app_state_with_settings):
        """scan_lock уже залочен → {"status": "already_running"}."""
        # Симулируем залоченный lock
        mock_app_state_with_settings.scan_lock.locked.return_value = True
        mock_app_state_with_settings.scan_id = "existing-scan-12"
        result = await run_quality_scan({}, mock_app_state_with_settings)

        assert result["scanned"] is False
        assert result["status"] == "already_running"
        assert result["scan_id"] == "existing-scan-12"

    @pytest.mark.asyncio
    async def test_run_scan_prunes_old_finished(self, mock_app_state_with_settings):
        """13.19: при старте нового скана prune_finished() удаляет старые done/error записи."""
        # Старые завершённые записи в трекере (реальный трекер — проверяем prune)
        from mcp_server.progress import ImportProgressTracker

        tracker = ImportProgressTracker()
        tracker.start("scan-old-1", total=5)
        tracker.done("scan-old-1", {"metrics": {}})
        tracker.start("scan-old-2", total=5)
        tracker.error("scan-old-2", "boom")
        mock_app_state_with_settings.scan_progress = tracker

        with patch("mcp_server.tools.quality._bg_scan", new=AsyncMock()) as mock_bg:
            result = await run_quality_scan({}, mock_app_state_with_settings)

        assert result["scanned"] is True
        assert mock_bg.called
        # Обе старые записи удалены prune_finished, новая (текущий scan_id) осталась
        remaining = list(mock_app_state_with_settings.scan_progress._data.keys())
        assert "scan-old-1" not in remaining
        assert "scan-old-2" not in remaining
        assert len(remaining) == 1
        assert remaining[0] == result["scan_id"]

    @pytest.mark.asyncio
    async def test_run_scan_no_settings_graceful(self, mock_app_state):
        """app_state без settings → knowledge_dir=None, но скан всё равно стартует."""
        mock_metrics = {"files_scanned": 0, "review_queue_size": 0, "duplicates_detected": 0, "issues_created": 0}

        # minimal 13.15 state для mock_app_state
        mock_app_state.scan_lock = MagicMock()
        mock_app_state.scan_lock.locked = MagicMock(return_value=False)
        mock_app_state.scan_progress = MagicMock()
        mock_app_state.scan_id = None

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, mock_app_state)

        assert result["scanned"] is True
        assert result["status"] == "started"

    @pytest.mark.asyncio
    async def test_run_scan_no_progress_returns_error(self, mock_app_state):
        """app_state без scan_progress → scanned=false, error."""
        mock_app_state.scan_lock = MagicMock()
        mock_app_state.scan_lock.locked = MagicMock(return_value=False)
        mock_app_state.scan_progress = None  # не инициализирован

        result = await run_quality_scan({}, mock_app_state)

        assert result["scanned"] is False
        assert result["status"] == "error"
        assert "scan_progress" in result.get("error", "").lower()


# ═══════════════════════════════════════════════════════════════
# Step 8: review_queue (unit-only)
# ═══════════════════════════════════════════════════════════════


class TestReviewQueue:
    """review_queue — unit-only (mock qdrant.scroll wrapper)..

    Обёртка имеет scroll() → unit-тесты через mock.
    """

    async def test_review_queue_sorts_desc_by_staleness(self, mock_app_state):
        """Mock scroll возвращает точки с разным staleness_score → сортировка DESC."""
        # Создаём mock points
        def _make_point(kid, score):
            pt = MagicMock()
            pt.id = kid
            pt.payload = {
                "knowledge_id": kid,
                "staleness_score": score,
                "quality_flags": ["stale"],
                "updated_at": "2026-01-01T00:00:00Z",
            }
            return pt

        points = [
            _make_point("kid-low", 0.3),
            _make_point("kid-high", 0.9),
            _make_point("kid-mid", 0.55),
        ]

        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue({"limit": 10}, mock_app_state)

        assert "error" not in result
        queue = result["queue"]
        # Должны быть только те, у кого score >= REVIEW_THRESHOLD (0.45)
        assert len(queue) >= 2  # kid-high(0.9), kid-mid(0.55)
        # Проверяем сортировку DESC
        scores = [item["staleness_score"] for item in queue]
        assert scores == sorted(scores, reverse=True), f"Not sorted DESC: {scores}"

    async def test_review_queue_filters_below_threshold(self, mock_app_state):
        """Точки с staleness_score < REVIEW_THRESHOLD (0.45) — не попадают."""
        def _make_point(kid, score):
            pt = MagicMock()
            pt.id = kid
            pt.payload = {
                "knowledge_id": kid,
                "staleness_score": score,
                "quality_flags": [],
                "updated_at": "2026-01-01T00:00:00Z",
            }
            return pt

        points = [
            _make_point("kid-valid", 0.88),
            _make_point("kid-low1", 0.1),
            _make_point("kid-low2", 0.3),
            _make_point("kid-border", 0.45),
        ]

        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue({"limit": 10}, mock_app_state)

        queue = result["queue"]
        kid_names = {item["knowledge_id"] for item in queue}
        assert "kid-valid" in kid_names
        # 0.45 >= threshold → должно попасть
        assert "kid-border" in kid_names
        # ниже порога — не попадают
        assert "kid-low1" not in kid_names
        assert "kid-low2" not in kid_names

    async def test_review_queue_respects_limit(self, mock_app_state):
        """limit=1 → только 1 запись."""
        def _make_point(kid, score):
            pt = MagicMock()
            pt.id = kid
            pt.payload = {
                "knowledge_id": kid,
                "staleness_score": score,
                "quality_flags": [],
                "updated_at": "2026-01-01T00:00:00Z",
            }
            return pt

        points = [
            _make_point("kid-a", 0.9),
            _make_point("kid-b", 0.8),
            _make_point("kid-c", 0.7),
        ]

        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue({"limit": 1}, mock_app_state)
        assert len(result["queue"]) == 1
        assert result["queue"][0]["staleness_score"] == 0.9

    async def test_review_queue_with_domain_filter(self, mock_app_state):
        """Фильтр domain → scroll вызывается с domain-условием."""
        mock_app_state.qdrant.scroll = MagicMock(return_value=([], None))

        result = await review_queue({"domain": "engineering", "limit": 5}, mock_app_state)

        assert result["queue"] == []
        # Проверяем что scroll был вызван
        mock_app_state.qdrant.scroll.assert_called_once()

    async def test_review_queue_handles_exception(self, mock_app_state):
        """qdrant_client.scroll бросает исключение → error, пустая queue."""
        mock_app_state.qdrant.scroll = MagicMock(
            side_effect=ConnectionError("Qdrant down")
        )

        result = await review_queue({"limit": 10}, mock_app_state)

        assert result["queue"] == []
        assert "error" in result
        assert "Qdrant down" in result["error"]

    async def test_review_queue_empty_scroll(self, mock_app_state):
        """Пустой scroll → пустая queue."""
        mock_app_state.qdrant.scroll = MagicMock(return_value=([], None))

        result = await review_queue({"limit": 10}, mock_app_state)

        assert result["queue"] == []
        assert result["total_in_queue"] == 0


# ═══════════════════════════════════════════════════════════════
# Фаза 13.14: review_queue_books — агрегация книг
# ═══════════════════════════════════════════════════════════════


class TestReviewQueueBooks:
    """review_queue_books — агрегация по parent_knowledge_id, пагинация scroll."""

    def _make_point(self, kid, parent_id, score, domain="eng", subject="python",
                    section_header="", quality_flags=None, status="published"):
        pt = MagicMock()
        pt.id = kid
        pt.payload = {
            "knowledge_id": kid,
            "parent_knowledge_id": parent_id,
            "staleness_score": score,
            "quality_flags": quality_flags or ["stale"],
            "domain": domain,
            "subject": subject,
            "section_header": section_header,
            "status": status,
            "updated_at": "2026-01-01T00:00:00Z",
        }
        return pt

    async def test_groups_by_parent(self, mock_app_state):
        """Точки с разными parent_knowledge_id → отдельные книги."""
        points = [
            self._make_point("kid-1", "book-a", 0.6),
            self._make_point("kid-2", "book-a", 0.7),
            self._make_point("kid-3", "book-b", 0.8),
        ]
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 10}, mock_app_state)

        assert "error" not in result
        books = result["books"]
        assert result["total_books"] == 2
        assert len(books) == 2
        book_ids = {b["book_id"] for b in books}
        assert book_ids == {"book-a", "book-b"}

    async def test_computes_stale_fraction(self, mock_app_state):
        """stale_fraction = stale_sections (>=0.45) / total_sections."""
        points = [
            self._make_point("kid-1", "book-x", 0.9),
            self._make_point("kid-2", "book-x", 0.8),
            self._make_point("kid-3", "book-x", 0.1),  # below threshold
        ]
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 1
        book = books[0]
        assert book["total_sections"] == 3
        assert book["stale_sections"] == 2
        assert book["stale_fraction"] == pytest.approx(2 / 3, rel=0.01)
        assert book["max_score"] == 0.9

    async def test_sorts_books_by_fraction_then_score(self, mock_app_state):
        """Книги сортируются: сначала по stale_fraction DESC, затем max_score DESC."""
        points = [
            # book-a: 3 из 3 = 1.0, max 0.9
            self._make_point("a1", "book-a", 0.9),
            self._make_point("a2", "book-a", 0.6),
            self._make_point("a3", "book-a", 0.5),
            # book-b: 2 из 3 = 0.67, max 0.8
            self._make_point("b1", "book-b", 0.8),
            self._make_point("b2", "book-b", 0.5),
            self._make_point("b3", "book-b", 0.1),
            # book-c: 1 из 3 = 0.33, max 0.95
            self._make_point("c1", "book-c", 0.95),
            self._make_point("c2", "book-c", 0.1),
            self._make_point("c3", "book-c", 0.0),
        ]
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 3
        # Порядок: book-a (1.0) > book-b (0.67) > book-c (0.33)
        assert books[0]["book_id"] == "book-a"
        assert books[1]["book_id"] == "book-b"
        assert books[2]["book_id"] == "book-c"

    async def test_excludes_points_without_parent(self, mock_app_state):
        """Точки без parent_knowledge_id — не включаются в агрегат."""
        points = [
            self._make_point("kid-orphan", None, 0.8),  # no parent → skip
            self._make_point("kid-1", "book-x", 0.8),
        ]
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 10}, mock_app_state)

        assert result["total_books"] == 1

    async def test_respects_limit(self, mock_app_state):
        """limit=1 → только 1 книга."""
        points = [
            self._make_point("a1", "book-a", 0.9),
            self._make_point("b1", "book-b", 0.85),
        ]
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 1}, mock_app_state)

        assert len(result["books"]) == 1

    async def test_top_sections_limited_to_5(self, mock_app_state):
        """top_sections содержит не более 5 секций."""
        points = []
        for i in range(10):
            points.append(self._make_point(f"kid-{i}", "book-x", 0.9 - i * 0.05))
        mock_app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        result = await review_queue_books({"limit": 10}, mock_app_state)

        book = result["books"][0]
        assert len(book["top_sections"]) <= 5

    async def test_paginated_scroll(self, mock_app_state):
        """scroll вызывается с пагинацией (offset-based)."""
        batch1_pts = [self._make_point("a1", "book-a", 0.9)]
        batch2_pts = [self._make_point("b1", "book-b", 0.8)]

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (batch1_pts, "offset-1")
            if call_count == 2:
                return (batch2_pts, None)
            # 3-й вызов — _batch_resolve_book_titles (R1): payload без title
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll
        result = await review_queue_books({"limit": 10}, mock_app_state)

        assert call_count == 3  # 2x основной scroll + 1x резолв title (R1)
        assert result["total_books"] == 2

    async def test_handles_exception(self, mock_app_state):
        """qdrant_client.scroll бросает исключение → error."""
        mock_app_state.qdrant.scroll = MagicMock(
            side_effect=ConnectionError("Qdrant down")
        )
        result = await review_queue_books({"limit": 10}, mock_app_state)
        assert result["books"] == []
        assert "error" in result

    # ── R3: фильтр книг с stale_sections == 0 ──────────────────

    async def test_filters_books_with_zero_stale_sections(self, mock_app_state):
        """Книги с 0 устаревших секций НЕ попадают в результат (R3)."""
        points = [
            # book-a: все секции >= 0.45 → stale (должна быть в выдаче)
            self._make_point("a1", "book-a", 0.9),
            self._make_point("a2", "book-a", 0.5),
            # book-b: ВСЕ секции < 0.45 → 0 staled (НЕ должна быть в выдаче)
            self._make_point("b1", "book-b", 0.1),
            self._make_point("b2", "book-b", 0.0),
        ]

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Основной scroll: все точки
                return (points, None)
            # _batch_resolve_book_titles: пустой ответ (нет title в payload)
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 1  # только book-a
        assert books[0]["book_id"] == "book-a"
        assert books[0]["stale_sections"] == 2
        assert books[0]["total_sections"] == 2
        # total_books отражает только книги со stale секциями
        assert result["total_books"] == 1

    async def test_returns_empty_when_no_stale_books(self, mock_app_state):
        """Все книги имеют 0 stale секций → пустая выдача."""
        points = [
            self._make_point("x1", "book-x", 0.1),
            self._make_point("y1", "book-y", 0.0),
        ]

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (points, None)
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        assert result["books"] == []
        assert result["total_books"] == 0
        assert result["total_stale_sections"] == 0

    # ── R1: title из родительской записи ───────────────────────

    def _make_parent_point(self, kid, title):
        """Mock-точка родительской записи с title в payload."""
        pt = MagicMock()
        pt.id = kid
        pt.payload = {"knowledge_id": kid, "title": title}
        return pt

    async def test_resolves_book_title_from_parent_record(self, mock_app_state):
        """Title книги берётся из payload родительской записи (R1)."""
        points = [
            self._make_point("sec-1", "book-alpha", 0.8),
            self._make_point("sec-2", "book-alpha", 0.6),
        ]
        parent_point = self._make_parent_point("book-alpha", "Alpha Book Title")

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Основной scroll: секции книги
                return (points, None)
            # _batch_resolve_book_titles: родительская запись с title
            return ([parent_point], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        assert "error" not in result
        books = result["books"]
        assert len(books) == 1
        assert books[0]["title"] == "Alpha Book Title"

    async def test_fallback_title_when_parent_not_found(self, mock_app_state):
        """Если родительская запись не найдена → fallback на subject (R1)."""
        points = [
            self._make_point("sec-1", "book-missing", 0.8, domain="devops", subject="engineering"),
        ]

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (points, None)
            # batch resolve: parent not found
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 1
        # Fallback: section_header (empty) → subject
        assert books[0]["title"] == "engineering"

    async def test_title_from_section_header_fallback(self, mock_app_state):
        """Если section_header есть в payload секции — используется как fallback title."""
        points = [
            self._make_point("sec-1", "book-z", 0.8, section_header="Z Book"),
        ]

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (points, None)
            # batch resolve: empty
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 1
        # Fallback: section_header используется (batch resolve не нашёл)
        assert books[0]["title"] == "Z Book"

    async def test_deprecated_parent_title_skipped_for_status(self, mock_app_state):
        """Deprecated-родитель — title всё равно резолвится, но status остаётся от секции (R3 edge case)."""
        points = [
            self._make_point("sec-1", "book-dep", 0.9, status="published"),
        ]
        # Родитель deprecated
        parent_point = self._make_parent_point("book-dep", "Deprecated Book")
        parent_point.payload["status"] = "deprecated"

        call_count = 0

        def mock_scroll(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (points, None)
            # batch resolve: родитель с title
            return ([parent_point], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await review_queue_books({"limit": 10}, mock_app_state)

        books = result["books"]
        assert len(books) == 1
        # Title резолвится из родителя
        assert books[0]["title"] == "Deprecated Book"
        # Статус секции (published) не меняется родительским deprecated
        assert books[0]["status"] == "published"


# ═══════════════════════════════════════════════════════════════
# Фаза 13.14: resolve_quality_issue + knowledge_id + cascade
# ═══════════════════════════════════════════════════════════════


class TestResolveQualityIssueCascade:
    """resolve_quality_issue с knowledge_id и cascade."""

    async def test_direct_knowledge_id_deprecate(self, mock_app_state):
        """knowledge_id без issue_id → прямая операция."""
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"knowledge_id": "book-123", "action": "deprecate", "reason": "test"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["knowledge_id"] == "book-123"
        mock_app_state.qdrant.set_payload.assert_called_once()

    async def test_direct_knowledge_id_restore(self, mock_app_state):
        """knowledge_id + action=restore → прямая операция."""
        mock_app_state.qdrant.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"knowledge_id": "book-123", "action": "restore", "reason": "test"},
            mock_app_state,
        )

        assert result["resolved"] is True
        mock_app_state.qdrant.set_payload.assert_called_once()

    async def test_deprecate_cascade_calls_cascade_set_payload(self, mock_app_state):
        """cascade=True → _cascade_set_payload вызывается через scroll+set_payload."""
        mock_app_state.qdrant.set_payload = MagicMock()
        # Mock scroll для cascade — возвращает 2 дочерние секции
        def _make_child(kid):
            pt = MagicMock()
            pt.payload = {"knowledge_id": kid}
            return pt

        scroll_calls = 0

        def mock_scroll(**kwargs):
            nonlocal scroll_calls
            scroll_calls += 1
            if scroll_calls == 1:
                return ([_make_child("kid-sec-1"), _make_child("kid-sec-2")], None)
            return ([], None)

        mock_app_state.qdrant.scroll = mock_scroll

        result = await resolve_quality_issue(
            {"knowledge_id": "book-123", "action": "deprecate", "cascade": True, "reason": "test"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["cascade_affected"] == 2
        assert "LIFECYCLE cascade" in str(result["side_effects"])

    async def test_either_issue_id_or_knowledge_id_required(self, mock_app_state):
        """Без issue_id и knowledge_id → ошибка."""
        result = await resolve_quality_issue(
            {"action": "deprecate"}, mock_app_state,
        )
        assert result["resolved"] is False
        assert "issue_id or knowledge_id" in result["error"].lower()

    async def test_resolve_still_requires_issue_id(self, mock_app_state):
        """action=resolve всё ещё требует issue_id."""
        result = await resolve_quality_issue(
            {"knowledge_id": "book-123", "action": "resolve"}, mock_app_state,
        )
        assert result["resolved"] is False


# ═══════════════════════════════════════════════════════════════
# Фаза 13.14: _cascade_set_payload helper
# ═══════════════════════════════════════════════════════════════


class TestCascadeSetPayload:
    """_cascade_set_payload — scroll по parent + set_payload на секции (13.15: async)."""

    @pytest.mark.asyncio
    async def test_sets_payload_on_all_children(self):
        """Дочерние секции получают set_payload с переданным payload."""
        mock_qdrant = MagicMock()
        mock_qdrant.set_payload = MagicMock()

        child1 = MagicMock()
        child1.payload = {"knowledge_id": "sec-1"}
        child2 = MagicMock()
        child2.payload = {"knowledge_id": "sec-2"}

        mock_qdrant.scroll = MagicMock(return_value=([child1, child2], None))

        payload = {"status": "deprecated"}
        affected = await _cascade_set_payload(mock_qdrant, "book-x", payload, "deprecated")

        assert affected == 2
        assert mock_qdrant.set_payload.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_empty_children(self):
        """Нет дочерних секций → affected=0."""
        mock_qdrant = MagicMock()
        mock_qdrant.set_payload = MagicMock()
        mock_qdrant.scroll = MagicMock(return_value=([], None))

        payload = {"status": "deprecated"}
        affected = await _cascade_set_payload(mock_qdrant, "book-x", payload, "deprecated")

        assert affected == 0
        mock_qdrant.set_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_paginated_scroll_for_many_children(self):
        """Пагинированный scroll при >1000 секций."""
        mock_qdrant = MagicMock()
        mock_qdrant.set_payload = MagicMock()

        def _child(kid):
            c = MagicMock()
            c.payload = {"knowledge_id": kid}
            return c

        batch1 = [_child(f"sec-{i}") for i in range(5)]
        batch2 = [_child(f"sec-{i}") for i in range(5, 8)]
        mock_qdrant.scroll = MagicMock(side_effect=[
            (batch1, "offset-2"),
            (batch2, None),
        ])

        payload = {"status": "deprecated"}
        affected = await _cascade_set_payload(mock_qdrant, "book-x", payload, "deprecated")

        assert affected == 8
        assert mock_qdrant.set_payload.call_count == 8


# ═══════════════════════════════════════════════════════════════
# 13.18: cancel_quality_scan — отмена активного quality scan
# ═══════════════════════════════════════════════════════════════


class TestCancelQualityScan:
    """cancel_quality_scan — отмена активного скана (13.18)."""

    @pytest.mark.asyncio
    async def test_cancel_no_active_scan_returns_false(self, mock_app_state_with_settings):
        """Нет активного скана (lock не залочен) → cancelled=False."""
        mock_app_state_with_settings.scan_lock.locked.return_value = False

        result = await cancel_quality_scan({}, mock_app_state_with_settings)

        assert result["cancelled"] is False
        assert "no active scan" in result.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_cancel_active_scan_returns_true(self, mock_app_state_with_settings):
        """Активный скан (lock залочен + scan_cancel_event есть) → cancelled=True."""
        mock_app_state_with_settings.scan_lock.locked.return_value = True
        mock_app_state_with_settings.scan_cancel_event = asyncio.Event()

        result = await cancel_quality_scan({}, mock_app_state_with_settings)

        assert result["cancelled"] is True
        assert mock_app_state_with_settings.scan_cancel_event.is_set()

    @pytest.mark.asyncio
    async def test_cancel_active_scan_creates_event_if_missing(self, mock_app_state_with_settings):
        """Активный скан, но scan_cancel_event=None → создаётся и устанавливается."""
        mock_app_state_with_settings.scan_lock.locked.return_value = True
        mock_app_state_with_settings.scan_cancel_event = None

        result = await cancel_quality_scan({}, mock_app_state_with_settings)

        assert result["cancelled"] is True
        assert mock_app_state_with_settings.scan_cancel_event is not None
        assert mock_app_state_with_settings.scan_cancel_event.is_set()


# ═══════════════════════════════════════════════════════════════
# 13.18: _bg_scan с cancel_event — прокидывание в run_scan
# ═══════════════════════════════════════════════════════════════


class TestBgScanCancel:
    """_bg_scan — прокидывает cancel_event в run_scan (13.18)."""

    @pytest.mark.asyncio
    async def test_bg_scan_passes_cancel_event_to_run_scan(self, tmp_path):
        """_bg_scan создаёт cancel_event и передаёт его в run_scan."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        scan_lock = asyncio.Lock()
        cancel_event = asyncio.Event()
        scan_progress = MagicMock()
        scan_state = {"lock": scan_lock, "task_ref": [None]}

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value={
            "files_scanned": 0, "scores_updated": 0,
            "duplicates_detected": 0, "issues_created": 0,
            "review_queue_size": 0,
        })) as mock_run_scan:
            task = asyncio.create_task(
                _bg_scan(
                    scan_id="test-scan-1",
                    knowledge_dir=tmp_path,
                    qdrant_client=None,
                    scan_progress=scan_progress,
                    scan_state=scan_state,
                    cancel_event=cancel_event,
                )
            )
            await task

            # Проверяем, что cancel_event был передан в run_scan
            call_kwargs = mock_run_scan.call_args.kwargs
            assert "cancel_event" in call_kwargs
            assert call_kwargs["cancel_event"] is cancel_event
