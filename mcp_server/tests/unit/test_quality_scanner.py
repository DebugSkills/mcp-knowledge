"""Unit-тесты для quality/scanner.py — pure functions + .trash filter + cancel (4.5+13.18)."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.scanner import (
    _are_dup_candidates,
    _empty_result,
    _parse_frontmatter,
    _scan_filesystem,
    run_scan,
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


# ═══════════════════════════════════════════════════════════════
# 13.18: _scan_filesystem skips .trash/
# ═══════════════════════════════════════════════════════════════


class TestScanFilesystemSkipsTrash:
    """_scan_filesystem — исключает файлы из .trash/."""

    @pytest.mark.asyncio
    async def test_scan_filesystem_skips_trash(self):
        """knowledge/x.md — возвращается; knowledge/.trash/stale.md — НЕТ."""
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge"
            knowledge_dir.mkdir()
            trash_dir = knowledge_dir / ".trash"
            trash_dir.mkdir()

            # Создаём валидный .md файл в корне knowledge/
            (knowledge_dir / "valid_entry.md").write_text(
                "---\nknowledge_id: kid-1\ndomain: eng\nsubject: test\ntags: [t]\n"
                "created_at: 2026-08-01T10:00:00+03:00\n"
                "updated_at: 2026-08-03T10:00:00+03:00\n---\n# Valid\n"
            )

            # Создаём .md файл в .trash/ (должен быть пропущен)
            (trash_dir / "deleted_entry.md").write_text(
                "---\nknowledge_id: kid-2\ndomain: eng\nsubject: test\ntags: [t]\n"
                "created_at: 2026-08-01T10:00:00+03:00\n"
                "updated_at: 2026-08-03T10:00:00+03:00\n---\n# Deleted\n"
            )

            # Создаём другой подкаталог (не .trash) — файлы должны быть видны
            sub_dir = knowledge_dir / "engineering"
            sub_dir.mkdir()
            (sub_dir / "nested_entry.md").write_text(
                "---\nknowledge_id: kid-3\ndomain: eng\nsubject: test\ntags: [t]\n"
                "created_at: 2026-08-01T10:00:00+03:00\n"
                "updated_at: 2026-08-03T10:00:00+03:00\n---\n# Nested\n"
            )

            # Создаём .trash/ внутри подкаталога (имитация soft-delete вложенной записи)
            nested_trash = sub_dir / ".trash"
            nested_trash.mkdir()
            (nested_trash / "nested_deleted.md").write_text(
                "---\nknowledge_id: kid-4\ndomain: eng\nsubject: test\ntags: [t]\n"
                "created_at: 2026-08-01T10:00:00+03:00\n"
                "updated_at: 2026-08-03T10:00:00+03:00\n---\n# NestedDeleted\n"
            )

            entries = await _scan_filesystem(knowledge_dir)

            knowledge_ids = {entry[1].knowledge_id for entry in entries}
            assert "kid-1" in knowledge_ids, "valid_entry.md should be scanned"
            assert "kid-3" in knowledge_ids, "nested_entry.md should be scanned"
            assert "kid-2" not in knowledge_ids, ".trash/deleted_entry.md should be skipped"
            assert "kid-4" not in knowledge_ids, "engineering/.trash/nested_deleted.md should be skipped"
            assert len(entries) == 2


# ═══════════════════════════════════════════════════════════════
# 13.18: run_scan cancel_event — прерывание при отмене
# ═══════════════════════════════════════════════════════════════


class TestRunScanCancel:
    """run_scan — проверка cancel_event между фазами (13.18)."""

    @pytest.mark.asyncio
    async def test_run_scan_cancel_event_returns_partial_metrics(self):
        """cancel_event установлен → run_scan прерывается рано, возвращает partial metrics."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp)
            # Создаём несколько валидных .md файлов
            for i in range(5):
                (knowledge_dir / f"entry_{i}.md").write_text(
                    f"---\nknowledge_id: kid-{i}\ndomain: eng\nsubject: test\ntags: [t]\n"
                    "created_at: 2026-08-01T10:00:00+03:00\n"
                    f"updated_at: 2026-08-03T10:00:00+03:00\n---\n# Entry {i}\n"
                )

