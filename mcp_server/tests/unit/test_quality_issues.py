"""Unit-тесты для quality/issues.py (4.1)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from mcp_server.quality.issues import (
    Issue,
    create_issue,
    get_issues_store_path,
    list_issues,
    set_store_dir,
    update_issue_status,
)


class TestCreateIssue:
    """Тесты создания issues."""

    def test_create_issue_basic(self):
        """Создание issue и чтение из JSONL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue = create_issue(
                issue_type="duplicate",
                knowledge_id="test-kb-001",
                severity="warn",
                detail="cosine=0.94 with test-kb-002",
            )
            assert issue.knowledge_id == "test-kb-001"
            assert issue.type == "duplicate"
            assert issue.severity == "warn"
            assert issue.status == "open"
            assert issue.issue_id.startswith("iss_")
            assert issue.resolved_at is None
            # Проверяем что запись попала в JSONL
            store_path = get_issues_store_path()
            with open(store_path) as f:
                lines = f.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["knowledge_id"] == "test-kb-001"

    def test_create_issue_idempotent(self):
        """Повторный create с теми же параметрами не создаёт дубликат."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue1 = create_issue(
                issue_type="missing_field",
                knowledge_id="test-kb-003",
                severity="warn",
                detail="missing source",
            )
            issue2 = create_issue(
                issue_type="missing_field",
                knowledge_id="test-kb-003",
                severity="warn",
                detail="missing source",
            )
            assert issue1.issue_id == issue2.issue_id
            # В JSONL одна запись
            store_path = get_issues_store_path()
            with open(store_path) as f:
                lines = f.readlines()
            assert len(lines) == 1

    def test_create_issue_different_detail_not_idempotent(self):
        """Разный detail → разные issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue1 = create_issue(
                issue_type="duplicate",
                knowledge_id="test-kb-005",
                severity="warn",
                detail="cosine=0.94",
            )
            issue2 = create_issue(
                issue_type="duplicate",
                knowledge_id="test-kb-005",
                severity="warn",
                detail="cosine=0.96",  # другой detail
            )
            assert issue1.issue_id != issue2.issue_id
            store_path = get_issues_store_path()
            with open(store_path) as f:
                lines = f.readlines()
            assert len(lines) == 2

    def test_create_issue_atomic_write_no_tmp_leftover(self):
        """Атомарная запись: .tmp файлы не остаются."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            create_issue(
                issue_type="broken_link",
                knowledge_id="test-kb-007",
                severity="info",
                detail="https://example.com returned 404",
            )
            # Проверяем что нет .tmp файлов в директории
            tmp_files = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            assert len(tmp_files) == 0

    def test_create_issue_metadata_refresh(self):
        """Фаза 3 (0b): повторный create с новым metadata обновляет поле, не дублирует."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            create_issue(
                "duplicate", "kb-refresh", "warn", "dup",
                metadata={"subject": "a", "target_subject": None},
            )
            refreshed = create_issue(
                "duplicate", "kb-refresh", "warn", "dup",
                metadata={"subject": "a", "target_subject": "a", "target_kid": "t"},
            )
            assert refreshed.metadata == {"subject": "a", "target_subject": "a", "target_kid": "t"}
            # Одна запись в сторе (не дубликат)
            store_path = get_issues_store_path()
            with open(store_path) as f:
                lines = f.readlines()
            assert len(lines) == 1

    def test_create_issue_metadata_refresh_idempotent_id(self):
        """Фаза 3 (0b): refresh НЕ меняет issue_id (идемпотентность ID сохранена)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            iss1 = create_issue("duplicate", "kb-r2", "warn", "dup", metadata={"x": 1})
            iss2 = create_issue("duplicate", "kb-r2", "warn", "dup", metadata={"x": 2})
            assert iss1.issue_id == iss2.issue_id
            assert iss2.metadata == {"x": 2}


class TestListIssues:
    """Тесты фильтрации и листинга."""

    def _setup_issues(self, store_dir: str) -> None:
        """Создаёт тестовый набор issues."""
        set_store_dir(store_dir)
        create_issue("duplicate", "kb-1", "warn", "dup with kb-2")
        create_issue("missing_field", "kb-1", "info", "missing source")
        create_issue("edit_war", "kb-3", "critical", "5 edits in 24h")
        create_issue("broken_link", "kb-4", "warn", "dead URL")

    def test_list_all_open(self):
        """list_issues без фильтров возвращает все open."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._setup_issues(tmpdir)
            all_issues = list_issues()
            assert len(all_issues) == 4
            for issue in all_issues:
                assert issue.status == "open"

    def test_list_filter_by_type(self):
        """Фильтрация по type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._setup_issues(tmpdir)
            dupes = list_issues(types=["duplicate"])
            assert len(dupes) == 1
            assert dupes[0].type == "duplicate"

    def test_list_multiple_types(self):
        """Фильтрация по нескольким типам."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._setup_issues(tmpdir)
            result = list_issues(types=["duplicate", "missing_field"])
            assert len(result) == 2
            types = {i.type for i in result}
            assert types == {"duplicate", "missing_field"}

    def test_list_filter_by_status(self):
        """Фильтрация по status — после resolve."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._setup_issues(tmpdir)
            # Получаем первый issue и резолвим его
            all_open = list_issues(status="open")
            first = all_open[0]
            update_issue_status(first.issue_id, "resolved", "fixed manually")
            # Теперь open должно быть 3
            still_open = list_issues(status="open")
            assert len(still_open) == 3
            # А resolved — 1
            resolved = list_issues(status="resolved")
            assert len(resolved) == 1
            assert resolved[0].issue_id == first.issue_id


class TestUpdateIssueStatus:
    """Тесты изменения статуса."""

    def test_update_to_resolved(self):
        """resolve → status=resolved, resolved_at установлен."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue = create_issue("duplicate", "kb-10", "warn", "test")
            updated = update_issue_status(
                issue.issue_id, "resolved", "merged into kb-11"
            )
            assert updated.status == "resolved"
            assert updated.resolved_at is not None
            assert updated.resolution == "merged into kb-11"

    def test_update_to_ignored(self):
        """ignore → status=ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue = create_issue("broken_link", "kb-12", "info", "false alarm")
            updated = update_issue_status(issue.issue_id, "ignored")
            assert updated.status == "ignored"
            assert updated.resolved_at is not None

    def test_resolved_at_is_utc_datetime(self):
        """resolved_at — UTC datetime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            issue = create_issue("duplicate", "kb-13", "warn", "test")
            updated = update_issue_status(issue.issue_id, "resolved")
            assert updated.resolved_at is not None
            # Pydantic v2 парсит "+00:00" в pydantic_core.TzInfo(UTC) — отдельный
            # объект, не identity с timezone.utc. Проверяем семантику UTC:
            # tzinfo присутствует + utcoffset = 0.
            assert updated.resolved_at.tzinfo is not None
            assert updated.resolved_at.utcoffset() == timedelta(0)


