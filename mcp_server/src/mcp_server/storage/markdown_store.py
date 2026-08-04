"""Markdown SSOT-хранилище + git-аудит (#2, #21) + гибридная иерархия (#4).

Задачи 1.1 и 1.2 плана Фазы 1.

Контракт:
- Markdown на диске — единственный источник правды (SSOT)
- CRUD над knowledge/{domain}/{subject}/{project}/*.md
- Soft-delete → knowledge/.trash/
- Каждый write/update/delete → git add && git commit (#21)
- asyncio.Lock на git-операции (гонка на .git/index.lock)
- git gc --auto встроен

Фаза 3 F2: Atomic optimistic locking (expected_version → VersionConflictError).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import git
import yaml

from ..config import settings
from ..models import (
    KnowledgeEntry,
    KnowledgeFrontmatter,
    VersionConflictError,
    WriteRequest,
)

logger = logging.getLogger("mcp_knowledge.markdown_store")

# Разделитель YAML frontmatter
_FM_DELIMITER = "---\n"
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class MarkdownStore:
    """CRUD-хранилище Markdown SSOT с git-аудитом."""

    def __init__(self, knowledge_root: str | Path = settings.KNOWLEDGE_ROOT):
        self._root = Path(knowledge_root).resolve()
        self._trash = self._root / ".trash"
        self._trash.mkdir(parents=True, exist_ok=True)

        # Git repo (knowledge/ — отдельный репозиторий #28)
        self._repo = git.Repo(self._root) if self._root.joinpath(".git").exists() else None
        # asyncio.Lock для сериализации git-операций (#21, v2.2)
        self._git_lock = asyncio.Lock()

        if self._repo is None:
            logger.warning("knowledge/ не является git-репозиторием — git-аудит отключён")
        else:
            logger.info("MarkdownStore: knowledge root=%s, git=%s", self._root, self._repo.git_dir)

    # ── Public API ─────────────────────────────────────────

    async def read(self, knowledge_id: str) -> KnowledgeEntry | None:
        """Прочитать запись по knowledge_id (поиск по всем директориям)."""
        path = self._find_by_id(knowledge_id)
        if path is None:
            return None
        return self._parse_file(path)

    async def write(self, req: WriteRequest) -> KnowledgeEntry:
        """Создать новую .md запись + git commit."""
        knowledge_id = req.knowledge_id or self._generate_id(req.domain, req.subject, req.content)

        now = datetime.now(timezone.utc)
        fm = KnowledgeFrontmatter(
            knowledge_id=knowledge_id,
            domain=req.domain,
            subject=req.subject,
            project=req.project,
            cross_subjects=req.cross_subjects,
            tags=req.tags,
            version=1,
            created_at=now,
            updated_at=now,
        )
        entry = KnowledgeEntry(frontmatter=fm, content=req.content)

        # Запись на диск
        path = self._resolve_path(entry.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_file(path, entry)

        # Git-аудит
        await self.flush(f"add: {knowledge_id}")

        logger.info("write_knowledge: %s → %s", knowledge_id, path)
        return entry

    async def write_entry(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """Записать готовую KnowledgeEntry на диск (без git-коммита).

        Используется batch-импортом (Фаза 5) для записи секций с batched git-коммитами.
        Git-коммиты делаются отдельно вызывающим кодом каждые IMPORT_BATCH_COMMIT секций.
        """
        path = self._resolve_path(entry.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_file(path, entry)
        logger.debug("write_entry: %s → %s", entry.frontmatter.knowledge_id, path)
        return entry

    async def update(self, knowledge_id: str, content: str | None = None,
                     metadata: dict | None = None,
                     expected_version: int | None = None) -> KnowledgeEntry:
        """Обновить запись (контент и/или метаданные).

        Фаза 3 F2: Atomic optimistic locking через expected_version.
        - expected_version=None → last-write-wins (backward-compatible)
        - expected_version=N → проверка внутри _git_lock → VersionConflictError при несовпадении

        Возвращает обновлённый KnowledgeEntry.

        Raises:
            VersionConflictError: если expected_version не совпадает с текущим.
        """
        path = self._find_by_id(knowledge_id)
        if path is None:
            return None

        # Атомарно (read-check-write под _git_lock): читаем, проверяем версию, обновляем
        async with self._git_lock:
            # Перечитываем с диска (на случай внешнего git pull / concurrent write)
            entry = self._parse_file(path)
            fm = entry.frontmatter

            # F2: Optimistic locking check
            if expected_version is not None and fm.version != expected_version:
                raise VersionConflictError(knowledge_id, expected_version, fm.version)

            if content is not None:
                entry.content = content
            if metadata:
                for key, value in metadata.items():
                    if hasattr(fm, key):
                        setattr(fm, key, value)

            fm.updated_at = datetime.now(timezone.utc)
            fm.version += 1

            entry = KnowledgeEntry(frontmatter=fm, content=entry.content)
            self._write_file(path, entry)

        # Git-аудит (вне блокировки — git сам сериализует)
        await self.flush(f"update: {knowledge_id} v{fm.version}")

        logger.info("update_entry: %s v%d", knowledge_id, fm.version)
        return entry

    async def delete(self, knowledge_id: str) -> bool:
        """Soft-delete: переместить в .trash/ + git commit."""
        path = self._find_by_id(knowledge_id)
        if path is None:
            return False

        trash_path = self._trash / f"{knowledge_id}.md"
        # Разрешаем конфликт: добавляем timestamp при дубликате в .trash
        if trash_path.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            trash_path = self._trash / f"{knowledge_id}.{ts}.md"

        path.rename(trash_path)
        await self.flush(f"delete: {knowledge_id} → .trash/")

        logger.info("delete_entry: %s → %s", knowledge_id, trash_path.name)
        return True

    async def list_entries(self, domain: str | None = None,
                           subject: str | None = None) -> list[str]:
        """Список knowledge_id в заданном домене/предмете (или все)."""
        scan_root = self._root
        if domain:
            scan_root = scan_root / domain
        if subject:
            scan_root = scan_root / subject

        if not scan_root.exists():
            return []

        ids = []
        for md_file in scan_root.rglob("*.md"):
            if ".trash" in md_file.parts:
                continue
            try:
                entry = self._parse_file(md_file)
                ids.append(entry.frontmatter.knowledge_id)
            except Exception:  # noqa: BLE001
                logger.warning("Пропущен битый файл: %s", md_file)
        return sorted(ids)

    async def reindex_scan(self) -> list[Path]:
        """Обход всех .md для полного reindex (задача 1.9)."""
        paths = []
        for md_file in self._root.rglob("*.md"):
            if ".trash" in md_file.parts or md_file.name.startswith("_"):
                continue
            paths.append(md_file)
        return sorted(paths)

    # ── Internal helpers ───────────────────────────────────

    def _resolve_path(self, relative: str) -> Path:
        return self._root / relative

    def _find_by_id(self, knowledge_id: str) -> Path | None:
        """Поиск .md по knowledge_id (обходит все директории)."""
        pattern = f"{knowledge_id}.md"
        # Быстрый путь: ищем по имени файла
        for md_file in self._root.rglob(pattern):
            if ".trash" in md_file.parts:
                continue
            return md_file
        return None

    def _parse_file(self, path: Path) -> KnowledgeEntry:
        """Разобрать .md файл: YAML frontmatter + контент."""
        text = path.read_text(encoding="utf-8")
        return self._parse_text(text)

    @staticmethod
    def _parse_text(text: str) -> KnowledgeEntry:
        """Разобрать текст с YAML frontmatter."""
        match = _FM_PATTERN.match(text)
        if not match:
            raise ValueError("Отсутствует YAML frontmatter (--- ... ---)")

        fm_raw = match.group(1)
        fm_dict = yaml.safe_load(fm_raw) or {}
        fm = KnowledgeFrontmatter(**fm_dict)

        # Контент — всё после закрывающего ---
        content = text[match.end():].strip()
        return KnowledgeEntry(frontmatter=fm, content=content)

    @staticmethod
    def _write_file(path: Path, entry: KnowledgeEntry) -> None:
        """Записать .md файл: YAML frontmatter + контент."""
        fm_dict = entry.frontmatter.model_dump(
            mode="json",
            exclude_none=True,
        )
        # Форматируем даты в ISO
        fm_dict["created_at"] = entry.frontmatter.created_at.isoformat()
        fm_dict["updated_at"] = entry.frontmatter.updated_at.isoformat()

        fm_yaml = yaml.dump(fm_dict, allow_unicode=True, default_flow_style=False,
                            sort_keys=False).strip()
        text = f"{_FM_DELIMITER}{fm_yaml}\n{_FM_DELIMITER}\n{entry.content}\n"
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _generate_id(domain: str, subject: str, content: str) -> str:
        """Авто-генерация knowledge_id: domain-subject-hash8."""
        # Берём первый заголовок ## как подсказку
        title_match = re.search(r"^##\s+(.+)$", content, re.MULTILINE)
        slug = ""
        if title_match:
            slug = re.sub(r"[^a-z0-9]+", "-", title_match.group(1).lower().strip())[:40]
            slug = slug.strip("-")
        if not slug:
            slug = hashlib.sha256(content[:200].encode()).hexdigest()[:8]

        return f"{domain}-{subject}-{slug}"[:128]

    async def flush(self, message: str) -> None:
        """Публичный метод: git add && git commit с защитой от гонки."""
        if self._repo is None or not settings.GIT_AUDIT:
            return

        async with self._git_lock:
            try:
                self._repo.git.add(all=True)
                self._repo.index.commit(message)
                # git gc --auto встроен (срабатывает автоматически при необходимости)
            except git.GitCommandError as e:
                logger.error("git commit failed: %s", e)
                # Не роняем write-операцию из-за git-ошибки

    async def _git_commit(self, message: str) -> None:
        """Deprecated: используйте flush()."""
        import warnings
        warnings.warn(
            "_git_commit() is deprecated, use flush()",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.flush(message)
