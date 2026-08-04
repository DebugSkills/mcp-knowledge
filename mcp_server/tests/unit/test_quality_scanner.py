"""Unit-тесты для quality/scanner.py — pure functions (4.5)."""

from __future__ import annotations

from datetime import datetime, timezone

import yaml
from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.scanner import (
    _are_dup_candidates,
    _empty_result,
    _parse_frontmatter,
)


def _make_fm(**overrides) -> KnowledgeFrontmatter:
    """Фабрика KnowledgeFrontmatter с разумными defaults."""
    defaults = {
        "knowledge_id": "test-kb-001",
        "domain": "engineering",
        "subject": "python",
        "tags": ["asyncio", "testing", "patterns"],
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 3, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return KnowledgeFrontmatter(**defaults)


class TestParseFrontmatter:
    """Парсинг YAML frontmatter."""

    def test_valid_frontmatter(self):
        md = "---\nknowledge_id: test-001\ndomain: eng\nsubject: rust\ntags:\n  - async\ncreated_at: 2026-08-01T10:00:00+03:00\nupdated_at: 2026-08-03T10:00:00+03:00\n---\n# Content"
        fm = _parse_frontmatter(md, yaml)
        assert fm is not None
        assert fm.knowledge_id == "test-001"
        assert fm.domain == "eng"
        assert fm.tags == ["async"]

    def test_no_frontmatter(self):
        md = "# Just markdown"
        fm = _parse_frontmatter(md, yaml)
        assert fm is None

    def test_unclosed_frontmatter(self):
        md = "---\nknowledge_id: test\n# no close"
        fm = _parse_frontmatter(md, yaml)
        assert fm is None

    def test_invalid_frontmatter(self):
        md = "---\nknowledge_id: test\n---\n# no domain"
        fm = _parse_frontmatter(md, yaml)
        assert fm is None  # Missing domain → Pydantic ValidationError


class TestAreDupCandidates:
    """Эвристика dup-кандидатов."""

    def test_same_subject_tag_overlap(self):
        """Одинаковый subject + tags пересекаются ≥50% → дубль."""
        a = _make_fm(knowledge_id="kb-a", subject="python", tags=["asyncio", "patterns", "testing"])
        b = _make_fm(knowledge_id="kb-b", subject="python", tags=["asyncio", "patterns", "concurrency"])
        # overlap = {asyncio, patterns} = 2, min(3,3)=3, 2/3=0.67 ≥ 0.5
        assert _are_dup_candidates(a, b) is True

    def test_different_subject_not_dup(self):
        """Разный subject → не дубль."""
        a = _make_fm(subject="python")
        b = _make_fm(subject="rust")
        assert _are_dup_candidates(a, b) is False

    def test_no_tag_overlap(self):
        """Tags не пересекаются → не дубль."""
        a = _make_fm(knowledge_id="kb-a", tags=["asyncio"])
        b = _make_fm(knowledge_id="kb-b", tags=["borrow-checker"])
        assert _are_dup_candidates(a, b) is False

    def test_below_threshold_overlap(self):
        """Пересечение <50% → не дубль."""
        a = _make_fm(knowledge_id="kb-a", tags=["asyncio", "patterns", "testing", "fastapi"])
        b = _make_fm(knowledge_id="kb-b", tags=["asyncio", "borrow-checker", "traits"])
        # overlap = {asyncio} = 1, min(4,3)=3, 1/3=0.33 < 0.5
        assert _are_dup_candidates(a, b) is False

    def test_empty_tags_not_dup(self):
        """Пустые tags → не дубль."""
        a = _make_fm(knowledge_id="kb-a", tags=[])
        b = _make_fm(knowledge_id="kb-b", tags=[])
        assert _are_dup_candidates(a, b) is False


class TestEmptyResult:
    """_empty_result возвращает нулевые метрики."""

    def test_all_zero(self):
        result = _empty_result()
        assert result["files_scanned"] == 0
        assert result["scores_updated"] == 0
        assert result["duplicates_detected"] == 0
        assert result["issues_created"] == 0
        assert result["review_queue_size"] == 0

    def test_keys_match_expected(self):
        result = _empty_result()
        expected_keys = {
            "files_scanned",
            "scores_updated",
            "duplicates_detected",
            "issues_created",
            "review_queue_size",
        }
        assert set(result.keys()) == expected_keys
