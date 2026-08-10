"""PDFPreprocessor — препроцессор для content_type="pdf".

13.21: Извлечение текста через pdfplumber + OCR (Tesseract) для сканов.
Декомпозиция по font-size заголовкам (>1.3x median) + fallback постранично.
Checkpoint/resume: кеш извлечённого текста по content_hash.

P0-1: OCR через run_in_executor (не блокирует event loop).
P0-2: Cache prune перед импортом (TTL + LRU max size).
"""

# ruff: noqa: BLE001
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time as _time
from pathlib import Path

from ..config import settings
from .keywords import deduplicate_tags, extract_keywords
from .linking import make_knowledge_id
from .preprocessor import ContentPreprocessor, ImportMeta, Section, ValidationResult

logger = logging.getLogger("mcp_knowledge.content.pdf_preprocessor")

# Порог для определения заголовка: char size > MEDIAN * HEADING_RATIO
HEADING_RATIO = 1.3
# Минимум символов текста на странице, чтобы считать её текстовой (не скан)
MIN_TEXT_CHARS_PER_PAGE = 20


class PDFPreprocessor(ContentPreprocessor):
    """Препроцессор для content_type="pdf" — PDF-файлы через pdfplumber + Tesseract OCR."""

    content_type = "pdf"

    def __init__(self):
        self._max_pages = settings.MAX_PDF_PAGES
        self._max_size = settings.MAX_PDF_FILE_SIZE
        self._cache_dir = self._resolve_cache_dir(settings.PDF_IMPORT_CACHE_DIR)
        self._cache_max_age_days = settings.PDF_IMPORT_CACHE_MAX_AGE_DAYS
        self._cache_max_size_mb = settings.PDF_IMPORT_CACHE_MAX_SIZE_MB

    @staticmethod
    def _resolve_cache_dir(preferred: str) -> str:
        """Resolve cache dir — fallback to /tmp if Docker path unavailable."""
        import tempfile
        try:
            Path(preferred).mkdir(parents=True, exist_ok=True)
            return preferred
        except (PermissionError, OSError):
            return tempfile.mkdtemp(prefix="pdf_cache_")

    # ── Validation ─────────────────────────────────────────

    def validate(self, content: str, metadata: ImportMeta) -> ValidationResult:
        """Проверка PDF: существование файла, не encrypted, лимиты страниц/размера."""
        source_path = metadata.source_path
        if not source_path or not os.path.exists(source_path):
            return ValidationResult(
                valid=False,
                error="PDF source file not found. Provide source_path in ImportMeta.",
                content_size=0,
            )

        # Проверка размера файла
        try:
            file_size = os.path.getsize(source_path)
        except OSError as e:
            return ValidationResult(
                valid=False,
                error=f"Cannot read PDF file: {e}",
                content_size=0,
            )

        if file_size > self._max_size:
            return ValidationResult(
                valid=False,
                error=(
                    f"PDF file too large: {file_size / (1024 * 1024):.1f} MB "
                    f"(max {self._max_size / (1024 * 1024):.0f} MB)"
                ),
                content_size=file_size,
            )

        # Открываем PDF для проверки encrypted + числа страниц
        try:
            import pdfplumber
            pdf = pdfplumber.open(source_path)
        except Exception as e:
            error_msg = str(e).lower()
            error_type = type(e).__name__.lower()
            if ("password" in error_msg or "encrypt" in error_msg
                    or "permission" in error_msg or "pdfminer" in error_type):
                return ValidationResult(
                    valid=False,
                    error=(
                        "PDF защищён паролем — расшифруйте и загрузите снова. "
                        "Пароли не хранятся на сервере."
                    ),
                    content_size=file_size,
                )
            return ValidationResult(
                valid=False,
                error=f"Cannot open PDF: {e}",
                content_size=file_size,
            )

        try:
            page_count = len(pdf.pages)
            if page_count == 0:
                pdf.close()
                return ValidationResult(
                    valid=False,
                    error="PDF has 0 pages",
                    content_size=file_size,
                )

            if page_count > self._max_pages:
                pdf.close()
                return ValidationResult(
                    valid=False,
                    error=(
                        f"PDF too many pages: {page_count} "
                        f"(max {self._max_pages})"
                    ),
                    content_size=file_size,
                )

            estimated_sections = max(page_count, 1)
            pdf.close()
            return ValidationResult(
                valid=True,
                content_size=file_size,
                estimated_sections=estimated_sections,
            )
        except Exception as e:
            pdf.close()
            return ValidationResult(
                valid=False,
                error=f"PDF validation error: {e}",
                content_size=file_size,
            )

    # ── Decomposition ──────────────────────────────────────

    async def decompose(
        self,
        content: str,
        metadata: ImportMeta,
        cancel_event: asyncio.Event | None = None,
    ) -> list[Section]:
        """Извлечение текста из PDF → декомпозиция на секции.

        Phase 1: extract text per-page (pdfplumber + OCR fallback).
        Phase 2: heading detection (font-size >1.3x median → new section).
        Fallback: per-page sections if <2 headings.

        cancel_event: проверяется между страницами (P0-1).
        """
        source_path = metadata.source_path
        if not source_path:
            raise ValueError("source_path is required for PDF decomposition")

        # ── Checkpoint: content_hash → cache lookup ─────────
        content_hash = self._compute_content_hash(source_path)
        cache_dir = Path(self._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{content_hash}.txt"

        # [P0-2] Prune cache before writing new entry
        await self._prune_pdf_cache(cache_dir)

        if cache_path.exists():
            logger.info(
                "PDF checkpoint HIT: %s → reading cached text", content_hash[:12]
            )
            full_text = cache_path.read_text(encoding="utf-8")
        else:
            # ── Extract text per-page ─────────────────────────
            import pdfplumber

            pdf = pdfplumber.open(source_path)
            pages_text: list[str] = []
            total_pages = len(pdf.pages)

            try:
                for page_idx, page in enumerate(pdf.pages):
                    # Cancel check (P0-1)
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError("PDF import cancelled")

                    # Yield event loop между страницами (P0-1)
                    await asyncio.sleep(0)

                    page_text = await self._extract_page_text(page, page_idx, cancel_event)
                    if page_text.strip():
                        pages_text.append(page_text)
                    else:
                        # Пустая страница — сохраняем как разделитель
                        pages_text.append("")

                    if (page_idx + 1) % 20 == 0:
                        logger.debug(
                            "PDF extraction: page %d/%d", page_idx + 1, total_pages
                        )
            finally:
                pdf.close()

            full_text = "\n\n".join(pages_text)

            # Сохраняем checkpoint
            cache_path.write_text(full_text, encoding="utf-8")
            logger.info(
                "PDF checkpoint written: %s (%d chars, %d pages)",
                content_hash[:12], len(full_text), total_pages,
            )

        # ── Heading detection + decomposition ────────────────
        return await self._build_sections(full_text, metadata, source_path)

    # ── Page text extraction (pdfplumber + OCR fallback) ──

    async def _extract_page_text(
        self,
        page,
        page_idx: int,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Извлечь текст со страницы: pdfplumber → OCR если пусто.

        P0-1: OCR через run_in_executor (НЕ блокирует event loop).
        Возвращает текстовое представление страницы.
        """

        # Пробуем pdfplumber текст
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.debug("pdfplumber extract_text failed page %d: %s", page_idx + 1, e)
            text = ""

        # Проверяем, достаточно ли текста (не скан)
        if len(text.strip()) >= MIN_TEXT_CHARS_PER_PAGE:
            return text

        # ── Скан: OCR через Tesseract ────────────────────
        # Проверяем cancel_event перед тяжёлой операцией
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("PDF import cancelled before OCR")

        logger.info("PDF page %d: low text (%d chars) → OCR", page_idx + 1, len(text.strip()))

        try:
            # Рендер страницы в изображение через pdfplumber (Pillow image)
            loop = asyncio.get_running_loop()
            page_image = await loop.run_in_executor(
                None, page.to_image, 300  # 300 DPI для качества OCR
            )

            # P0-1: OCR в executor
            import pytesseract

            ocr_text = await loop.run_in_executor(
                None,
                lambda: pytesseract.image_to_string(
                    page_image.original, lang="rus+eng"
                ),
            )
            # Yield после тяжёлой операции
            await asyncio.sleep(0)

            if ocr_text.strip():
                logger.info(
                    "PDF page %d: OCR extracted %d chars",
                    page_idx + 1, len(ocr_text.strip()),
                )
                return ocr_text
            else:
                logger.warning("PDF page %d: OCR returned empty text", page_idx + 1)
                return text  # возвращаем что было (пустая строка)
        except Exception as e:
            logger.warning("PDF page %d: OCR failed: %s", page_idx + 1, e)
            return text

    # ── Content hash for checkpoint ────────────────────────

    def _compute_content_hash(self, source_path: str) -> str:
        """SHA256 первых 64KB + размера файла → уникальный ID контента."""
        sha = hashlib.sha256()
        try:
            with open(source_path, "rb") as f:
                chunk = f.read(65536)  # первые 64KB
                sha.update(chunk)
            sha.update(str(os.path.getsize(source_path)).encode())
        except (OSError, FileNotFoundError):
            # For nonexistent files, hash the path itself
            sha.update(source_path.encode())
        return sha.hexdigest()

    # ── Cache prune (P0-2) ─────────────────────────────────

    async def _prune_pdf_cache(self, cache_dir: Path) -> int:
        """Удалить устаревшие/избыточные checkpoint-файлы.

        Критерии:
        1. Старше PDF_IMPORT_CACHE_MAX_AGE_DAYS дней
        2. Суммарный размер > PDF_IMPORT_CACHE_MAX_SIZE_MB → LRU (oldest mtime first)

        P0-2: предотвращает disk exhaustion.
        Возвращает число удалённых файлов.
        """
        if not cache_dir.exists():
            return 0

        max_age_seconds = self._cache_max_age_days * 86400
        max_size_bytes = self._cache_max_size_mb * 1024 * 1024

        now = _time.time()
        removed = 0

        try:
            files = sorted(
                cache_dir.glob("*.txt"),
                key=lambda p: p.stat().st_mtime,
            )

            total_size = 0
            keep_files: list[Path] = []

            for fp in files:
                try:
                    st = fp.stat()
                    age = now - st.st_mtime
                    # Критерий 1: возраст
                    if age > max_age_seconds:
                        fp.unlink()
                        removed += 1
                        logger.debug("PDF cache prune (age): %s (%.1f days)", fp.name, age / 86400)
                        continue
                    # Критерий 2: размер (LRU — удаляем oldest если превышен лимит)
                    total_size += st.st_size
                    keep_files.append(fp)
                except OSError:
                    continue

            # LRU eviction: если суммарный размер > лимита, удаляем oldest
            while total_size > max_size_bytes and keep_files:
                oldest = keep_files.pop(0)
                try:
                    st = oldest.stat()
                    oldest.unlink()
                    total_size -= st.st_size
                    removed += 1
                    logger.debug(
                        "PDF cache prune (LRU size): %s (total=%d MB)",
                        oldest.name, total_size // (1024 * 1024),
                    )
                except OSError:
                    pass

            if removed:
                logger.info("PDF cache prune: %d files removed", removed)
        except Exception as exc:
            logger.warning("PDF cache prune failed (non-fatal): %s", exc)

        return removed

    # ── Section building (heading detection + chunking) ────

    async def _build_sections(
        self,
        full_text: str,
        metadata: ImportMeta,
        source_path: str,
    ) -> list[Section]:
        """Декомпозиция извлечённого текста в секции.

        Стратегия:
        1. Собрать font-size из оригинального PDF через pdfplumber chars
        2. Heading = chars с size > 1.3× median
        3. Текст между heading-boundaries → секция
        4. Fallback: <2 headings → постранично
        5. Chunking: oversized секции → hybrid_split
        """

        # Собираем font-size информацию из PDF
        heading_boundaries = await self._detect_headings(source_path)

        if not heading_boundaries or len(heading_boundaries) < 2:
            # Fallback: per-page decomposition
            return await self._fallback_per_page(full_text, metadata)

        # Собираем секции по heading boundaries
        return await self._sections_by_headings(full_text, heading_boundaries, metadata)

    async def _detect_headings(self, source_path: str) -> list[tuple[int, str]]:
        """Обнаружить заголовки по font-size эвристике.

        Returns:
            [(char_position, heading_text), ...] — границы секций.
        """
        import pdfplumber

        all_chars: list[dict] = []
        pdf = pdfplumber.open(source_path)
        try:
            for page in pdf.pages:
                chars = page.chars
                if chars:
                    all_chars.extend(chars)
        finally:
            pdf.close()

        if not all_chars:
            return []

        # Собираем размеры шрифтов
        sizes = [c.get("size", 0) for c in all_chars if c.get("size", 0) > 0]
        if not sizes:
            return []

        median_size = sorted(sizes)[len(sizes) // 2]
        heading_threshold = median_size * HEADING_RATIO

        # Находим строки с крупным шрифтом (heading candidates)
        headings: list[tuple[int, str]] = []
        current_heading_chars: list[dict] = []

        for char in all_chars:
            char_size = char.get("size", 0)

            if char_size >= heading_threshold:
                current_heading_chars.append(char)
            elif current_heading_chars:
                # Завершили heading-блок
                text = "".join(c.get("text", "") for c in current_heading_chars).strip()
                if text and len(text) > 3:
                    # Примерная позиция (x0 страницы/документа)
                    current_heading_chars[0].get("x0", 0) + sum(
                        p.get("page_number", 0) * 10000
                        for p in current_heading_chars
                    )
                    headings.append((len(text), text))  # упрощённо: позиция = длина текста до этого места
                current_heading_chars = []

        # Упрощённый подход: возвращаем заголовки как (индекс, текст)
        # Реальная позиция будет определена при разборе полного текста
        heading_lines: list[tuple[int, str]] = []
        for h_text in [h[1] for h in headings]:
            # Ищем позицию этого текста в полном извлечённом тексте
            idx = 0
            heading_lines.append((idx, h_text))

        return heading_lines

    async def _fallback_per_page(
        self, full_text: str, metadata: ImportMeta
    ) -> list[Section]:
        """Fallback: каждая страница → одна секция."""
        from .splitting import hybrid_split

        pages = full_text.split("\n\n")
        sections: list[Section] = []
        seq = 0

        for page_text in pages:
            pt = page_text.strip()
            if not pt:
                continue
            seq += 1

            # Chunking oversized текста
            if len(pt) > 4000:
                chunks = hybrid_split(pt, max_tokens=512, token_counter=None)
            else:
                chunks = [pt]

            for chunk_idx, chunk in enumerate(chunks):
                section_title = (
                    f"Страница {seq}" if chunk_idx == 0
                    else f"Страница {seq} (часть {chunk_idx + 1})"
                )
                knowledge_id = make_knowledge_id(
                    metadata.domain,
                    metadata.subject,
                    section_title,
                    seq,
                    metadata.title or "pdf_import",
                )
                keywords = extract_keywords(chunk, top_n=5)
                tags = deduplicate_tags(
                    metadata.tags + keywords,
                    metadata.cross_subjects,
                )

                sections.append(Section(
                    title=section_title,
                    body=chunk,
                    sequence_number=seq,
                    tags=tags,
                    meta={
                        "knowledge_id": knowledge_id,
                        "domain": metadata.domain,
                        "subject": metadata.subject,
                        "project": metadata.project,
                        "content_type": "book",
                        "cross_subjects": metadata.cross_subjects,
                    },
                ))

        return sections

    async def _sections_by_headings(
        self,
        full_text: str,
        heading_boundaries: list[tuple[int, str]],
        metadata: ImportMeta,
    ) -> list[Section]:
        """Разбиение текста по обнаруженным заголовкам."""

        # Упрощённая реализация: разбиваем по заголовкам в полном тексте
        sections: list[Section] = []
        seq = 0

        # Если заголовков нет в тексте — fallback per-page
        remaining = full_text
        for heading_idx, heading_text in heading_boundaries:
            # Ищем heading в remaining тексте
            pos = remaining.find(heading_text)
            if pos < 0:
                continue

            body = remaining[:pos].strip()
            if body:
                seq += 1
                sections.extend(
                    self._make_sections_from_text(body, heading_text, seq, metadata)
                )

            # Двигаемся дальше
            remaining = remaining[pos + len(heading_text):]

        # Последний блок
        if remaining.strip():
            seq += 1
            sections.extend(
                self._make_sections_from_text(
                    remaining, "Последний раздел", seq, metadata
                )
            )

        if not sections:
            # Fallback если ничего не получилось
            return await self._fallback_per_page(full_text, metadata)

        return sections

    def _make_sections_from_text(
        self,
        body: str,
        title: str,
        seq: int,
        metadata: ImportMeta,
    ) -> list[Section]:
        """Создать секцию(и) из текстового блока с chunking."""
        from .splitting import hybrid_split

        sections: list[Section] = []

        if len(body) > 4000:
            chunks = hybrid_split(body, max_tokens=512, token_counter=None)
        else:
            chunks = [body]

        for chunk_idx, chunk in enumerate(chunks):
            section_title = title if chunk_idx == 0 else f"{title} (часть {chunk_idx + 1})"
            knowledge_id = make_knowledge_id(
                metadata.domain,
                metadata.subject,
                section_title,
                seq,
                metadata.title or "pdf_import",
            )
            keywords = extract_keywords(chunk, top_n=5)
            # Flatten: ensure keywords is list[str] not list[list]
            flat_keywords = [
                k for k in keywords if isinstance(k, str)
            ]
            tags = deduplicate_tags(
                metadata.tags + flat_keywords,
                metadata.cross_subjects,
            )

            sections.append(Section(
                title=section_title,
                body=chunk,
                sequence_number=seq,
                tags=tags,
                meta={
                    "knowledge_id": knowledge_id,
                    "domain": metadata.domain,
                    "subject": metadata.subject,
                    "project": metadata.project,
                    "content_type": "book",
                    "cross_subjects": metadata.cross_subjects,
                },
            ))

        return sections
