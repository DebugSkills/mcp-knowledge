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
import re
import time as _time
from bisect import bisect_left
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

# Порог минимальной длины нормализованного чанка для локализации (fail-safe Л1):
# короче — чанк неоднозначен, спаны не фабрикуются.
MIN_NORM = 24

_WS_COLLAPSE_RE = re.compile(r"\s+")


def _collapse(s: str) -> str:
    """Схлопнуть серии пробельных в один пробел.

    Единственная доказанная мутация splitting.py (split + strip + " ".join) —
    схлопывание пробельных серий на границах предложений. Нормализация
    идентична с обеих сторон (body и chunk), поэтому поиск по нормали
    эквивалентен поиску по исходному тексту с точной картой позиций.
    """
    return _WS_COLLAPSE_RE.sub(" ", s)


def normalize_with_map(s: str) -> tuple[str, list[int]]:
    """Нормализовать пробелы и построить карту позиций norm -> source.

    Returns:
        (norm, pos_map): ``norm = _collapse(s)`` (каждая серия пробельных
        заменена одним пробелом); ``pos_map[i]`` — индекс в ``s`` символа,
        которому соответствует ``norm[i]`` (для схлопнутого пробела — индекс
        ПЕРВОГО символа серии); ``pos_map[len(norm)] = len(s)`` — сентинел
        конца матча. ``pos_map`` строго монотонен (bisect-инвертируем).
    """
    norm = _collapse(s)
    pos_map: list[int] = []
    s_pos = 0
    for run in _WS_COLLAPSE_RE.finditer(s):
        pos_map.extend(range(s_pos, run.start()))
        pos_map.append(run.start())
        s_pos = run.end()
    pos_map.extend(range(s_pos, len(s)))
    pos_map.append(len(s))  # sentinel
    return norm, pos_map


