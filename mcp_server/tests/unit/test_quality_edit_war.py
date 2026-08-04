"""Unit-тесты для quality/edit_war.py (4.4)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp_server.quality.edit_war import (
    EDIT_WAR_THRESHOLD,
    EDIT_WAR_WINDOW_H,
    _find_git_root,
    detect_all_edit_wars,
    detect_edit_war,
)


def _now() -> datetime:
    return datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


class TestFindGitRoot:
    """Поиск корня .git."""

    def test_finds_git_root_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            subdir = root / "knowledge" / "engineering" / "python"
            subdir.mkdir(parents=True)
            found = _find_git_root(subdir)
            assert found == root

    def test_no_git_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            found = _find_git_root(root)
            assert found is None

    def test_finds_from_deep_nesting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            deep = root / "a" / "b" / "c" / "d" / "e"
            deep.mkdir(parents=True)
            found = _find_git_root(deep)
            assert found == root


class TestDetectEditWar:
    """Detect_edit_war с моками git."""

    def test_no_git_available(self):
        """gitpython не импортируется → False (graceful degradation)."""
        with patch("mcp_server.quality.edit_war._find_git_root", return_value=None):
            result = detect_edit_war("/fake/path/test.md")
            assert result is False

    def test_below_threshold_not_edit_war(self):
        """Менее 3 коммитов → не edit-war."""
        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = [MagicMock(), MagicMock()]  # 2 commits

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            result = detect_edit_war(Path("/repo/knowledge/test.md"))
            assert result is False

    def test_at_threshold_is_edit_war(self):
        """3 коммита за 24ч → edit-war."""
        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = [MagicMock()] * 3

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            result = detect_edit_war(Path("/repo/knowledge/test.md"))
            assert result is True

    def test_custom_threshold(self):
        """Кастомный threshold=5."""
        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = [MagicMock()] * 5

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            result = detect_edit_war(
                Path("/repo/knowledge/test.md"), threshold=5
            )
            assert result is True

    def test_custom_window(self):
        """Кастомное окно 1h — старые коммиты не учитываются."""
        mock_repo = MagicMock()
        mock_repo.iter_commits.return_value = []

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            result = detect_edit_war(
                Path("/repo/knowledge/test.md"), window_h=1
            )
            assert result is False

    def test_git_error_graceful(self):
        """GitError → False, не крашится."""
        from git.exc import GitError

        mock_repo = MagicMock()
        mock_repo.iter_commits.side_effect = GitError("git failed")

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            result = detect_edit_war(Path("/repo/knowledge/test.md"))
            assert result is False

    def test_file_outside_repo(self):
        """Файл вне репо → False."""
        mock_repo = MagicMock()

        with patch("git.Repo", return_value=mock_repo), \
             patch("mcp_server.quality.edit_war._find_git_root", return_value=Path("/repo")):
            # Файл НЕ внутри /repo
            result = detect_edit_war(Path("/other/test.md"))
            assert result is False


class TestDetectAllEditWars:
    """Сканирование всей директории."""

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = detect_all_edit_wars(tmpdir)
            assert result == []

    def test_no_git_repo_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.md").write_text("content")
            result = detect_all_edit_wars(tmpdir)
            assert result == []


class TestDefaults:
    """Константы по умолчанию соответствуют плану."""

    def test_window_default(self):
        assert EDIT_WAR_WINDOW_H == 24

    def test_threshold_default(self):
        assert EDIT_WAR_THRESHOLD == 3
