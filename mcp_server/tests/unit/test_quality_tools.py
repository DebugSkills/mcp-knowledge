"""Unit-тесты для quality tools: list_quality_issues, resolve_quality_issue, run_quality_scan, review_queue.

Фаза 13 (v1.0): закрывает gap-анализ — quality tools (list_quality_issues, resolve_quality_issue, run_quality_scan)
были 0 coverage; review_queue unit-only (scroll() missing в QdrantClient-обёртке).

Все тесты не требуют Qdrant/Ollama — используют tempdir + mocks.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.quality.issues import create_issue, set_store_dir
from mcp_server.tools.quality import (
    list_quality_issues,
    resolve_quality_issue,
    review_queue,
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
    state.qdrant_client = MagicMock()
    state.store = MagicMock()
    return state


@pytest.fixture
def mock_app_state_with_settings():
    """app_state mock с settings.knowledge_dir."""
    state = MagicMock()
    state.qdrant_client = MagicMock()
    state.store = MagicMock()
    state.settings = SimpleNamespace(knowledge_dir="/tmp/test-knowledge")
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
        mock_app_state.qdrant_client.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "deprecate", "reason": "Obsolete"},
            mock_app_state,
        )

        assert result["resolved"] is True
        assert result["status"] == "resolved"
        # set_payload должен быть вызван
        mock_app_state.qdrant_client.set_payload.assert_called_once()
        # side_effects содержит упоминание deprecated
        assert len(result["side_effects"]) >= 1
        assert any("deprecated" in se.lower() for se in result["side_effects"])

    async def test_merge_action_requires_target_id(self, quality_tempdir, mock_app_state):
        """action=merge без target_id → error."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test merge")
        mock_app_state.qdrant_client.set_payload = MagicMock()

        result = await resolve_quality_issue(
            {"issue_id": issue.issue_id, "action": "merge", "reason": "Dup"},
            mock_app_state,
        )

        assert result["resolved"] is False
        assert "target_id" in result.get("error", "").lower()

    async def test_merge_action_with_target_id(self, quality_tempdir, mock_app_state):
        """action=merge с target_id → success."""
        issue = create_issue("duplicate", "kid-1", "warn", "Test merge ok")
        mock_app_state.qdrant_client.set_payload = MagicMock()

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
        mock_app_state.qdrant_client.set_payload = MagicMock()

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
    """run_quality_scan — unit+integration: mock run_scan, проверка проброса metrics."""

    @pytest.mark.asyncio
    async def test_run_scan_calls_scanner_with_mock(self, mock_app_state_with_settings):
        """Mock run_scan → verify scanned=True, metrics проброшены."""
        mock_metrics = {
            "files_scanned": 10,
            "review_queue_size": 3,
            "duplicates_detected": 2,
            "issues_created": 5,
        }

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, mock_app_state_with_settings)

        assert result["scanned"] is True
        assert result["metrics"] == mock_metrics

    @pytest.mark.asyncio
    async def test_run_scan_with_domain_param(self, mock_app_state_with_settings):
        """Параметр domain пробрасывается (не фильтруется tools-уровнем)."""
        mock_metrics = {"files_scanned": 2, "review_queue_size": 1, "duplicates_detected": 0, "issues_created": 1}

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)) as mock_run:
            result = await run_quality_scan({"domain": "engineering"}, mock_app_state_with_settings)

        assert result["scanned"] is True
        mock_run.assert_awaited_once()
        # run_scan вызывается с knowledge_dir
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["knowledge_dir"] == "/tmp/test-knowledge"

    @pytest.mark.asyncio
    async def test_run_scan_no_settings_graceful(self, mock_app_state):
        """app_state без settings → knowledge_dir=None, run_scan всё равно вызывается."""
        mock_metrics = {"files_scanned": 0, "review_queue_size": 0, "duplicates_detected": 0, "issues_created": 0}

        with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(return_value=mock_metrics)):
            result = await run_quality_scan({}, mock_app_state)

        assert result["scanned"] is True

    @pytest.mark.asyncio
    async def test_run_scan_handles_exception(self, mock_app_state_with_settings):
        """Scanner бросает исключение → scanned=False, error."""
        with patch(
            "mcp_server.quality.scanner.run_scan",
            new=AsyncMock(side_effect=RuntimeError("Disk full")),
        ):
            result = await run_quality_scan({}, mock_app_state_with_settings)

        assert result["scanned"] is False
        assert "error" in result
        assert "Disk full" in result["error"]


# ═══════════════════════════════════════════════════════════════
# Step 8: review_queue (unit-only)
# ═══════════════════════════════════════════════════════════════


class TestReviewQueue:
    """review_queue — unit-only (mock qdrant_client.scroll).

    QdrantClient-обёртка НЕ имеет scroll() (только scroll_unique_values) → E2E невозможен.
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

        mock_app_state.qdrant_client.scroll = MagicMock(return_value=(points, None))

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

        mock_app_state.qdrant_client.scroll = MagicMock(return_value=(points, None))

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

        mock_app_state.qdrant_client.scroll = MagicMock(return_value=(points, None))

        result = await review_queue({"limit": 1}, mock_app_state)
        assert len(result["queue"]) == 1
        assert result["queue"][0]["staleness_score"] == 0.9

    async def test_review_queue_with_domain_filter(self, mock_app_state):
        """Фильтр domain → scroll вызывается с domain-условием."""
        mock_app_state.qdrant_client.scroll = MagicMock(return_value=([], None))

        result = await review_queue({"domain": "engineering", "limit": 5}, mock_app_state)

        assert result["queue"] == []
        # Проверяем что scroll был вызван
        mock_app_state.qdrant_client.scroll.assert_called_once()

    async def test_review_queue_handles_exception(self, mock_app_state):
        """qdrant_client.scroll бросает исключение → error, пустая queue."""
        mock_app_state.qdrant_client.scroll = MagicMock(
            side_effect=ConnectionError("Qdrant down")
        )

        result = await review_queue({"limit": 10}, mock_app_state)

        assert result["queue"] == []
        assert "error" in result
        assert "Qdrant down" in result["error"]

    async def test_review_queue_empty_scroll(self, mock_app_state):
        """Пустой scroll → пустая queue."""
        mock_app_state.qdrant_client.scroll = MagicMock(return_value=([], None))

        result = await review_queue({"limit": 10}, mock_app_state)

        assert result["queue"] == []
        assert result["total_in_queue"] == 0
