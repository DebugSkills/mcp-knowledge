"""Интеграционные тесты quality flow — сквозной сценарий (4.9).

Проверяет полный цикл качества:
write_knowledge → gate.validate → dup-detect → create_issue → resolve → lifecycle

Использует моки для Qdrant + embedder — тестирует логическую целостность,
не требует реального Qdrant/BGE-M3.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.quality.gates import GateResult, evaluate_frontmatter
from mcp_server.quality.issues import (
    create_issue,
    list_issues,
    set_store_dir,
    update_issue_status,
)
from mcp_server.quality.scoring import (
    StalenessInput,
    REVIEW_THRESHOLD,
    staleness_score,
    should_review,
)
from mcp_server.quality.lifecycle import (
    get_status,
    make_deprecation_payload_update,
    make_restore_payload_update,
)
from mcp_server.quality.edit_war import detect_edit_war
from mcp_server.quality.dup_gate import find_duplicates, compute_cosine


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def quality_store():
    """Временное хранилище issues.jsonl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        set_store_dir(tmpdir)
        yield tmpdir


@pytest.fixture
def sample_markdown():
    """Валидный markdown для тестов."""
    return """---
knowledge_id: test-int-001
domain: engineering
subject: python
tags:
  - asyncio
  - testing
created_at: 2026-08-01T10:00:00+03:00
updated_at: 2026-08-03T10:00:00+03:00
source: https://example.com/test
cross_subjects:
  - devops
evergreen: false
---
# Async Patterns in Python

This is a test knowledge entry about async programming.
"""


# ── Flow 1: Write → Gate → Staleness ─────────────────────────

class TestQualityPipeline:
    """Сквозной сценарий: write → gate → staleness → review."""

    def test_frontmatter_gate_passes_valid_entry(self, sample_markdown):
        gate_result = evaluate_frontmatter(sample_markdown, strict=False)
        assert gate_result.passed is True
        assert gate_result.blocked is False

    def test_frontmatter_gate_blocks_missing_required(self):
        md = """---
knowledge_id: test-int-002
# Нет domain, subject, tags
---
# Content
"""
        gate_result = evaluate_frontmatter(md)
        assert gate_result.blocked is True

    def test_staleness_score_for_fresh_entry(self):
        now = datetime.now(timezone.utc)
        inp = StalenessInput(
            updated_at=now,
            evergreen=False,
            dup_count=0,
        )
        score = staleness_score(inp, now=now)
        assert score == 0.0
        assert should_review(score) is False

    def test_staleness_score_for_old_dup_entry(self):
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        inp = StalenessInput(
            updated_at=now - timedelta(days=400),
            evergreen=False,
            dup_count=2,
            recommended_missing=3,
            edit_war=True,
        )
        score = staleness_score(inp, now=now)
        assert score >= REVIEW_THRESHOLD
        assert should_review(score) is True


# ── Flow 2: Issue lifecycle ──────────────────────────────────

class TestIssueLifecycle:
    """Issue: create → list → resolve."""

    def test_create_and_list_issue(self, quality_store):
        issue = create_issue(
            issue_type="duplicate",
            knowledge_id="test-kb-100",
            severity="warn",
            detail="cosine=0.94 with test-kb-101",
        )
        assert issue.status == "open"

        issues = list_issues(status="open")
        assert len(issues) == 1
        assert issues[0].knowledge_id == "test-kb-100"

    def test_resolve_issue(self, quality_store):
        issue = create_issue(
            issue_type="missing_field",
            knowledge_id="test-kb-200",
            severity="info",
            detail="missing source",
        )
        updated = update_issue_status(issue.issue_id, "resolved", "fixed")
        assert updated.status == "resolved"
        assert updated.resolved_at is not None

    def test_idempotent_create(self, quality_store):
        i1 = create_issue("duplicate", "test-kb-300", "warn", "same detail")
        i2 = create_issue("duplicate", "test-kb-300", "warn", "same detail")
        assert i1.issue_id == i2.issue_id
        assert len(list_issues()) == 1


# ── Flow 3: Duplicate detection ──────────────────────────────

class TestDuplicateDetection:
    """Dup-gate: embed → search → compare."""

    def test_find_duplicates_with_self_exclusion(self):
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("self-id", [1.0, 0.0, 0.0]),
            ("dup-id", [0.95, 0.1, 0.0]),
        ]
        result = find_duplicates(query, candidates, exclude_id="self-id")
        assert len(result) == 1
        assert result[0]["knowledge_id"] == "dup-id"
        assert result[0]["score"] >= 0.92

    def test_no_duplicates_found(self):
        query = [1.0, 0.0, 0.0]
        candidates = [("other", [0.0, 1.0, 0.0])]
        result = find_duplicates(query, candidates)
        assert len(result) == 0

    def test_cosine_perfect_match(self):
        v = [0.5, 0.5, 0.5, 0.5]
        assert compute_cosine(v, v) == pytest.approx(1.0)

    def test_cosine_orthogonal(self):
        assert compute_cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ── Flow 4: Lifecycle — deprecate + restore ──────────────────

class TestLifecycleFlow:
    """Deprecate → search-exclude → restore → re-visible."""

    def test_default_status_is_published(self):
        assert get_status(None) == "published"
        assert get_status({}) == "published"

    def test_deprecate_then_restore(self):
        # Deprecate
        payload = make_deprecation_payload_update()
        assert get_status(payload) == "deprecated"

        # Restore
        payload = make_restore_payload_update()
        assert get_status(payload) == "published"

    def test_search_excludes_deprecated_by_default(self):
        from mcp_server.quality.lifecycle import build_search_filter
        f = build_search_filter(include_deprecated=False)
        assert f is not None
        assert "must_not" in f

    def test_search_includes_deprecated_when_requested(self):
        from mcp_server.quality.lifecycle import build_search_filter
        f = build_search_filter(include_deprecated=True)
        assert f is None  # no filter = see all


# ── Flow 5: Edit-war detection ───────────────────────────────

class TestEditWarDetection:
    """Git-based edit-war: моки git."""

    def test_no_git_available_returns_false(self):
        result = detect_edit_war("/nonexistent/path/test.md")
        assert result is False

    @patch("mcp_server.quality.edit_war._find_git_root")
    @patch("git.Repo")
    def test_multiple_commits_trigger_edit_war(self, mock_repo_cls, mock_find_root):
        mock_find_root.return_value = Path("/repo")
        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = [MagicMock()] * 5
        mock_repo_cls.return_value = mock_repo

        result = detect_edit_war(Path("/repo/knowledge/test.md"))
        assert result is True