# Second occurrence (line ~230)
            for i in range(3):
                (knowledge_dir / f"entry_{i}.md").write_text(
                    f"---\nknowledge_id: kid-{i}\ndomain: eng\nsubject: test\ntags: [t]\n"
                    "created_at: 2026-08-01T10:00:00+03:00\n"
                    f"updated_at: 2026-08-03T10:00:00+03:00\n---\n# Entry {i}\n"
                )

            # Устанавливаем cancel_event ДО запуска — скан должен прерваться после _scan_filesystem
            cancel_event = asyncio.Event()
            cancel_event.set()

            result = await run_scan(
                knowledge_dir=knowledge_dir,
                qdrant_client=None,  # без Qdrant — только scoring
                cancel_event=cancel_event,
            )

            # _scan_filesystem успел выполниться (files_scanned = 5)
            assert result["files_scanned"] == 5
            # scoring должен быть пропущен (cancel после _scan_filesystem)
            assert result["scores_updated"] == 0
            assert result["review_queue_size"] == 0

    @pytest.mark.asyncio
    async def test_run_scan_no_cancel_event_runs_fully(self):
        """Без cancel_event → run_scan проходит все фазы."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp)
            for i in range(3):
                (knowledge_dir / f"entry_{i}.md").write_text(
                    f"---\nknowledge_id: kid-{i}\ndomain: eng\nsubject: test\ntags: [t]\n"
                    "created_at: 2026-08-01T10:00:00+03:00\n"
                    f"updated_at: 2026-08-03T10:00:00+03:00\n---\n# Entry {i}\n"
                )

            result = await run_scan(
                knowledge_dir=knowledge_dir,
                qdrant_client=None,
                cancel_event=None,  # без отмены
            )

            assert result["files_scanned"] == 3
            # Без Qdrant — scores не пишутся, но scoring выполнен
            assert result["scores_updated"] == 0  # qdrant_client=None
            # dup_scan и issues должны отработать
            assert isinstance(result["duplicates_detected"], int)
            assert isinstance(result["issues_created"], int)


# ═══════════════════════════════════════════════════════════════
# P0 (A2a/A3a): embedding-dup fallback + auto-clear
# ═══════════════════════════════════════════════════════════════


class TestScanDupPairsP0:
    """_scan_dup_pairs — embedding-путь и fallback на теговую эвристику (P0)."""

    def _mk_entry(self, kid: str, subject: str, tags: list[str]):
        fm = _make_fm(knowledge_id=kid, subject=subject, tags=tags)
        return (Path(f"/tmp/{kid}.md"), fm, 0.1)

    def test_fallback_tag_heuristic_when_embedder_none(self):
        """embedder=None → теговая эвристика (не crash)."""
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk_entry("kid-a", "devops", ["ai", "automation", "teaching"])
        b = self._mk_entry("kid-b", "devops", ["ai", "automation", "teaching", "extra"])
        dup_count, dup_map = _scan_dup_pairs([a, b])
        assert dup_count == 1  # tags overlap ≥50% → дубль по эвристике
        assert "kid-a" in dup_map and "kid-b" in dup_map

    def test_embedder_failure_falls_back_to_heuristic(self):
        """embedder.embed_sync бросает → graceful fallback на теговую эвристику."""
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk_entry("kid-a", "devops", ["ai", "automation"])
        b = self._mk_entry("kid-b", "devops", ["ai", "automation", "extra"])

        class BrokenEmbedder:
            def embed_sync(self, texts):
                raise RuntimeError("embed unavailable")

        dup_count, _ = _scan_dup_pairs([a, b], embedder=BrokenEmbedder())
        assert dup_count == 1  # fallback сработал

    def test_toc_sections_skipped(self):
        """TOC-секции не считаются дублями (существующий guard сохранён)."""
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk_entry("book-table-of-content-part-1", "devops", ["ai", "toc"])
        b = self._mk_entry("book-table-of-content-part-2", "devops", ["ai", "toc"])
        dup_count, _ = _scan_dup_pairs([a, b])
        assert dup_count == 0


class TestAutoClearStaleIssues:
    """_auto_clear_stale_issues — закрытие устаревших missing_field issues (A3a)."""

    @pytest.fixture
    def issues_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    def test_clears_stale_issues(self, issues_tempdir):
        """Запись с score < threshold → её open missing_field issues закрываются."""
        from mcp_server.quality.issues import create_issue, list_issues
        from mcp_server.quality.scanner import _auto_clear_stale_issues

        # Создаём open missing_field issue для записи, которая теперь не проблемна
        create_issue(
            "missing_field", "kid-fixed", "warn",
            "Staleness score 0.5 >= 0.45 — needs review",
        )
        # Свежая запись: score 0.1 < 0.45
        fm = _make_fm(knowledge_id="kid-fixed")
        scored = [(Path("/tmp/kid-fixed.md"), fm, 0.1)]

        cleared = _auto_clear_stale_issues(scored)
        assert cleared == 1

        open_issues = list_issues(status="open", limit=50)
        assert all(i.knowledge_id != "kid-fixed" for i in open_issues)

    def test_keeps_problematic_issues(self, issues_tempdir):
        """Запись с score ≥ threshold → её issues НЕ закрываются."""
        from mcp_server.quality.issues import create_issue, list_issues
        from mcp_server.quality.scanner import _auto_clear_stale_issues

        create_issue(
            "missing_field", "kid-still-bad", "warn",
            "Staleness score 0.5 >= 0.45 — needs review",
        )
        fm = _make_fm(knowledge_id="kid-still-bad")
        scored = [(Path("/tmp/kid-still-bad.md"), fm, 0.5)]

        cleared = _auto_clear_stale_issues(scored)
        assert cleared == 0

        open_issues = list_issues(status="open", limit=50)
        assert len(open_issues) == 1
        assert open_issues[0].knowledge_id == "kid-still-bad"


class TestRepresentativeText:
    """_representative_text — репрезентативный текст для embedding-dup (P0)."""

    def test_uses_available_fields(self):
        """Использует knowledge_id + subject + tags (без поля title)."""
        from mcp_server.quality.scanner import _representative_text
        fm = _make_fm(
            knowledge_id="kid-test-001",
            subject="devops",
            tags=["ai", "automation"],
        )
        text = _representative_text(fm)
        assert "kid-test-001" in text
        assert "devops" in text
        assert "ai" in text
        assert "automation" in text

    def test_no_crash_with_empty_tags(self):
        """Пустые tags → не падает."""
        from mcp_server.quality.scanner import _representative_text
        fm = _make_fm(knowledge_id="kid-test-001", subject="devops", tags=[])
        text = _representative_text(fm)
        assert "kid-test-001" in text
        assert "devops" in text


# ═══════════════════════════════════════════════════════════════
# Фаза 1 dedup: negation guard + content_hash + skip-deprecated
# ═══════════════════════════════════════════════════════════════


class TestNegationGuard:
    """has_negation_pattern — защита от антоним-FP (Фаза 1)."""

    def test_antonyms_detected(self):
        from mcp_server.quality.scanner import has_negation_pattern
        # Контрпример Critic: «что ИИ любит» ≈ «что ИИ НЕ любит»
        assert has_negation_pattern(
            "chto-lyubit-ai", "chto-ne-lyubit-ai"
        ) is True

    def test_similar_slugs_not_flagged(self):
        from mcp_server.quality.scanner import has_negation_pattern
        assert has_negation_pattern("kid-a-1", "kid-a-2") is False
        assert has_negation_pattern("engineering-mcp-1", "engineering-mcp-2") is False

    def test_empty_inputs(self):
        from mcp_server.quality.scanner import has_negation_pattern
        assert has_negation_pattern("", "kid") is False
        assert has_negation_pattern("kid", "") is False


class TestContentHash:
    """content_hash/нормализация тела (Фаза 1)."""

    def test_extract_body(self):
        from mcp_server.quality.scanner import _extract_body
        md = "---\nknowledge_id: kid\n---\n# Body\ncontent"
        assert _extract_body(md) == "\n# Body\ncontent"

    def test_normalize_and_hash(self):
        from mcp_server.quality.scanner import _content_body_hash, _normalize_body
        body = "  # Title\n\n\n\ncontent  "
        norm = _normalize_body(body)
        assert "\n\n\n" not in norm  # пустые строки схлопнуты
        assert norm == norm.strip()
        h1 = _content_body_hash("text\n\n\nmore")
        h2 = _content_body_hash("text\n\nmore")
        assert h1 == h2  # нормализация → одинаковый hash
        assert len(h1) == 64  # Фаза 3 (0e): полный sha256 (было 16 hex)

    def test_hash_stable_for_identical(self):
        from mcp_server.quality.scanner import _content_body_hash
        assert _content_body_hash("same body") == _content_body_hash("same body")
        assert _content_body_hash("same body") != _content_body_hash("other")


class TestSkipDeprecated:
    """_scan_dup_pairs — skip deprecated (Фаза 1 re-detection loop fix)."""

    def _mk(self, kid: str, subject: str = "devops"):
        fm = _make_fm(knowledge_id=kid, subject=subject, tags=["ai", "auto"])
        return (Path(f"/tmp/{kid}.md"), fm, 0.1)

    def test_deprecated_pair_skipped(self):
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk("kid-a")
        b = self._mk("kid-b")
        dup_count, _ = _scan_dup_pairs(
            [a, b],
            deprecated_kids={"kid-b"},
        )
        assert dup_count == 0  # пара с deprecated пропущена

    def test_no_deprecated_normal_scan(self):
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk("kid-a")
        b = self._mk("kid-b")
        dup_count, _ = _scan_dup_pairs([a, b], deprecated_kids=set())
        assert dup_count == 1  # теговая эвристика (embedder None)


# ═══════════════════════════════════════════════════════════════
# code-2026-09-24-011 (В3): 5.5 snapshot + cancel + batched-лог
# ═══════════════════════════════════════════════════════════════


class TestAutoClearSnapshot:
    """5.5 — snapshot-эквивалентность, cancel-aware, batched-лог (трасса 011)."""

    @pytest.fixture
    def issues_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    @staticmethod
    def _reference_per_kid(scored):
        """Эталонная per-kid реализация (старый код 5.5 до фикса)."""
        from mcp_server.quality.issues import list_issue_ids
        from mcp_server.quality.scoring import REVIEW_THRESHOLD

        to_close = []
        for _f, fm, score in scored:
            if score < REVIEW_THRESHOLD:
                to_close.extend(
                    list_issue_ids(
                        types=["missing_field"], status="open",
                        knowledge_id=fm.knowledge_id,
                    )
                )
        return to_close

    def _mixed_fixture(self):
        """Фикстура AC2: open/resolved/ignored × missing_field/sensitive/duplicate × kids."""
        from mcp_server.quality.issues import create_issue, update_issue_status

        # Непроблемные kids (score 0.1 < 0.45) с open missing_field → закрывать
        for i in range(3):
            create_issue("missing_field", f"kid-ok-{i}", "warn", f"score high {i}")
        # Проблемный kid (score 0.5 >= 0.45) → НЕ закрывать
        create_issue("missing_field", "kid-bad", "warn", "score high")
        # Resolved missing_field у непроблемного → уже закрыт, не трогаем
        r = create_issue("missing_field", "kid-ok-0", "warn", "already resolved branch")
        update_issue_status(r.issue_id, "resolved")
        # Чужие типы у непроблемного → НЕ закрывать
        create_issue("sensitive", "kid-ok-1", "warn", "sensitive markers")
        create_issue("duplicate", "kid-ok-2", "warn", "possible duplicate")
        # Ignored missing_field у непроблемного → не open, не трогаем
        ig = create_issue("missing_field", "kid-ok-2", "warn", "ignored branch")
        update_issue_status(ig.issue_id, "ignored")
        # Непроблемный kid без issues
        fm_entries = [
            (Path("/tmp/kid-ok-0.md"), _make_fm(knowledge_id="kid-ok-0"), 0.1),
            (Path("/tmp/kid-ok-1.md"), _make_fm(knowledge_id="kid-ok-1"), 0.2),
            (Path("/tmp/kid-ok-2.md"), _make_fm(knowledge_id="kid-ok-2"), 0.0),
            (Path("/tmp/kid-bad.md"), _make_fm(knowledge_id="kid-bad"), 0.5),
            (Path("/tmp/kid-clean.md"), _make_fm(knowledge_id="kid-clean"), 0.1),
        ]
        return fm_entries

    def test_snapshot_equivalence_vs_per_kid_reference(self, issues_tempdir):
        """AC2: множество закрываемых == эталонной per-kid реализации (mixed-фикстура)."""
        from unittest.mock import patch

        from mcp_server.quality.scanner import _auto_clear_stale_issues

        scored = self._mixed_fixture()

        # Эталон ДО вызова: spy вызывает реальный bulk_update → стор мутирует
        expected = self._reference_per_kid(scored)

        captured = {}
        real_bulk = __import__(
            "mcp_server.quality.issues", fromlist=["bulk_update_status"]
        ).bulk_update_status

        def spy_bulk(ids, status, resolution=None):
            captured["ids"] = list(ids)
            captured["status"] = status
            return real_bulk(ids, status, resolution)

        with patch("mcp_server.quality.scanner.bulk_update_status", side_effect=spy_bulk):
            cleared = _auto_clear_stale_issues(scored)

        assert sorted(captured["ids"]) == sorted(expected)
        assert len(captured["ids"]) == 3  # ровно open missing_field у kid-ok-*
        assert cleared == 3
        assert captured["status"] == "resolved"

    def test_cancel_event_set_no_bulk_update(self, issues_tempdir):
        """AC3-unit: cancel_event set → bulk_update_status НЕ вызывается."""
        import asyncio
        from unittest.mock import patch

        from mcp_server.quality.issues import create_issue
        from mcp_server.quality.scanner import _auto_clear_stale_issues

        create_issue("missing_field", "kid-ok-0", "warn", "score high")
        scored = [(Path("/tmp/kid-ok-0.md"), _make_fm(knowledge_id="kid-ok-0"), 0.1)]
        cancel = asyncio.Event()
        cancel.set()

        with patch("mcp_server.quality.scanner.bulk_update_status") as mock_bulk:
            cleared = _auto_clear_stale_issues(scored, cancel_event=cancel)

        assert cleared == 0
        mock_bulk.assert_not_called()

    def test_cancel_midway_chunk_no_bulk_update(self, issues_tempdir, monkeypatch):
        """AC3: cancel в середине (между чанками) → bulk_update_status НЕ вызывается."""
        import asyncio
        from unittest.mock import patch

        from mcp_server.quality import scanner as scanner_mod

        monkeypatch.setattr(scanner_mod, "AUTO_CLEAR_CHUNK_SIZE", 2)

        from mcp_server.quality.issues import create_issue

        for i in range(5):
            create_issue("missing_field", f"kid-ok-{i}", "warn", f"score high {i}")
        scored = [
            (Path(f"/tmp/kid-ok-{i}.md"), _make_fm(knowledge_id=f"kid-ok-{i}"), 0.1)
            for i in range(5)
        ]
        cancel = asyncio.Event()

        # Cancel срабатывает при первом чанк-чеке (после 2 kids)
        orig_is_set = cancel.is_set
        calls = {"n": 0}

        def is_set():
            calls["n"] += 1
            if calls["n"] >= 1:
                return True
            return orig_is_set()

        cancel.is_set = is_set

        with patch("mcp_server.quality.scanner.bulk_update_status") as mock_bulk:
            cleared = scanner_mod._auto_clear_stale_issues(
                scored, cancel_event=cancel,
            )

        assert cleared == 0
        mock_bulk.assert_not_called()

    def test_progress_log_batches(self, issues_tempdir):
        """AC5: batched-лог auto-clear по образцу issues:/sensitive:."""
        from mcp_server.quality.issues import create_issue
        from mcp_server.quality.scanner import _auto_clear_stale_issues

        for i in range(5):
            create_issue("missing_field", f"kid-ok-{i}", "warn", f"score high {i}")
        scored = [
            (Path(f"/tmp/kid-ok-{i}.md"), _make_fm(knowledge_id=f"kid-ok-{i}"), 0.1)
            for i in range(5)
        ]

        class FakeTracker:
            def __init__(self):
                self.logs = []

            def log(self, pid, level, text):
                self.logs.append((pid, level, text))

        tracker = FakeTracker()
        _auto_clear_stale_issues(
            scored, progress=tracker, progress_id="scan-test",
        )
        assert any(
            pid == "scan-test" and "auto-clear:" in text
            for pid, _lvl, text in tracker.logs
        ), f"auto-clear лог отсутствует: {tracker.logs}"

    def test_reads_store_once_for_grouped_snapshot(self, issues_tempdir):
        """O(N+M): один проход стора на весь шаг (анти-O(N×M))."""
        from unittest.mock import patch

        from mcp_server.quality import issues as issues_mod
        from mcp_server.quality.issues import create_issue
        from mcp_server.quality.scanner import _auto_clear_stale_issues

        for i in range(5):
            create_issue("missing_field", f"kid-ok-{i}", "warn", f"score high {i}")
        scored = [
            (Path(f"/tmp/kid-ok-{i}.md"), _make_fm(knowledge_id=f"kid-ok-{i}"), 0.1)
            for i in range(5)
        ]

        with patch.object(
            issues_mod, "_read_all_issues", wraps=issues_mod._read_all_issues
        ) as mock_read, patch("mcp_server.quality.scanner.bulk_update_status"):
            _auto_clear_stale_issues(scored)

        # grouped-снапшот читает стор 1 раз; bulk_update замокан (реальный тоже читает 1)
        assert mock_read.call_count == 1


# ═══════════════════════════════════════════════════════════════
# code-2026-09-24-011 (В3): батч-запись шагов 5 / 5.4 / dup_scan
# ═══════════════════════════════════════════════════════════════


class TestCreateIssuesForProblemsBatched:
    """Шаг 5: спеки собираются в батч → один create_issues_batch на батч 500."""

    @pytest.fixture
    def issues_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    def test_single_batch_call_no_per_record_create(self, issues_tempdir):
        """create_issue per-record НЕ вызывается; create_issues_batch — 1 раз на батч."""
        from unittest.mock import patch

        from mcp_server.quality import scanner as scanner_mod

        scored = [
            (Path(f"/tmp/kid-bad-{i}.md"), _make_fm(knowledge_id=f"kid-bad-{i}"), 0.9)
            for i in range(3)
        ]
        with patch("mcp_server.quality.issues.create_issue") as mock_create, \
             patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            count = scanner_mod._create_issues_for_problems(scored)

        mock_create.assert_not_called()
        assert mock_batch.call_count == 1
        specs = mock_batch.call_args[0][0]
        assert len(specs) == 3
        assert count == 3  # метрика = число спеков (как было число вызовов)

    def test_equivalent_to_sequential_create(self, issues_tempdir):
        """Эквивалентность: issue_id-множество батча == последовательному созданию."""
        from mcp_server.quality.issues import list_issues
        from mcp_server.quality.scanner import _create_issues_for_problems

        scored = [
            (Path(f"/tmp/kid-bad-{i}.md"), _make_fm(knowledge_id=f"kid-bad-{i}"), 0.7 + i / 100)
            for i in range(4)
        ]
        _create_issues_for_problems(scored)
        batch_ids = {i.issue_id for i in list_issues(status="open", limit=1000)}

        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp_b:
            from mcp_server.quality.issues import create_issue, set_store_dir
            set_store_dir(tmp_b)
            for _f, fm, score in scored:
                create_issue(
                    "missing_field", fm.knowledge_id, "warn",
                    f"Staleness score {score} >= 0.45 — needs review",
                )
            seq_ids = {i.issue_id for i in list_issues(status="open", limit=1000)}
        assert batch_ids == seq_ids

    def test_cancel_between_batches_no_call(self, issues_tempdir, monkeypatch):
        """Cancel между батчами → последующие батчи не создаются (AC3)."""
        import asyncio
        from unittest.mock import patch

        from mcp_server.quality import scanner as scanner_mod

        monkeypatch.setattr(scanner_mod, "ISSUES_BATCH_SIZE", 2)
        scored = [
            (Path(f"/tmp/kid-bad-{i}.md"), _make_fm(knowledge_id=f"kid-bad-{i}"), 0.9)
            for i in range(6)
        ]
        cancel = asyncio.Event()

        orig = cancel.is_set
        calls = {"n": 0}

        def is_set():
            calls["n"] += 1
            if calls["n"] > 1:
                return True
            return orig()

        cancel.is_set = is_set
        with patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            scanner_mod._create_issues_for_problems(scored, cancel_event=cancel)

        assert mock_batch.call_count == 1  # второй батч отменён


class TestCreateSensitiveIssuesBatched:
    """Шаг 5.4: sensitive-спеки → один create_issues_batch на батч."""

    @pytest.fixture
    def issues_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    @staticmethod
    def _sensitive_entry(kid: str, path: str):
        fm = _make_fm(knowledge_id=kid)
        return (Path(path), fm, {})

    def test_batched_flush_with_metadata(self, issues_tempdir, tmp_path):
        """Sensitive: create_issue НЕ вызывается; батч несёт metadata (refresh-семантика)."""
        from unittest.mock import patch

        from mcp_server.quality import scanner as scanner_mod

        p1 = tmp_path / "partners_entry.md"
        p1.write_text("руб. руб. маржа")  # ≥2 контент-совпадений
        p2 = tmp_path / "clean.md"
        p2.write_text("чистый контент")

        entries = [
            self._sensitive_entry("kid-sens", str(p1)),
            self._sensitive_entry("kid-clean", str(p2)),
        ]
        with patch("mcp_server.quality.issues.create_issue") as mock_create, \
             patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            count = scanner_mod._create_sensitive_issues(entries)

        mock_create.assert_not_called()
        assert mock_batch.call_count == 1
        specs = mock_batch.call_args[0][0]
        assert len(specs) == 1
        assert specs[0]["metadata"]["path_flag"] is True or len(specs[0]["metadata"]["matched"]) >= 2
        assert count == 1


class TestScanDupPairsBuffered:
    """Шаг 4 dup_scan: буфер пар → create_issues_batch per domain-бакет; флаш при cancel (P2-4)."""

    @pytest.fixture
    def issues_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    def _mk_entry(self, kid: str, subject: str, tags: list[str], domain: str = "eng"):
        fm = _make_fm(knowledge_id=kid, subject=subject, tags=tags, domain=domain)
        return (Path(f"/tmp/{kid}.md"), fm, 0.1)

    def test_no_inline_create_issue(self, issues_tempdir):
        """Inline create_issue в dup_scan заменён на батч-флаш."""
        from unittest.mock import patch

        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk_entry("kid-a", "devops", ["ai", "automation", "teaching"])
        b = self._mk_entry("kid-b", "devops", ["ai", "automation", "teaching", "extra"])
        with patch("mcp_server.quality.issues.create_issue") as mock_create, \
             patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            dup_count, dup_map = _scan_dup_pairs([a, b])

        mock_create.assert_not_called()
        assert mock_batch.call_count == 1  # один флаш на domain-бакет
        specs = mock_batch.call_args[0][0]
        assert len(specs) == 1
        assert specs[0]["issue_type"] == "duplicate"
        assert specs[0]["metadata"]["target_kid"] == "kid-b"
        assert dup_count == 1

    def test_flush_per_domain_bucket(self, issues_tempdir):
        """Два domain-бакета → два отдельных флаша (по одному на бакет)."""
        from unittest.mock import patch

        from mcp_server.quality.scanner import _scan_dup_pairs

        a1 = self._mk_entry("kid-a1", "subj", ["t1", "t2"], domain="eng")
        b1 = self._mk_entry("kid-b1", "subj", ["t1", "t2"], domain="eng")
        a2 = self._mk_entry("kid-a2", "subj", ["t1", "t2"], domain="math")
        b2 = self._mk_entry("kid-b2", "subj", ["t1", "t2"], domain="math")

        with patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            dup_count, _ = _scan_dup_pairs([a1, b1, a2, b2])

        assert dup_count == 2
        assert mock_batch.call_count == 2
        for call in mock_batch.call_args_list:
            assert len(call[0][0]) == 1  # по одной паре на бакет

    def test_intra_bucket_cancel_flushes_buffer(self, issues_tempdir, monkeypatch):
        """P2-4: cancel ВНУТРИ бакета → накопленный буфер фллашится перед выходом."""
        import asyncio
        from unittest.mock import patch

        from mcp_server.quality import scanner as scanner_mod

        monkeypatch.setattr(scanner_mod, "DUP_CANCEL_CHECK_EVERY", 1)

        # 3 записи → 3 пары; cancel после первой итерации → буфер с 1 парой флашится
        a = self._mk_entry("kid-a", "subj", ["t1", "t2"])
        b = self._mk_entry("kid-b", "subj", ["t1", "t2"])
        c = self._mk_entry("kid-c", "subj", ["t1", "t2"])

        cancel = asyncio.Event()
        orig = cancel.is_set
        calls = {"n": 0}

        def is_set():
            calls["n"] += 1
            # n=1 — межбакетный чек (не cancel); n=2,3 — пары (0,1),(0,2)
            # буферизуются; n=4 — cancel → флаш 2 пар перед выходом
            if calls["n"] >= 4:
                return True
            return orig()

        cancel.is_set = is_set

        with patch("mcp_server.quality.scanner.create_issues_batch") as mock_batch:
            dup_count, dup_map = scanner_mod._scan_dup_pairs([a, b, c], cancel_event=cancel)

        # Частичный результат сохранён (флаш перед выходом)
        assert mock_batch.call_count == 1
        flushed = mock_batch.call_args[0][0]
        assert len(flushed) >= 1
        # dup_count/dup_map считаются до флаша — частичные метрики валидны
        assert dup_count == len(flushed)

    def test_dup_flush_equivalent_to_sequential(self, issues_tempdir):
        """Эквивалентность: buffered-dup == последовательному create_issue (issue_id+metadata)."""
        import tempfile as _tf

        from mcp_server.quality.issues import list_issues
        from mcp_server.quality.scanner import _scan_dup_pairs

        a = self._mk_entry("kid-a", "devops", ["ai", "automation", "teaching"])
        b = self._mk_entry("kid-b", "devops", ["ai", "automation", "teaching", "extra"])
        _scan_dup_pairs([a, b])
        batch = {
            (i.issue_id): (i.metadata, i.status)
            for i in list_issues(types=["duplicate"], status="open", limit=1000)
        }

        with _tf.TemporaryDirectory() as tmp_b:
            from mcp_server.quality.issues import create_issue, set_store_dir
            set_store_dir(tmp_b)
            create_issue(
                "duplicate", "kid-a", "warn",
                "Possible duplicate of kid-b (same subject=devops, tag overlap)",
                metadata={
                    "cosine": None, "content_hash": None, "content_length": None,
                    "target_content_hash": None, "target_content_length": None,
                    "slug_negation": False, "standalone": True,
                    "target_standalone": True, "subject": "devops",
                    "target_subject": "devops", "target_kid": "kid-b",
                },
            )
            seq = {
                (i.issue_id): (i.metadata, i.status)
                for i in list_issues(types=["duplicate"], status="open", limit=1000)
            }

        assert set(batch) == set(seq)
        assert batch == seq
