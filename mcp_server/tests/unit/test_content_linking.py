"""Unit tests: content/linking.py — parent-child collection linking (#35)."""

from __future__ import annotations

import pytest

from mcp_server.content.linking import (
    slugify,
    make_knowledge_id,
    make_collection_id,
    build_collection,
    CollectionRoot,
    ChildEntry,
)


class TestSlugify:
    """Генерация kebab-case slug с транслитом."""

    def test_simple_english(self):
        assert slugify("Clean Code") == "clean-code"

    def test_cyrillic_transliteration(self):
        slug = slugify("Чистый код")
        assert "chistyi" in slug or "chisty" in slug

    def test_special_chars(self):
        slug = slugify("Hello, World!")
        assert slug == "hello-world"

    def test_max_length(self):
        long_title = "a" * 100
        slug = slugify(long_title, max_len=40)
        assert len(slug) <= 40

    def test_empty(self):
        assert slugify("") == ""


class TestMakeKnowledgeId:
    """Генерация knowledge_id для секции."""

    def test_with_hash(self):
        kid = make_knowledge_id(
            "engineering", "python", "Async Patterns", 3, "abc12345xyz"
        )
        assert kid.startswith("engineering-python-async-patterns-")
        assert len(kid.split("-")[-1]) == 8  # 8 hex chars

    def test_without_hash(self):
        kid = make_knowledge_id(
            "engineering", "python", "Functions", 7
        )
        assert kid == "engineering-python-functions-007"

    def test_single_digit_sequence(self):
        kid = make_knowledge_id("dev", "ops", "Intro", 1)
        assert kid == "dev-ops-intro-001"

    def test_cyrillic_title(self):
        kid = make_knowledge_id("eng", "py", "Функции", 1)
        assert kid.startswith("eng-py-")
        assert "funktsii" in kid or "funkc" in kid


class TestMakeCollectionId:
    """Генерация knowledge_id для root-коллекции."""

    def test_basic(self):
        cid = make_collection_id("engineering", "python", "Clean Code")
        assert cid == "engineering-python-clean-code-collection"

    def test_cyrillic(self):
        cid = make_collection_id("eng", "py", "Чистый код")
        assert cid.endswith("-collection")


class TestBuildCollection:
    """Создание root-коллекции с TOC."""

    def test_basic_collection(self):
        col = build_collection(
            domain="engineering",
            subject="python",
            project="backend",
            title="Async Patterns Book",
            section_titles=["Chapter 1", "Chapter 2", "Chapter 3"],
            section_ids=["eng-py-ch01", "eng-py-ch02", "eng-py-ch03"],
            tags=["async", "book"],
            cross_subjects=["architecture"],
        )

        assert isinstance(col, CollectionRoot)
        assert col.content_type == "collection"
        assert col.domain == "engineering"
        assert col.subject == "python"
        assert col.title == "Async Patterns Book"
        assert len(col.children) == 3
        assert col.children[0]["knowledge_id"] == "eng-py-ch01"
        assert col.children[0]["sequence_number"] == 1
        assert col.children[2]["sequence_number"] == 3

    def test_to_frontmatter(self):
        col = build_collection(
            domain="eng",
            subject="py",
            project=None,
            title="Test",
            section_titles=["S1"],
            section_ids=["eng-py-s1"],
            tags=[],
            cross_subjects=[],
        )
        fm = col.to_frontmatter()
        assert fm.knowledge_id == col.knowledge_id
        assert fm.content_type == "collection"
        assert fm.parent_knowledge_id is None
        assert fm.sequence_number is None
        assert fm.children == col.children

    def test_empty_collection(self):
        col = build_collection(
            domain="eng", subject="py", project=None,
            title="Empty", section_titles=[], section_ids=[],
            tags=[], cross_subjects=[],
        )
        assert col.children == []


class TestChildEntry:
    """Запись-ребёнок коллекции."""

    def test_to_frontmatter(self):
        child = ChildEntry(
            knowledge_id="eng-py-ch01",
            title="Chapter 1",
            body="Content text",
            sequence_number=1,
            parent_knowledge_id="eng-py-collection",
            domain="engineering",
            subject="python",
            tags=["async"],
        )
        fm = child.to_frontmatter()
        assert fm.knowledge_id == "eng-py-ch01"
        assert fm.parent_knowledge_id == "eng-py-collection"
        assert fm.sequence_number == 1
        assert fm.content_type == "book"
        assert fm.domain == "engineering"
