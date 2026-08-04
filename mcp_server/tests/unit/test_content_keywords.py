"""Unit tests: content/keywords.py — TF-IDF keyword extraction (#34, §6.4)."""

from __future__ import annotations

from mcp_server.content.keywords import (
    _is_stop_word,
    _normalize_tag,
    deduplicate_tags,
    extract_keywords,
)


class TestExtractKeywords:
    """TF-IDF extraction — авто-теги на секциях."""

    def test_russian_text(self):
        """Русский текст → keywords через TF-IDF."""
        texts = [
            "Асинхронное программирование в Python использует asyncio и event loop.",
            "Базы данных PostgreSQL обеспечивают надежное хранение и транзакции.",
            "Асинхронный код требует понимания корутин и футур в Python.",
        ]
        result = extract_keywords(texts, top_n=3)
        assert len(result) == 3
        # At least one non-empty result
        assert any(len(kw) > 0 for kw in result)

    def test_english_text(self):
        """Английский текст → keywords."""
        texts = [
            "Machine learning models require careful feature engineering.",
            "Deep learning uses neural networks for pattern recognition.",
            "Feature engineering is critical for traditional ML models.",
        ]
        result = extract_keywords(texts, top_n=3)
        assert len(result) == 3
        assert any(len(kw) > 0 for kw in result)

    def test_empty_input(self):
        """Пустой список → пустые списки."""
        assert extract_keywords([]) == []
        assert extract_keywords([""]) == [[]]

    def test_stop_words_filtered(self):
        """Стоп-слова RU/EN не попадают в keywords."""
        texts = ["И в не на что он она так но да ты к у же вы за бы по."]
        result = extract_keywords(texts, top_n=5)
        # Стоп-слова должны быть отфильтрованы
        keywords = result[0]
        for kw in keywords:
            assert not _is_stop_word(kw)

    def test_kebab_case_normalization(self):
        """Теги нормализуются в kebab-case."""
        texts = ["Программирование на Python"]
        result = extract_keywords(texts, top_n=5)
        # Check that tags are lowercase and clean
        for kw in result[0]:
            assert kw == kw.lower()
            assert " " not in kw

    def test_top_n_limit(self):
        """top_n ограничивает количество тегов."""
        texts = [
            "This is a test document with many different words to extract keywords from."
        ]
        result = extract_keywords(texts, top_n=2)
        assert len(result[0]) <= 2


class TestNormalizeTag:
    """Нормализация в kebab-case тег."""

    def test_cyrillic_transliteration(self):
        assert _normalize_tag("Программирование") == "programmirovanie"

    def test_special_chars_removed(self):
        assert _normalize_tag("hello-world!") == "hello-world"
        assert _normalize_tag("test@case") == "testcase"

    def test_short_word_filtered(self):
        result = _normalize_tag("ab")
        assert len(result) < 3  # too short

    def test_dedup_dashes(self):
        assert _normalize_tag("a---b") == "a-b"


class TestDeduplicateTags:
    """Дедупликация inherited + auto."""

    def test_no_duplicates(self):
        result = deduplicate_tags(["async", "python"], ["clean-code", "book"])
        assert result == ["clean-code", "book", "async", "python"]

    def test_removes_duplicates(self):
        result = deduplicate_tags(["async", "python"], ["async", "book"])
        assert result == ["async", "book", "python"]

    def test_case_insensitive(self):
        result = deduplicate_tags(["Async"], ["async"])
        assert len(result) == 1
        assert result[0] == "async"

    def test_empty_inherited(self):
        result = deduplicate_tags(["tag1", "tag2"], [])
        assert result == ["tag1", "tag2"]

    def test_empty_both(self):
        result = deduplicate_tags([], [])
        assert result == []
