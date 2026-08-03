"""Unit-тесты для quality/dup_gate.py — semantic duplicate detection (4.3).

Тестирует чистые функции: compute_cosine, find_duplicates, _extract_representative_text.
check_duplicates() требует эмбеддер + Qdrant — тестируется в интеграционных тестах (4.9).
"""

from __future__ import annotations

import pytest
from mcp_server.quality.dup_gate import (
    DUP_SIMILARITY_THRESHOLD,
    compute_cosine,
    find_duplicates,
    _extract_representative_text,
)


class TestCosineSimilarity:
    """Вычисление косинусного сходства."""

    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert compute_cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert compute_cosine(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        assert compute_cosine(a, b) == pytest.approx(-1.0)

    def test_similar_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 1.9, 3.0]
        score = compute_cosine(a, b)
        assert score > 0.95  # очень близкие

    def test_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert compute_cosine(a, b) == 0.0

    def test_empty_vectors(self):
        assert compute_cosine([], []) == 0.0

    def test_dimension_mismatch(self):
        with pytest.raises(ValueError):
            compute_cosine([1.0, 2.0], [1.0])

    def test_high_dimensional(self):
        """768-мерные векторы (BGE-M3 размерность)."""
        import random
        random.seed(42)
        a = [random.random() for _ in range(768)]
        b = [a[i] + random.uniform(-0.01, 0.01) for i in range(768)]
        score = compute_cosine(a, b)
        assert score > 0.99


class TestFindDuplicates:
    """Поиск дубликатов среди кандидатов."""

    def test_no_duplicates_below_threshold(self):
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("id1", [0.0, 1.0, 0.0]),  # orthogonal → 0.0
            ("id2", [0.0, 0.0, 1.0]),  # orthogonal → 0.0
        ]
        result = find_duplicates(query, candidates)
        assert len(result) == 0

    def test_one_duplicate_above_threshold(self):
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("id1", [0.99, 0.01, 0.0]),  # cosine ≈ 0.9999 → дубль
            ("id2", [0.0, 1.0, 0.0]),     # cosine = 0
        ]
        result = find_duplicates(query, candidates)
        assert len(result) == 1
        assert result[0]["knowledge_id"] == "id1"
        assert result[0]["score"] > DUP_SIMILARITY_THRESHOLD

    def test_self_exclusion(self):
        """При update_entry исключаем свой knowledge_id."""
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("self", [1.0, 0.0, 0.0]),   # это я → исключаем
            ("dup", [0.95, 0.0, 0.0]),    # дубль
        ]
        result = find_duplicates(query, candidates, exclude_id="self")
        assert len(result) == 1
        assert result[0]["knowledge_id"] == "dup"

    def test_sorted_by_score_desc(self):
        """Результаты сортируются по score DESC."""
        query = [1.0, 0.0]
        candidates = [
            ("low", [0.5, 0.5]),    # cosine ≈ 0.707
            ("high", [0.9, 0.1]),   # cosine ≈ 0.907
            ("mid", [0.7, 0.3]),    # cosine ≈ 0.764
        ]
        result = find_duplicates(query, candidates, threshold=0.0)
        assert result[0]["knowledge_id"] == "high"
        assert result[1]["knowledge_id"] == "mid"
        assert result[2]["knowledge_id"] == "low"

    def test_custom_threshold(self):
        """Повышенный порог → меньше дублей."""
        query = [1.0, 0.0]
        candidates = [
            ("close", [0.85, 0.5]),  # cosine ≈ 0.862
        ]
        # Порог 0.85 → дубль (0.862 > 0.85)
        assert len(find_duplicates(query, candidates, threshold=0.85)) == 1
        # Порог 0.95 → не дубль (0.862 < 0.95)
        assert len(find_duplicates(query, candidates, threshold=0.95)) == 0

    def test_empty_candidates(self):
        result = find_duplicates([1.0, 0.0], [])
        assert result == []

    def test_duplicate_has_rounded_score(self):
        """Score округлён до 4 знаков."""
        query = [1.0, 0.0, 0.0]
        candidates = [("dup", [0.987654321, 0.0, 0.0])]
        result = find_duplicates(query, candidates)
        # 0.987654321 → rounded to 4dp
        assert len(str(result[0]["score"]).split(".")[1]) <= 4


class TestExtractRepresentativeText:
    """Извлечение заголовка + первого параграфа."""

    def test_extracts_title_and_first_paragraph(self):
        md = "---\nknowledge_id: test\ndomain: eng\nsubject: rust\n---\n# Async Patterns in Rust\n\nThis guide covers async patterns using tokio.\n\n## Details\n\nMore content here."
        text = _extract_representative_text(md)
        assert "Async Patterns in Rust" in text
        assert "tokio" in text

    def test_no_frontmatter(self):
        md = "# Simple Title\n\nJust a paragraph."
        text = _extract_representative_text(md)
        assert "Simple Title" in text
        assert "Just a paragraph" in text

    def test_empty_content(self):
        text = _extract_representative_text("")
        assert text == ""

    def test_code_blocks_skipped(self):
        """Блоки кода не попадают в репрезентативный текст."""
        md = "# Title\n\n```python\nprint('hello')\n```\n\nReal paragraph here."
        text = _extract_representative_text(md)
        assert "print" not in text
        assert "Real paragraph" in text


class TestDefaults:
    """Константы по умолчанию."""

    def test_threshold(self):
        assert DUP_SIMILARITY_THRESHOLD == 0.92
