"""INDEX.gen.yaml generation (#30, #32) — структурная карта знаний.

Задача 1.11 плана Фазы 1 (~250 строк).

Генерирует:
- root INDEX.gen.yaml (≤4 KB): sections[], top-20 tags, how_to_orient, completeness%
- per-section _INDEX.gen.yaml (≤8 KB): files[], domain_tags, gap_analysis, adaptive L3

Триггеры:
1. Полная перестройка при reconciliation (старт)
2. Инкрементальное обновление затронутой секции при write/update/delete
3. При structural change (rename/move/delete директории) → полная root + обе секции

Gap analysis (#32):
- top-5 missing (записи без tags/domain/subject)
- suggested_tags (frequency-based + path-based)
- coverage% (доля записей с заполненным frontmatter)

In-memory cache с инвалидацией (P1-1)
Size enforcement с truncation (P1-3)
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import settings
from ..models import KnowledgeEntry
from ..storage.markdown_store import MarkdownStore

logger = logging.getLogger("mcp_knowledge.index")

# Лимиты (P1-3)
MAX_ROOT_TAGS = 20
MAX_SECTION_TAGS = 30
MAX_FILES_PER_SECTION = 20
ROOT_SIZE_LIMIT = 4096  # bytes
SECTION_SIZE_LIMIT = 8192  # bytes

# Обязательные поля frontmatter для gap analysis
REQUIRED_FIELDS = ["domain", "subject", "tags"]


class KnowledgeIndex:
    """Генератор INDEX.gen.yaml — структурная карта Markdown SSOT."""

    def __init__(self, store: MarkdownStore):
        self._store = store
        self._root = Path(settings.KNOWLEDGE_ROOT)
        # In-memory cache (P1-1): key = section_name or "__root__"
        self._cache: dict[str, dict] = {}

    # ── Public API ─────────────────────────────────────────

    def rebuild_all(self) -> dict:
        """Полная перестройка root + всех per-section индексов.

        Вызывается при reconciliation (старт) и после reindex.
        """
        logger.info("INDEX: полная перестройка root + per-section")

        # Сканируем все .md файлы
        entries_by_section = self._scan_entries_by_section()

        # Root INDEX
        root_index = self._build_root_index(entries_by_section)
        self._write_index(self._root / "INDEX.gen.yaml", root_index, ROOT_SIZE_LIMIT)
        self._cache["__root__"] = root_index

        # Per-section индексы
        section_indices = {}
        for section_name, entries in entries_by_section.items():
            section_index = self._build_section_index(section_name, entries)
            section_path = self._root / section_name / "_INDEX.gen.yaml"
            section_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_index(section_path, section_index, SECTION_SIZE_LIMIT)
            section_indices[section_name] = section_index
            self._cache[section_name] = section_index

        logger.info("INDEX: перестроено — root + %d секций", len(section_indices))
        result = {"root": root_index, "sections": section_indices}
        return result

    def update_section(self, section_name: str) -> dict:
        """Инкрементальное обновление per-section индекса + root.

        Вызывается при write/update/delete.
        """
        logger.info("INDEX: инкрементальное обновление секции %s", section_name)

        # Пересканируем все секции для root (root зависит от всех)
        entries_by_section = self._scan_entries_by_section()

        # Обновляем root
        root_index = self._build_root_index(entries_by_section)
        self._write_index(self._root / "INDEX.gen.yaml", root_index, ROOT_SIZE_LIMIT)
        self._invalidate("__root__")
        self._cache["__root__"] = root_index

        # Обновляем затронутую секцию
        entries = entries_by_section.get(section_name, [])
        section_index = self._build_section_index(section_name, entries)
        section_path = self._root / section_name / "_INDEX.gen.yaml"
        section_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_index(section_path, section_index, SECTION_SIZE_LIMIT)
        self._invalidate(section_name)
        self._cache[section_name] = section_index

        return {"root": root_index, "section": section_index}

    def handle_structural_change(self, old_section: str, new_section: str):
        """Обработка rename/move/delete директории (P1-5).

        Полная перестройка root + обеих затронутых секций.
        """
        logger.info("INDEX: structural change %s → %s", old_section, new_section)
        entries_by_section = self._scan_entries_by_section()

        # Root
        root_index = self._build_root_index(entries_by_section)
        self._write_index(self._root / "INDEX.gen.yaml", root_index, ROOT_SIZE_LIMIT)
        self._invalidate("__root__")
        self._cache["__root__"] = root_index

        # Старая секция (если ещё существует)
        for section_name in (old_section, new_section):
            if section_name:
                entries = entries_by_section.get(section_name, [])
                section_index = self._build_section_index(section_name, entries)
                section_path = self._root / section_name / "_INDEX.gen.yaml"
                if section_path.parent.exists() or entries:
                    section_path.parent.mkdir(parents=True, exist_ok=True)
                    self._write_index(section_path, section_index, SECTION_SIZE_LIMIT)
                    self._invalidate(section_name)
                    self._cache[section_name] = section_index

    def get_map(self, domain: str | None = None) -> dict:
        """Получить структурную карту (root или per-section).

        Использует in-memory cache с ленивой загрузкой при промахе.
        """
        if domain:
            # Per-section
            if domain in self._cache:
                return self._cache[domain]
            # Ленивая загрузка с диска
            section_path = self._root / domain / "_INDEX.gen.yaml"
            if section_path.exists():
                data = yaml.safe_load(section_path.read_text(encoding="utf-8")) or {}
                self._cache[domain] = data
                return data
            return {}
        else:
            # Root
            if "__root__" in self._cache:
                return self._cache["__root__"]
            root_path = self._root / "INDEX.gen.yaml"
            if root_path.exists():
                data = yaml.safe_load(root_path.read_text(encoding="utf-8")) or {}
                self._cache["__root__"] = data
                return data
            return {}

    # ── Internal ───────────────────────────────────────────

    def _scan_entries_by_section(self) -> dict[str, list[KnowledgeEntry]]:
        """Сканировать все .md и сгруппировать по секциям (domain)."""
        sections: dict[str, list[KnowledgeEntry]] = {}
        paths = []
        for md_file in self._root.rglob("*.md"):
            if ".trash" in md_file.parts or md_file.name.startswith("_"):
                continue
            paths.append(md_file)

        for path in sorted(paths):
            try:
                entry = self._store._parse_file(path)
                fm = entry.frontmatter
                section = fm.domain
                sections.setdefault(section, []).append(entry)
            except Exception as e:  # noqa: BLE001
                logger.debug("Пропущен битый файл при INDEX gen: %s — %s", path, e)

        return sections

    def _build_root_index(self, sections: dict[str, list[KnowledgeEntry]]) -> dict:
        """Build root INDEX.gen.yaml (≤4 KB)."""
        all_tags: Counter = Counter()
        total_entries = 0
        entries_with_all_fields = 0
        section_list = []

        for section_name, entries in sorted(sections.items()):
            section_list.append({
                "section": section_name,
                "files": len(entries),
                "path": section_name,
            })
            for entry in entries:
                total_entries += 1
                fm = entry.frontmatter
                for tag in fm.tags:
                    all_tags[tag] += 1
                for tag in fm.cross_subjects:
                    all_tags[tag] += 1

                # Проверка заполненности frontmatter
                has_all = all(
                    getattr(fm, field, None) for field in REQUIRED_FIELDS
                )
                if has_all:
                    entries_with_all_fields += 1

        # Top-N tags с truncation (P1-3)
        top_tags = all_tags.most_common(MAX_ROOT_TAGS)
        tag_list = [{"tag": t, "count": c} for t, c in top_tags]
        remaining = len(all_tags) - len(top_tags)
        if remaining > 0:
            tag_list.append({"tag": f"...and {remaining} more", "count": 0})

        completeness = round(entries_with_all_fields / max(total_entries, 1) * 100, 1)

        return {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_entries": total_entries,
            "completeness_pct": completeness,
            "sections": section_list,
            "top_tags": tag_list,
            "how_to_orient": (
                "Используйте get_knowledge_map(domain) для просмотра секции. "
                "search_by_tags() для точного поиска по тегам. "
                "search_knowledge() для семантического поиска по контенту."
            ),
        }

    def _build_section_index(self, section_name: str,
                             entries: list[KnowledgeEntry]) -> dict:
        """Build per-section _INDEX.gen.yaml (≤8 KB)."""
        files = []
        domain_tags: Counter = Counter()
        missing_frontmatter: list[dict] = []
        total_entries = len(entries)

        for entry in entries:
            fm = entry.frontmatter
            files.append({
                "knowledge_id": fm.knowledge_id,
                "subject": fm.subject,
                "project": fm.project,
                "tags": fm.tags,
                "version": fm.version,
                "updated_at": fm.updated_at.isoformat(),
            })

            for tag in fm.tags:
                domain_tags[tag] += 1
            for tag in fm.cross_subjects:
                domain_tags[tag] += 1

            # Gap analysis: проверка обязательных полей
            missing = []
            for field in REQUIRED_FIELDS:
                value = getattr(fm, field, None)
                if not value or (isinstance(value, list) and len(value) == 0):
                    missing.append(field)
            if missing:
                missing_frontmatter.append({
                    "knowledge_id": fm.knowledge_id,
                    "missing_fields": missing,
                    "suggested_tags": self._suggest_tags(fm, domain_tags, section_name),
                })

        # Top-N tags с truncation
        top_tags = domain_tags.most_common(MAX_SECTION_TAGS)
        tag_list = [{"tag": t, "count": c} for t, c in top_tags]
        remaining = len(domain_tags) - len(top_tags)
        if remaining > 0:
            tag_list.append({"tag": f"...and {remaining} more", "count": 0})

        # Top-5 missing
        gap_analysis = {
            "top_5_missing": missing_frontmatter[:5],
            "total_missing": len(missing_frontmatter),
            "coverage_pct": round(
                (total_entries - len(missing_frontmatter)) / max(total_entries, 1) * 100, 1
            ),
        }

        result = {
            "section": section_name,
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_files": len(files),
            "domain_tags": tag_list,
            "gap_analysis": gap_analysis,
        }

        # Adaptive L3: при >20 файлов группировка по subject/project (P1-3)
        if len(files) > MAX_FILES_PER_SECTION:
            result["adaptive_l3"] = self._build_l3_grouping(files)

        result["files"] = files
        return result

    def _build_l3_grouping(self, files: list[dict]) -> dict:
        """L3-группировка при >MAX_FILES_PER_SECTION файлов."""
        by_subject: dict[str, list[str]] = {}
        for f in files:
            subject = f.get("subject", "_ungrouped")
            by_subject.setdefault(subject, []).append(f["knowledge_id"])

        return {
            "grouped_by": "subject",
            "subjects": {
                subj: {"count": len(ids), "ids": ids[:10]}  # truncate to 10
                for subj, ids in sorted(by_subject.items())
            },
            "note": f"Секция содержит >{MAX_FILES_PER_SECTION} файлов. "
                    f"Используйте get_knowledge_map(domain={files[0].get('domain', '')})"
                    f" + subject фильтрацию.",
        }

    def _suggest_tags(self, fm, domain_tags: Counter,
                      section_name: str) -> list[str]:
        """suggested_tags алгоритм (P1-4): frequency-based + path-based."""
        existing = set(fm.tags) | set(fm.cross_subjects)
        suggested = []

        # (a) Frequency-based: top-5 частых тегов секции минус существующие
        for tag, _ in domain_tags.most_common(10):
            if tag not in existing and len(suggested) < 5:
                suggested.append(tag)

        # (b) Path-based: извлечение тегов из пути директории
        # domain, subject, project → kebab-case теги
        path_parts = []
        if fm.domain:
            path_parts.append(fm.domain)
        if fm.subject:
            path_parts.append(fm.subject)
        if fm.project:
            path_parts.append(fm.project)

        for part in path_parts:
            # Нормализация: заменяем _-/ на дефисы
            candidate = part.lower().replace("_", "-").replace("/", "-")
            if candidate not in existing and candidate not in suggested:
                suggested.append(candidate)
                if len(suggested) >= 7:
                    break

        return suggested[:7]  # Не больше 7 suggested тегов

    # ── Cache helpers ──────────────────────────────────────

    def _invalidate(self, key: str):
        """Инвалидировать запись in-memory кэша (P1-1)."""
        if key in self._cache:
            del self._cache[key]

    def invalidate_all(self):
        """Инвалидировать весь кэш."""
        self._cache.clear()

    # ── File I/O ──────────────────────────────────────────

    @staticmethod
    def _write_index(path: Path, data: dict, size_limit: int):
        """Записать INDEX.gen.yaml с валидацией размера (P1-3)."""
        yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                               sort_keys=False)
        size = len(yaml_text.encode("utf-8"))

        if size > size_limit:
            logger.warning(
                "INDEX size %d bytes > limit %d — автоматическое усечение",
                size, size_limit
            )
            # Усекаем файлы/теги до лимита
            if "files" in data and len(data.get("files", [])) > 10:
                data["files"] = data["files"][:10]
                data["_truncated"] = True
                data["_truncated_note"] = f"Показаны первые 10 из {len(data.get('files', []))} файлов"
            yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                                   sort_keys=False)

        path.write_text(yaml_text, encoding="utf-8")
        assert len(yaml_text.encode("utf-8")) <= size_limit * 1.5, (
            f"INDEX {path} size {len(yaml_text.encode('utf-8'))} > {size_limit * 1.5}"
        )