def locate_chunk(
    body: str,
    chunk: str,
    cursor: int,
    norm_body: str,
    map_body: list[int],
    cursor_norm: int,
    prev_end: int,
) -> tuple[int, int] | None:
    """Локализовать чанк в теле секции (fast-path verbatim -> нормализованный).

    Возвращает ``(start, end)`` — полуоткрытый интервал в координатах
    ``body``, либо ``None`` (fail-safe Л1: нелокализуемый/короткий/
    немонотонный чанк -> спанов нет, фабрикации нет).

    - fast-path: ``body.find(chunk, cursor)`` — вербатимные чанки (сохраняет
      текущие golden-тесты байт-в-байт);
    - иначе: ``norm_body.find(_collapse(chunk).strip(), cursor_norm)`` с
      обратным маппингом через ``map_body``;
    - ``len(nc) < MIN_NORM`` -> ``None`` (короткий неоднозначный чанк);
    - монотонность: ``start < prev_end`` -> ``None`` (защитный guard).
    """
    # fast-path: вербатимное вхождение
    p = body.find(chunk, cursor)
    if p >= 0:
        end = p + len(chunk)
        return (p, end) if p >= prev_end else None

    # нормализованный поиск
    nc = _collapse(chunk).strip()
    if len(nc) < MIN_NORM:
        return None
    p = norm_body.find(nc, cursor_norm)
    if p < 0:
        return None
    start = map_body[p]
    end = map_body[p + len(nc)]
    if start < prev_end:
        return None
    return (start, end)


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

        Phase 1 (Ф2b3, сегментный режим): PDFLocatorExtractor.extract_segments —
        единственный источник разбиения по страницам; спаны локаторов и
        source_id закладываются в Section.meta в точке извлечения (Л1).
        v1 ``.txt``-checkpoint в сегментном режиме НЕ читается (план §3.2:
        HIT v1 = MISS → переизвлечение), но пишется для совместимости
        потребителей extract_text (convert/extract_pdf_text).
        Phase 2: heading detection (font-size >1.3x median → new section).
        Fallback: per-page sections if <2 headings.

        Реестр без page-экстрактора → легаси-v1 путь (extract_text) БЕЗ
        спанов: декомпозиция жива, локаторы не фабрикуются (Л1).

        cancel_event: проверяется между страницами (P0-1).
        """
        source_path = metadata.source_path
        if not source_path:
            raise ValueError("source_path is required for PDF decomposition")

        # Phase 1: сегменты (per-page) через реестр экстракторов локаторов
        segments = await self._extract_locator_segments(
            source_path, metadata.content_sha256, cancel_event
        )

        page_spans = None
        source_id = None
        if segments is not None:
            from .locator import full_sha256, page_spans_for_text  # lazy: цикл импортов

            sha = metadata.content_sha256
            if sha is None:
                # pdf canonical ≡ original (Л3): полный sha256 того же файла
                loop = asyncio.get_running_loop()
                sha = await loop.run_in_executor(None, full_sha256, source_path)
            from .source import make_source_id  # Ф1-хелпер: src-<sha256_16>

            source_id = make_source_id(sha)

            full_text = "\n\n".join(seg.text for seg in segments)
            page_spans = page_spans_for_text(segments)

            # v1-checkpoint совместимость: запись (НЕ чтение)
            await self._write_v1_checkpoint_compat(
                source_path, full_text, len(segments)
            )
        else:
            # Легаси-v1: полный текст с checkpoint-кешем (спанов нет — Л1)
            full_text = await self.extract_text(source_path, cancel_event)

        # Phase 2: heading detection + decomposition
        return await self._build_sections(
            full_text, metadata, source_path,
            page_spans=page_spans, source_id=source_id,
        )

    async def _extract_locator_segments(
        self,
        source_path: str,
        content_sha256: str | None,
        cancel_event: asyncio.Event | None = None,
    ):
        """Ф2b3: сегменты по страницам через реестр экстракторов локаторов.

        P0-1: контракт экстрактора синхронный (внутри возможен OCR) →
        run_in_executor. Возвращает None, если page-экстрактор не
        зарегистрирован (реестр сброшен тестом / окружение без
        авторегистрации) — вызывающий уходит в легаси-v1 путь без спанов.
        """
        from .locator import get_locator_extractor

        try:
            extractor = get_locator_extractor("page")
        except ValueError as exc:
            logger.warning(
                "PDF decompose: page-экстрактор не зарегистрирован → "
                "легаси-v1 путь без спанов (%s)", exc,
            )
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: extractor.extract_segments(
                source_path, content_sha256=content_sha256, cancel_event=cancel_event
            ),
        )

    async def _write_v1_checkpoint_compat(
        self, source_path: str, full_text: str, pages: int
    ) -> None:
        """Записать v1 ``{prefix_hash}.txt``-checkpoint из сегментного текста.

        Сегментный режим v1-кеш НЕ ЧИТАЕТ (план §3.2: HIT v1 = MISS →
        переизвлечение) — запись нужна для совместимости extract_text
        (convert/extract_pdf_text) и регрессии checkpoint-тестов.
        """
        cache_dir = Path(self._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        await self._prune_pdf_cache(cache_dir)
        content_hash = self._compute_content_hash(source_path)
        (cache_dir / f"{content_hash}.txt").write_text(full_text, encoding="utf-8")
        logger.info(
            "PDF checkpoint written: %s (%d chars, %d pages)",
            content_hash[:12], len(full_text), pages,
        )

    async def extract_text(
        self,
        source_path: str,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Извлечь полный текст PDF (pdfplumber + OCR fallback) с checkpoint-кешем.

        Phase 1 decompose: cache lookup по content_hash → pdfplumber per-page
        извлечение → OCR fallback для сканов → запись checkpoint.

        Reusable: используется decompose() (импорт) и extract_pdf_text tool
        (конвертация PDF→текст для авто-классификации на клиенте).

        cancel_event: проверяется между страницами (P0-1).
        """
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

        return full_text


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
        page_spans: list | None = None,
        source_id: str | None = None,
    ) -> list[Section]:
        """Декомпозиция извлечённого текста в секции.

        Стратегия:
        1. Собрать font-size из оригинального PDF через pdfplumber chars
        2. Heading = chars с size > 1.3× median
        3. Текст между heading-boundaries → секция
        4. Fallback: <2 headings → постранично
        5. Chunking: oversized секции → hybrid_split

        Ф2b3: page_spans (координаты full_text) + source_id — продюсер
        спанов локаторов; None → ключи у секций не появляются (Л1).
        """

        # Собираем font-size информацию из PDF
        heading_boundaries = await self._detect_headings(source_path)

        if not heading_boundaries or len(heading_boundaries) < 2:
            # Fallback: per-page decomposition
            return await self._fallback_per_page(
                full_text, metadata, page_spans=page_spans, source_id=source_id
            )

        # Собираем секции по heading boundaries
        return await self._sections_by_headings(
            full_text, heading_boundaries, metadata,
            page_spans=page_spans, source_id=source_id,
        )

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
        self,
        full_text: str,
        metadata: ImportMeta,
        page_spans: list | None = None,
        source_id: str | None = None,
    ) -> list[Section]:
        """Fallback: каждая страница → одна секция.

        Ф2b3: в сегментном режиме страницы берутся из page_spans (спаны
        сегментов в координатах full_text) — РЕАЛЬНЫЕ границы страниц, а не
        эвристика split("\n\n"), ломавшаяся на пустых строках внутри
        страницы. Каждая секция (в т.ч. chunk-часть длинной страницы)
        целиком происходит со своей страницы → её спан [0, len(body)) —
        атрибуция по построению, точная даже при мутациях hybrid_split.
        """
        from .splitting import hybrid_split

        if page_spans is not None:
            from .locator import LocatorSpan, spans_to_meta

            pages: list[tuple[str, object]] = [
                (full_text[span.offset_start:span.offset_end], span)
                for span in page_spans
            ]
        else:
            pages = [(page_text, None) for page_text in full_text.split("\n\n")]
        sections: list[Section] = []
        seq = 0

        for page_text, span in pages:
            pt = page_text.strip()
            if not pt:
                continue
            seq += 1

            # Chunking oversized текста
            if len(pt) > 4000:
                chunks = await hybrid_split(
                    pt, embedder=None, max_tokens=512, token_counter=None
                )
                # hybrid_split возвращает Chunk[] — нормализуем до текста
                chunk_texts = [c.body for c in chunks]
            else:
                chunk_texts = [pt]

            for chunk_idx, chunk in enumerate(chunk_texts):
                section_title = (
                    f"Страница {seq}" if chunk_idx == 0
                    else f"Страница {seq} (часть {chunk_idx + 1})"
                )
                content_hash = hashlib.sha256(chunk[:200].encode()).hexdigest()
                knowledge_id = make_knowledge_id(
                    metadata.domain,
                    metadata.subject,
                    section_title,
                    seq,
                    content_hash,
                )
                # V3 13.26: extract_keywords ждёт list[str] — одна строка ломала
                # извлечение (итерировались символы) — теги были мёртвыми
                keywords = extract_keywords([chunk], top_n=5)[0]
                # Flatten: ensure keywords is list[str] not list[list]
                flat_keywords = [
                    k for k in keywords if isinstance(k, str)
                ]
                tags = deduplicate_tags(
                    metadata.tags + flat_keywords,
                    metadata.cross_subjects,
                )

                meta = {
                    "knowledge_id": knowledge_id,
                    "domain": metadata.domain,
                    "subject": metadata.subject,
                    "project": metadata.project,
                    # Ф2b4.D (план, строка 335): PDF-секции — content_type="pdf",
                    # не "book". Источник — контракт препроцессора.
                    "content_type": self.content_type,
                    "cross_subjects": metadata.cross_subjects,
                }
                # Ф2b3 (Л1): спаны — только при реальной странице; ключи
                # locator_spans/source_id появляются и исчезают вместе
                if span is not None and source_id is not None:
                    meta["locator_spans"] = spans_to_meta(
                        [LocatorSpan(span.locator, 0, len(chunk))]
                    )
                    meta["source_id"] = source_id

                sections.append(Section(
                    title=section_title,
                    body=chunk,
                    sequence_number=seq,
                    tags=tags,
                    meta=meta,
                ))

        return sections

    async def _sections_by_headings(
        self,
        full_text: str,
        heading_boundaries: list[tuple[int, str]],
        metadata: ImportMeta,
        page_spans: list | None = None,
        source_id: str | None = None,
    ) -> list[Section]:
        """Разбиение текста по обнаруженным заголовкам.

        Ф2b3: base — абсолютное смещение ``remaining`` в full_text, чтобы
        для каждого тела секции вычислить его диапазон (границы
        нормализованного .strip()-ом тела) и клиппировать page_spans.
        """

        # Упрощённая реализация: разбиваем по заголовкам в полном тексте
        sections: list[Section] = []
        seq = 0

        # Если заголовков нет в тексте — fallback per-page
        remaining = full_text
        base = 0
        for heading_idx, heading_text in heading_boundaries:
            # Ищем heading в remaining тексте
            pos = remaining.find(heading_text)
            if pos < 0:
                continue

            raw_body = remaining[:pos]
            body = raw_body.strip()
            if body:
                seq += 1
                body_range = None
                if page_spans is not None:
                    lead = len(raw_body) - len(raw_body.lstrip())
                    body_range = (base + lead, base + lead + len(body))
                sections.extend(
                    await self._make_sections_from_text(
                        body, heading_text, seq, metadata,
                        body_range=body_range, page_spans=page_spans,
                        source_id=source_id,
                    )
                )

            # Двигаемся дальше
            remaining = remaining[pos + len(heading_text):]
            base = base + pos + len(heading_text)

        # Последний блок
        raw_tail = remaining
        tail = raw_tail.strip()
        if tail:
            seq += 1
            body_range = None
            if page_spans is not None:
                lead = len(raw_tail) - len(raw_tail.lstrip())
                body_range = (base + lead, base + lead + len(tail))
            sections.extend(
                await self._make_sections_from_text(
                    tail, "Последний раздел", seq, metadata,
                    body_range=body_range, page_spans=page_spans,
                    source_id=source_id,
                )
            )

        if not sections:
            # Fallback если ничего не получилось
            return await self._fallback_per_page(
                full_text, metadata, page_spans=page_spans, source_id=source_id
            )

        return sections

    async def _make_sections_from_text(
        self,
        body: str,
        title: str,
        seq: int,
        metadata: ImportMeta,
        body_range: tuple[int, int] | None = None,
        page_spans: list | None = None,
        source_id: str | None = None,
    ) -> list[Section]:
        """Создать секцию(и) из текстового блока с chunking.

        Ф2b3: body_range — диапазон тела (нормализованного) в координатах
        full_text; chunk-секции локализуются в теле поиском с курсором
        (монотонность). Мутирующий hybrid_split (join предложений) может
        сделать чанк нелокализуемым → у этого чанка спанов НЕТ (Л1:
        неточные offsets не фабрикуются).
        """
        from .splitting import hybrid_split

        sections: list[Section] = []
        track_spans = page_spans is not None and body_range is not None

        if len(body) > 4000:
            chunks = await hybrid_split(
                body, embedder=None, max_tokens=512, token_counter=None
            )
            # hybrid_split возвращает Chunk[] — нормализуем до текста
            chunk_texts = [c.body for c in chunks]
        else:
            chunk_texts = [body]

        if track_spans:
            from .locator import clip_spans_to_range, spans_to_meta

            norm_body, map_body = normalize_with_map(body)
        else:
            norm_body, map_body = "", []

        cursor = 0
        cursor_norm = 0
        prev_end = 0
        unlocated_chunks = 0
        for chunk_idx, chunk in enumerate(chunk_texts):
            section_title = title if chunk_idx == 0 else f"{title} (часть {chunk_idx + 1})"
            content_hash = hashlib.sha256(chunk[:200].encode()).hexdigest()
            knowledge_id = make_knowledge_id(
                metadata.domain,
                metadata.subject,
                section_title,
                seq,
                content_hash,
            )
            # V3 13.26: extract_keywords ждёт list[str] — иначе теги мертвы
            keywords = extract_keywords([chunk], top_n=5)[0]
            # Flatten: ensure keywords is list[str] not list[list]
            flat_keywords = [
                k for k in keywords if isinstance(k, str)
            ]
            tags = deduplicate_tags(
                metadata.tags + flat_keywords,
                metadata.cross_subjects,
            )

            meta = {
                "knowledge_id": knowledge_id,
                "domain": metadata.domain,
                "subject": metadata.subject,
                "project": metadata.project,
                # Ф2b4.D (план, строка 335): PDF-секции — content_type="pdf",
                # не "book". Источник — контракт препроцессора.
                "content_type": self.content_type,
                "cross_subjects": metadata.cross_subjects,
            }
            if track_spans:
                located = locate_chunk(
                    body, chunk, cursor,
                    norm_body, map_body, cursor_norm, prev_end,
                )
                if located is not None:
                    start, end = located
                    cursor = end
                    cursor_norm = bisect_left(map_body, end)
                    prev_end = end
                    clipped = clip_spans_to_range(
                        page_spans, body_range[0] + start,
                        body_range[0] + end,
                    )
                    # Л1: ключи появляются только при непустом пересечении
                    if clipped and source_id is not None:
                        meta["locator_spans"] = spans_to_meta(clipped)
                        meta["source_id"] = source_id
                else:
                    unlocated_chunks += 1

            sections.append(Section(
                title=section_title,
                body=chunk,
                sequence_number=seq,
                tags=tags,
                meta=meta,
            ))

        if track_spans and unlocated_chunks:
            logger.warning(
                "PDF section '%s': %d/%d chunks unlocated (no locator spans, Л1)",
                title, unlocated_chunks, len(chunk_texts),
            )

        return sections
