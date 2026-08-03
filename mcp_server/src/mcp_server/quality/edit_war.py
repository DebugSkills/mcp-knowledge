"""Edit-war detection — git-based анализ частоты правок (4.4).

Обнаруживает «войны правок»: ≥3 коммитов в один knowledge_id за 24 часа.
Использует gitpython для чтения git-истории (#21 — git-аудит уже ведётся).

Зависимости: gitpython (лёгкий, уже в deps).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mcp_knowledge.quality.edit_war")

# ── Конфигурация ─────────────────────────────────────────────

EDIT_WAR_WINDOW_H: int = 24        # окно анализа (часы)
EDIT_WAR_THRESHOLD: int = 3        # мин. число коммитов для edit_war


def detect_edit_war(
    file_path: str | Path,
    *,
    window_h: int = EDIT_WAR_WINDOW_H,
    threshold: int = EDIT_WAR_THRESHOLD,
    repo_path: str | Path | None = None,
    now: Optional[datetime] = None,
) -> bool:
    """Проверяет — идёт ли «война правок» для конкретного файла знаний.

    Анализирует git-историю: если ≥threshold коммитов за последние window_h
    часов — возвращает True.

    Args:
        file_path: путь к .md файлу (относительный от корня репо).
        window_h: окно анализа в часах (default 24).
        threshold: мин. число коммитов (default 3).
        repo_path: путь к git-репозиторию (если None — ищется от file_path).
        now: «текущее время» для тестирования.

    Returns:
        True если edit-war обнаружен.
    """
    try:
        from git import Repo
        from git.exc import GitError
    except ImportError:
        logger.warning("gitpython not available, edit-war detection skipped")
        return False

    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(hours=window_h)

    # Определяем путь к репозиторию
    file_path = Path(file_path)
    if repo_path is None:
        # Ищем .git от директории файла вверх
        repo_path = _find_git_root(file_path.parent)
        if repo_path is None:
            logger.debug("No git repo found for %s", file_path)
            return False

    try:
        repo = Repo(str(repo_path))
    except GitError as exc:
        logger.warning("Failed to open git repo %s: %s", repo_path, exc)
        return False

    # Преобразуем путь файла в относительный от корня репо
    try:
        rel_path = file_path.relative_to(repo_path)
    except ValueError:
        # Файл вне репо — не можем анализировать
        return False

    try:
        # git log --follow --after=<cutoff> --format=%H <file>
        commits = list(
            repo.iter_commits(
                paths=str(rel_path),
                after=cutoff.astimezone(timezone.utc).replace(tzinfo=None),
                max_count=threshold * 2,  # лимит для производительности
            )
        )
    except GitError as exc:
        logger.warning("git log failed for %s: %s", rel_path, exc)
        return False

    recent_count = len(commits)
    is_edit_war = recent_count >= threshold

    if is_edit_war:
        logger.info(
            "Edit-war detected: %s — %d commits in %dh (threshold=%d)",
            rel_path, recent_count, window_h, threshold,
        )

    return is_edit_war


def detect_all_edit_wars(
    knowledge_dir: str | Path,
    *,
    window_h: int = EDIT_WAR_WINDOW_H,
    threshold: int = EDIT_WAR_THRESHOLD,
    repo_path: Optional[str | Path] = None,
) -> list[str]:
    """Сканирует все .md файлы в knowledge_dir на edit-war.

    Returns:
        список knowledge_id с обнаруженными edit-war.
    """
    knowledge_dir = Path(knowledge_dir)
    if not knowledge_dir.exists():
        return []

    edit_war_ids: list[str] = []
    for md_file in knowledge_dir.rglob("*.md"):
        if detect_edit_war(
            md_file, window_h=window_h, threshold=threshold, repo_path=repo_path
        ):
            # knowledge_id = имя файла без .md
            knowledge_id = md_file.stem
            edit_war_ids.append(knowledge_id)

    return edit_war_ids


def _find_git_root(start_dir: Path) -> Optional[Path]:
    """Ищет корень git-репозитория, поднимаясь по дереву директорий."""
    current = start_dir.resolve()
    for _ in range(20):  # защита от бесконечного цикла
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None