class TestIssueModel:
    """Тесты Pydantic-модели Issue."""

    def test_issue_model_creation(self):
        """Прямое создание Issue через Pydantic."""
        now = datetime.now(timezone.utc)
        issue = Issue(
            issue_id="iss_test123",
            type="duplicate",
            knowledge_id="kb-20",
            severity="warn",
            detail="test detail",
            detected_at=now,
            status="open",
        )
        assert issue.issue_id == "iss_test123"
        assert issue.status == "open"
        assert issue.resolved_at is None
        assert issue.resolution is None
        # JSON сериализация
        data = issue.model_dump(mode="json")
        assert data["type"] == "duplicate"


# ═══════════════════════════════════════════════════════════════
# code-2026-09-24-011 (В3): snapshot-хелперы для O(N+M) quality-scan
# ═══════════════════════════════════════════════════════════════


class TestListOpenIssueIdsGrouped:
    """list_open_issue_ids_grouped — один read стора → dict kid → [issue_id]."""

    def test_groups_open_by_kid_with_type_filter(self):
        """Группировка: только open + только заданные типы; resolved/чужие типы исключены."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import list_open_issue_ids_grouped

            i1 = create_issue("missing_field", "kid-a", "warn", "d1")
            i2 = create_issue("missing_field", "kid-a", "warn", "d2")
            i3 = create_issue("missing_field", "kid-b", "warn", "d3")
            create_issue("duplicate", "kid-a", "warn", "d4")  # чужой тип
            resolved = create_issue("missing_field", "kid-c", "warn", "d5")
            update_issue_status(resolved.issue_id, "resolved")

            grouped = list_open_issue_ids_grouped(["missing_field"])

            assert set(grouped) == {"kid-a", "kid-b"}
            assert sorted(grouped["kid-a"]) == sorted([i1.issue_id, i2.issue_id])
            assert grouped["kid-b"] == [i3.issue_id]

    def test_empty_store_returns_empty_dict(self):
        """Пустой/несуществующий стор → {} (не падает)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import list_open_issue_ids_grouped

            assert list_open_issue_ids_grouped(["missing_field"]) == {}

    def test_reads_store_once(self):
        """ОДИН проход по стору на вызов (снапшот-семантика, анти-O(N×M))."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from unittest.mock import patch

            from mcp_server.quality.issues import list_open_issue_ids_grouped

            for i in range(5):
                create_issue("missing_field", f"kid-{i}", "warn", f"d{i}")

            with patch(
                "mcp_server.quality.issues._read_all_issues", wraps=None
            ) as mock_read:
                list_open_issue_ids_grouped(["missing_field"])
                assert mock_read.call_count == 1


class TestCreateIssuesBatch:
    """create_issues_batch — один read-modify-write на батч, семантика create_issue."""

    def _spec(self, kid: str, detail: str, metadata: dict | None = None) -> dict:
        return {
            "issue_type": "missing_field",
            "knowledge_id": kid,
            "severity": "warn",
            "detail": detail,
            "metadata": metadata,
        }

    def test_creates_new_and_counts(self):
        """Новые спеки создаются, счётчики корректны."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import create_issues_batch, list_issues

            result = create_issues_batch([self._spec("kid-1", "d1"), self._spec("kid-2", "d2")])

            assert result == {"created": 2, "refreshed": 0, "skipped": 0}
            open_issues = list_issues(types=["missing_field"], status="open", limit=100)
            assert {i.knowledge_id for i in open_issues} == {"kid-1", "kid-2"}

    def test_idempotent_skip_existing(self):
        """Дубликаты спеков (существующие issue_id) → skipped, без rewrite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import create_issues_batch

            create_issues_batch([self._spec("kid-1", "d1")])
            result = create_issues_batch([self._spec("kid-1", "d1")])
            assert result == {"created": 0, "refreshed": 0, "skipped": 1}

    def test_metadata_refresh_semantics_preserved(self):
        """metadata-refresh как в create_issue: существующий issue получает новый metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import create_issues_batch, list_issues

            create_issues_batch([self._spec("kid-1", "d1", metadata={"cosine": 0.9})])
            result = create_issues_batch([self._spec("kid-1", "d1", metadata={"cosine": 0.95})])

            assert result == {"created": 0, "refreshed": 1, "skipped": 0}
            issues = list_issues(types=["missing_field"], status="open", limit=10)
            assert len(issues) == 1
            assert issues[0].metadata == {"cosine": 0.95}

    def test_exactly_one_write_per_batch(self):
        """РОВНО один _write_all_issues на батч (write-amplification K×→1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from unittest.mock import patch

            from mcp_server.quality.issues import create_issues_batch

            specs = [self._spec(f"kid-{i}", f"d{i}") for i in range(10)]
            with patch(
                "mcp_server.quality.issues._write_all_issues", wraps=None
            ) as mock_write:
                result = create_issues_batch(specs)
                assert mock_write.call_count == 1
            assert result["created"] == 10

    def test_no_write_when_all_skipped(self):
        """Все спеки — дубликаты без refresh → 0 write (лишний rewrite не нужен)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from unittest.mock import patch

            from mcp_server.quality.issues import create_issues_batch

            create_issues_batch([self._spec("kid-1", "d1", metadata={"m": 1})])
            with patch(
                "mcp_server.quality.issues._write_all_issues", wraps=None
            ) as mock_write:
                result = create_issues_batch([self._spec("kid-1", "d1", metadata={"m": 1})])
                assert mock_write.call_count == 0
            assert result["skipped"] == 1

    def test_equivalent_to_sequential_create_issue(self):
        """Эквивалентность: батч == последовательные create_issue (issue_id + metadata)."""
        with tempfile.TemporaryDirectory() as tmpdir_a, tempfile.TemporaryDirectory() as tmpdir_b:
            from mcp_server.quality.issues import list_issues

            specs = [
                self._spec("kid-1", "d1", {"cosine": 0.9}),
                self._spec("kid-2", "d2", None),
                self._spec("kid-1", "d3", {"cosine": 0.8}),
            ]
            # Стор A: батч
            set_store_dir(tmpdir_a)
            from mcp_server.quality.issues import create_issues_batch
            create_issues_batch(specs)
            batch_issues = {
                i.issue_id: (i.metadata, i.status) for i in list_issues(status="open", limit=1000)
            }
            # Стор B: последовательные create_issue
            set_store_dir(tmpdir_b)
            for spec in specs:
                create_issue(**spec)
            seq_issues = {
                i.issue_id: (i.metadata, i.status) for i in list_issues(status="open", limit=1000)
            }

            assert batch_issues == seq_issues

    def test_empty_specs_noop(self):
        """Пустой батч → нулевые счётчики, стор не создаётся."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import create_issues_batch, get_issues_store_path

            result = create_issues_batch([])
            assert result == {"created": 0, "refreshed": 0, "skipped": 0}
            assert not get_issues_store_path().exists()

    def test_no_tmp_leftover(self):
        """Атомарность: после батча нет .tmp-остатка."""
        with tempfile.TemporaryDirectory() as tmpdir:
            set_store_dir(tmpdir)
            from mcp_server.quality.issues import create_issues_batch, get_issues_store_path

            create_issues_batch([self._spec("kid-1", "d1")])
            store = get_issues_store_path()
            assert store.exists()
            assert not store.with_suffix(".jsonl.tmp").exists()
