"""Unit-тесты W4.3/W4.4: sensitive-сканер (план §2.5, порог §9.3).

Эвристики путей (PARTNERS/, _private/, REVIEW-*, .trash/) и контента
(₽|руб.|USD|маржа|гонорар|оклад|цена|договор). Порог: путь-маркер ИЛИ
≥2 контент-совпадений → issue "sensitive" severity=warn (флаг, не блок).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.scanner import (
    _check_sensitive,
    _create_sensitive_issues,
    run_scan,
)


def _make_fm(**overrides) -> KnowledgeFrontmatter:
    """Фабрика KnowledgeFrontmatter с разумными defaults."""
    defaults = {
        "knowledge_id": "test-kb-001",
        "domain": "engineering",
        "subject": "python",
        "tags": ["asyncio", "testing"],
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 3, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return KnowledgeFrontmatter(**defaults)


# ═══════════════════════════════════════════════════════════════
# _check_sensitive — чистая функция эвристик
# ═══════════════════════════════════════════════════════════════


class TestCheckSensitive:
    """_check_sensitive: путь-маркеры и контент-порог (§2.5 + §9.3)."""

    def test_partners_path_flags(self):
        payload = _check_sensitive("/kb/knowledge/PARTNERS/ivan.md", "обычный текст")
        assert payload is not None
        assert payload["type"] == "sensitive"
        assert payload["severity"] == "warn"
        assert payload["metadata"]["path_flag"] is True

    def test_private_path_flags(self):
        payload = _check_sensitive("/kb/knowledge/_private/notes.md", "текст")
        assert payload is not None
        assert payload["metadata"]["path_flag"] is True

    def test_review_path_flags(self):
        payload = _check_sensitive("/kb/knowledge/REVIEW-2026/x.md", "текст")
        assert payload is not None
        assert payload["metadata"]["path_flag"] is True

    def test_trash_path_flags(self):
        # Маркер .trash/ в хелпере есть (скан-уровень: _scan_filesystem
        # пропускает .trash-файлы, 13.18 — маркер остаётся страховкой)
        payload = _check_sensitive("/kb/knowledge/foo/.trash/x.md", "текст")
        assert payload is not None
        assert payload["metadata"]["path_flag"] is True

    def test_two_content_markers_flags(self):
        payload = _check_sensitive(
            "/kb/knowledge/eng/x.md", "гонорар 1000 руб. по договору",
        )
        assert payload is not None
        assert payload["metadata"]["path_flag"] is False
        assert set(payload["metadata"]["matched"]) >= {"гонорар", "руб.", "договор"}

    def test_single_content_marker_not_flagged(self):
        # «цена» встречается в учебных текстах → порог ≥2 (решение §9.3)
        assert _check_sensitive("/kb/knowledge/eng/x.md", "цена вопроса") is None

    def test_clean_path_and_content_not_flagged(self):
        assert _check_sensitive(
            "/kb/knowledge/eng/x.md", "обычный учебный текст без маркеров",
        ) is None

    def test_matched_deduplicated_and_sorted(self):
        # Детерминированный metadata (metadata-refresh в create_issue)
        payload = _check_sensitive("/kb/x.md", "руб. и руб. и USD")
        assert payload is not None
        assert payload["metadata"]["matched"] == ["USD", "руб."]


# ═══════════════════════════════════════════════════════════════
# _create_sensitive_issues — создание issue в сторе
# ═══════════════════════════════════════════════════════════════


class TestCreateSensitiveIssues:
    """_create_sensitive_issues: создание/идемпотентность sensitive-issues."""

    @pytest.fixture
    def issues_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    @staticmethod
    def _mk_entry(filepath: Path, kid: str) -> tuple:
        return (filepath, _make_fm(knowledge_id=kid), {})

    def test_partners_file_creates_issue(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            partners_dir = Path(tmp) / "PARTNERS"
            partners_dir.mkdir()
            f = partners_dir / "ivan.md"
            f.write_text("# Данные партнёра\n", encoding="utf-8")

            count = _create_sensitive_issues([self._mk_entry(f, "partner-ivan")])
            assert count == 1

            issues = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(issues) == 1
            assert issues[0].type == "sensitive"
            assert issues[0].knowledge_id == "partner-ivan"
            assert issues[0].severity == "warn"
            assert issues[0].metadata["path_flag"] is True

    def test_private_file_creates_issue(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "_private"
            private_dir.mkdir()
            f = private_dir / "notes.md"
            f.write_text("# Заметки\n", encoding="utf-8")

            _create_sensitive_issues([self._mk_entry(f, "private-notes")])

            issues = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(issues) == 1
            assert issues[0].knowledge_id == "private-notes"

    def test_review_file_creates_issue(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "REVIEW-2026-08"
            review_dir.mkdir()
            f = review_dir / "draft.md"
            f.write_text("# Черновик\n", encoding="utf-8")

            _create_sensitive_issues([self._mk_entry(f, "review-draft")])

            issues = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(issues) == 1
            assert issues[0].knowledge_id == "review-draft"

    def test_content_two_markers_creates_issue(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "finance.md"
            f.write_text("Оклад 50 000 руб. зафиксирован в договоре\n", encoding="utf-8")

            _create_sensitive_issues([self._mk_entry(f, "finance-001")])

            issues = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(issues) == 1
            assert issues[0].metadata["path_flag"] is False
            # findall сохраняет регистр исходного текста («Оклад») — сверка case-insensitive
            matched_lower = " ".join(issues[0].metadata["matched"]).lower()
            assert "оклад" in matched_lower

    def test_single_marker_no_issue(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "edu.md"
            f.write_text("Рассмотрим цену вопроса в обучении\n", encoding="utf-8")

            count = _create_sensitive_issues([self._mk_entry(f, "edu-001")])
            assert count == 0
            assert list_issues(types=["sensitive"], status="open", limit=10) == []

    def test_idempotent_second_run(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            partners_dir = Path(tmp) / "PARTNERS"
            partners_dir.mkdir()
            f = partners_dir / "ivan.md"
            f.write_text("# Данные партнёра\n", encoding="utf-8")

            entries = [self._mk_entry(f, "partner-ivan")]
            _create_sensitive_issues(entries)
            _create_sensitive_issues(entries)  # повторный скан

            issues = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(issues) == 1  # одна open issue на запись

    def test_unreadable_file_skipped(self, issues_store):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "partners-gone.md"
            # Файл не существует → read_text упадёт → graceful skip
            count = _create_sensitive_issues([self._mk_entry(f, "ghost")])
            assert count == 0


# ═══════════════════════════════════════════════════════════════
# run_scan — sensitive-флаг не блокирует скан (приёмка W4)
# ═══════════════════════════════════════════════════════════════


class TestRunScanSensitive:
    """run_scan завершается, sensitive-issue создан (severity=warn)."""

    @pytest.fixture
    def issues_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mcp_server.quality.issues import set_store_dir
            set_store_dir(tmp)
            yield tmp

    @pytest.mark.asyncio
    async def test_scan_completes_and_flags_partners(self, issues_store):
        from mcp_server.quality.issues import list_issues

        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge"
            partners_dir = knowledge_dir / "PARTNERS"
            partners_dir.mkdir(parents=True)

            def _md(kid: str, body: str) -> str:
                return (
                    f"---\nknowledge_id: {kid}\ndomain: eng\nsubject: test\n"
                    "tags: [t]\ncreated_at: 2026-08-01T10:00:00+03:00\n"
                    "updated_at: 2026-08-03T10:00:00+03:00\n---\n" + body
                )

            (partners_dir / "p.md").write_text(
                _md("kid-partner", "# Партнёрские данные\n"), encoding="utf-8",
            )
            (knowledge_dir / "normal.md").write_text(
                _md("kid-normal", "# Обычная запись\n"), encoding="utf-8",
            )

            result = await run_scan(knowledge_dir=knowledge_dir, qdrant_client=None)

            # Скан завершился без исключений, оба файла обработаны
            assert result["files_scanned"] == 2
            assert isinstance(result["issues_created"], int)

            sensitive = list_issues(types=["sensitive"], status="open", limit=10)
            assert len(sensitive) == 1
            assert sensitive[0].knowledge_id == "kid-partner"
            assert sensitive[0].severity == "warn"  # флаг, не блок
