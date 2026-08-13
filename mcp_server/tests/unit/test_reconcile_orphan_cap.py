"""Unit-тесты для reconcile.py — orphan-cap + prefix-skip (P0 A1a).

Проверяют, что коллекция книг с тысячами секций-детей не создаёт
тысячи ложных orphan-issues: секции с общим slug-префиксом пропускаются,
а лимит MAX_ORPHAN_ISSUES_PER_COLLECTION ограничивает число issues.
"""

from __future__ import annotations

from mcp_server.indexing.reconcile import (
    MAX_ORPHAN_ISSUES_PER_COLLECTION,
    _has_common_prefix,
)


class TestHasCommonPrefix:
    """_has_common_prefix — чистая функция, без I/O."""

    def test_same_book_sections_share_prefix(self):
        """Секции одной книги (общий slug, разные хвосты) → True."""
        parent = "universal-fpf-specification-universal-fpf-specification-book-collection"
        child = "universal-fpf-specification-e-10-0-use-this-when-p"
        assert _has_common_prefix(parent, child) is True

    def test_table_of_content_child(self):
        """TOC-секция той же книги → True (не потерянная запись)."""
        parent = "universal-fpf-specification-universal-fpf-specification-book-collection"
        child = "universal-fpf-specification-table-of-content-part-1"
        assert _has_common_prefix(parent, child) is True

    def test_unrelated_kids_no_prefix(self):
        """Разные книги/записи без общего slug → False (реальный orphan)."""
        parent = "engineering-devops-book-collection"
        child = "universal-fpf-specification-e-10-0"
        assert _has_common_prefix(parent, child) is False

    def test_empty_inputs(self):
        """Пустые ID → False."""
        assert _has_common_prefix("", "kid") is False
        assert _has_common_prefix("kid", "") is False
        assert _has_common_prefix("", "") is False

    def test_short_common_prefix_not_enough(self):
        """Слишком короткий общий префикс (< MIN_COMMON) → False."""
        assert _has_common_prefix("ab-kid-1", "xy-kid-2") is False
        assert _has_common_prefix("a-1", "a-2") is False


class TestOrphanCapConstant:
    """Константа лимита определена и разумна."""

    def test_cap_is_small(self):
        """Cap ≤ 10 — не плодит тысячи issues на коллекцию."""
        assert 0 < MAX_ORPHAN_ISSUES_PER_COLLECTION <= 10
