"""Locator-aware pipeline (Ф2a) — ядро извлечения локаторов.

План: plans/2026-10-02-bibliography-plan.md §3.2 (строки 122-149), фаза Ф2 (строка 335).

Контракт:
- ``Segment`` — атом адресуемого контента: ``locator`` + ``text``.
- ``LocatorExtractor`` — ABC: ``extract_segments(bytes|str) -> list[Segment]``.
  ``bytes`` — in-memory контент; ``str`` — путь к файлу (симметрично
  ``PDFPreprocessor.extract_text(source_path)``).
- Реестр экстракторов 1:1 по образцу ``content/registry.py``: register с
  отказом на дубликат, get со списком доступных kinds, list, reset.
- ``PDFLocatorExtractor`` (kind="page", 1-based int) — перенос per-page цикла
  ``pdf_preprocessor.py:211-231`` + OCR-fallback ``:247-307``.
- Сегментный кеш ``{full_sha256_canonical}.segments.v2.json``: полный sha256 —
  входной параметр ``content_sha256`` (прокидывается из ``document_store.put``,
  интеграция — Ф2b); при отсутствии вычисляется полный sha256 контента.
  Это НЕ v1 prefix-hash из ``_compute_content_hash`` (первые 64KB + размер) —
  тот допустим только для путей вне blob-store (план §3.2).
- Правило Л1 (provenance): спаны вычисляются в точке извлечения и никогда
  не выдумываются — «нет спанов → нет полей». Пустой результат допустим и
  корректен: страницы без текста (после экстракции и OCR-fallback) не порождают
  сегментов, локаторы к ним не фабрикуются.

P0-1 (async-инвариант): метод синхронный по контракту; блокирующие вызовы
(pdfplumber, рендер, Tesseract) при интеграции в async-пайплайн (Ф2b)
выполняются обёрткой ``run_in_executor`` — event loop не блокируется.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .pdf_preprocessor import MIN_TEXT_CHARS_PER_PAGE

logger = logging.getLogger("mcp_knowledge.content.locator")

# Версия сегментного кеша. v1 (`.txt`-checkpoint полного текста) в сегментном
# режиме ФИЗИЧЕСКИ не ищется: HIT v1 = MISS → переизвлечение (план §3.2).
SEGMENTS_CACHE_VERSION = 2


# ── Модели ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Locator:
    """Локатор — мульти-форматная ось адресации (план §3.2).

    kind="page" → int, 1-based страница canonical-PDF (у pdf ≡ original);
    kind="timestamp" → float сек; "image"/"sheet_row" → int (по спросу, Ф2+).
    ``display`` ОБЯЗАТЕЛЕН и формируется в ru-локали (напр. «с. 120–145»).
    """

    kind: str
    start: int | float
    end: int | float
    display: str

    @classmethod
    def page(cls, start: int, end: int | None = None) -> Locator:
        """Локатор страницы (1-based int) с display в ru-локали."""
        end = start if end is None else end
        display = f"с. {start}" if start == end else f"с. {start}–{end}"
        return cls(kind="page", start=start, end=end, display=display)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "display": self.display,
        }


@dataclass(frozen=True)
class Segment:
    """Атом адресуемого контента: локатор + ДОБЫТЫЙ текст (Л1 provenance)."""

    locator: Locator
    text: str

    def to_dict(self) -> dict:
        return {"locator": self.locator.to_dict(), "text": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> Segment:
        loc = data["locator"]
        return cls(
            locator=Locator(
                kind=loc["kind"],
                start=loc["start"],
                end=loc["end"],
                display=loc["display"],
            ),
            text=data["text"],
        )


# ── Контракт извлечения ─────────────────────────────────────────────────


class LocatorExtractor(ABC):
    """ABC экстрактора локаторов: source (bytes|str) → список Segment.

    ``content_sha256`` — полный sha256 canonical-артефакта (ключ сегментного
    кеша; прокидывается из ``document_store.put`` — Ф2b). Если не задан,
    экстрактор вычисляет полный sha256 контента сам.

    ``cancel_event`` — cooperативная отмена (перенос P0-1 из per-page цикла):
    проверяется между страницами и перед тяжёлым OCR.
    """

    kind: str

    @abstractmethod
    def extract_segments(
        self,
        source: bytes | str,
        *,
        content_sha256: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> list[Segment]:
        """Извлечь сегменты с локаторами.

        Л1: спаны вычисляются в точке извлечения, никогда не выдумываются.
        Нет извлекаемого контента → пустой список (корректный результат).
        Ошибка парсинга носителя пробрасывается наружу (без фабрикации).
        """


# ── Реестр экстракторов (1:1 по образцу content/registry.py:14-47) ──────

_registry: dict[str, LocatorExtractor] = {}


def register_locator_extractor(extractor: LocatorExtractor) -> None:
    """Зарегистрировать экстрактор для заданного locator kind."""
    kind = extractor.kind
    if kind in _registry:
        raise ValueError(
            f"Locator extractor for kind='{kind}' already registered"
        )
    _registry[kind] = extractor


def get_locator_extractor(kind: str) -> LocatorExtractor:
    """Получить экстрактор по kind.

    Raises:
        ValueError: если kind неизвестен, сообщение содержит список доступных.
    """
    extractor = _registry.get(kind)
    if extractor is None:
        available = list(_registry.keys())
        raise ValueError(
            f"Unknown locator kind: '{kind}'. "
            f"Available kinds: {available}"
        )
    return extractor


def list_locator_kinds() -> list[str]:
    """Вернуть список всех зарегистрированных locator kinds."""
    return sorted(_registry.keys())


def reset_locator_registry() -> None:
    """Очистить реестр (для тестов)."""
    _registry.clear()


# ── Полный sha256 + сегментный кеш ──────────────────────────────────────


def full_sha256(source: bytes | str | os.PathLike) -> str:
    """ПОЛНЫЙ sha256 контента — ключ сегментного кеша.

    bytes → хеш байтов; str | os.PathLike → путь к файлу → хеш всего файла
    чанками (Ф2b3: Path принимается явно — невнятный ``TypeError: object
    supporting the buffer API required`` вместо ошибки пути — footgun).
    В отличие от v1 prefix-hash (``_compute_content_hash``: первые 64KB +
    размер) не коллидирует на 64KB-близнецах — план §3.2 прямо требует
    полный sha256 canonical для сегментного кеша.
    """
    sha = hashlib.sha256()
    if isinstance(source, (bytes, bytearray)):
        sha.update(source)
    else:
        with open(os.fspath(source), "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
    return sha.hexdigest()


def segments_cache_filename(full_sha: str) -> str:
    """Имя файла сегментного кеша: ``{full_sha256_canonical}.segments.v2.json``."""
    return f"{full_sha}.segments.v{SEGMENTS_CACHE_VERSION}.json"


# ── PDF-экстрактор (kind="page") ────────────────────────────────────────


class PDFLocatorExtractor(LocatorExtractor):
    """Per-page экстрактор: страница PDF → Segment{locator: page N (1-based)}.

    Перенос per-page цикла ``pdf_preprocessor.py:211-231`` + OCR-fallback
    ``:247-307`` (порог ``MIN_TEXT_CHARS_PER_PAGE``, рендер 300 DPI, rus+eng).

    Осознанные отличия от v1 ``extract_text`` (план Ф2):
    - текст каждой страницы живёт в СВОЁМ сегменте полностью и без мутаций —
      никакого ``"\\n\\n".join`` с потерей границ страниц;
    - страницы без текста (экстракция и OCR дали пусто) НЕ порождают
      сегментов: локатор без контента = выдуманный спан (запрещён Л1);
    - сегментный кеш v2 (``.segments.v2.json``); v1 ``.txt``-checkpoint
      физически не ищется — HIT v1 = MISS → переизвлечение, НЕ «page=1».
    """

    kind = "page"

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        if cache_dir is None:
            cache_dir = Path(settings.PDF_IMPORT_CACHE_DIR) / "segments"
        self._cache_dir = Path(cache_dir)

    # ── Публичный API ──

    def extract_segments(
        self,
        source: bytes | str,
        *,
        content_sha256: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> list[Segment]:
        sha = content_sha256 or full_sha256(source)
        cache_path = self._cache_path(sha)
        cached = self._read_cache(cache_path)
        if cached is not None:
            logger.info("Segments cache HIT: %s → %d segments", sha[:12], len(cached))
            return cached

        segments = self._extract_per_page(source, cancel_event)
        self._write_cache(cache_path, sha, segments)
        return segments

    # ── Per-page цикл (перенос pdf_preprocessor.py:211-231) ──

    def _extract_per_page(
        self,
        source: bytes | str,
        cancel_event: asyncio.Event | None,
    ) -> list[Segment]:
        import pdfplumber

        if isinstance(source, (bytes, bytearray)):
            fp: object = io.BytesIO(bytes(source))
            close_fp = None
        else:
            close_fp = open(source, "rb")
            fp = close_fp
        try:
            with pdfplumber.open(fp) as pdf:
                segments: list[Segment] = []
                total_pages = len(pdf.pages)
                for page_idx, page in enumerate(pdf.pages):
                    # Cancel check (P0-1)
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError("PDF locator extraction cancelled")

                    page_text = self._extract_page_text(page, page_idx, cancel_event)
                    if not page_text.strip():
                        # Л1: нет текста → нет сегмента и нет локатора
                        continue
                    # 1-based: locator.start = номер страницы как в viewing PDF
                    segments.append(
                        Segment(locator=Locator.page(page_idx + 1), text=page_text)
                    )
                    if (page_idx + 1) % 20 == 0:
                        logger.debug(
                            "PDF locator extraction: page %d/%d", page_idx + 1, total_pages
                        )
                return segments
        finally:
            if close_fp is not None:
                close_fp.close()

    # ── Page text extraction: pdfplumber → OCR fallback (перенос :247-307) ──

    def _extract_page_text(
        self,
        page,
        page_idx: int,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Извлечь текст со страницы: pdfplumber → OCR если пусто.

        Синхронный порт: ``await run_in_executor(...)`` → прямой вызов
        (async-обёртка — Ф2b). Отказ OCR (нет tesseract/pypdfium2) — не
        ошибка: возвращается исходный (возможно пустой) текст.
        """
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.debug("pdfplumber extract_text failed page %d: %s", page_idx + 1, e)
            text = ""

        # Достаточно ли текста (не скан)
        if len(text.strip()) >= MIN_TEXT_CHARS_PER_PAGE:
            return text

        # Скан: OCR через Tesseract — проверка отмены перед тяжёлой операцией
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("PDF locator extraction cancelled before OCR")

        logger.info(
            "PDF page %d: low text (%d chars) → OCR", page_idx + 1, len(text.strip())
        )
        try:
            import pytesseract

            page_image = page.to_image(300)  # 300 DPI для качества OCR
            ocr_text = pytesseract.image_to_string(
                page_image.original, lang="rus+eng"
            )
            if ocr_text.strip():
                logger.info(
                    "PDF page %d: OCR extracted %d chars",
                    page_idx + 1, len(ocr_text.strip()),
                )
                return ocr_text
            logger.warning("PDF page %d: OCR returned empty text", page_idx + 1)
            return text
        except Exception as e:
            logger.warning("PDF page %d: OCR failed: %s", page_idx + 1, e)
            return text

    # ── Сегментный кеш v2 ──

    def _cache_path(self, sha: str) -> Path:
        return self._cache_dir / segments_cache_filename(sha)

    def _read_cache(self, cache_path: Path) -> list[Segment] | None:
        """Прочитать v2-кеш; отсутствие/повреждение/чужая версия = MISS.

        ВАЖНО (план §3.2): v1 ``{sha}.txt``-checkpoint здесь физически НЕ
        ищется — его наличие не влияет на результат (HIT v1 = MISS).
        """
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != SEGMENTS_CACHE_VERSION or raw.get("kind") != self.kind:
            return None
        try:
            return [Segment.from_dict(item) for item in raw["segments"]]
        except (KeyError, TypeError):
            logger.warning("Segments cache corrupt: %s → re-extract", cache_path.name)
            return None

    def _write_cache(self, cache_path: Path, sha: str, segments: list[Segment]) -> None:
        """Атомарная запись (tmp + os.replace); сбой записи не роняет извлечение."""
        payload = {
            "version": SEGMENTS_CACHE_VERSION,
            "kind": self.kind,
            "sha256": sha,
            "segments": [s.to_dict() for s in segments],
        }
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.parent / (cache_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cache_path)
            logger.info(
                "Segments cache written: %s (%d segments)", sha[:12], len(segments)
            )
        except OSError as e:
            logger.warning("Segments cache write failed (%s): %s", cache_path.name, e)


# ── Ф2b1: спаны локаторов секции + маппинг чанк→локаторы (план §3.2:127-147) ──


@dataclass(frozen=True)
class LocatorSpan:
    """Спан локатора внутри тела секции (план §3.2:127-131).

    ``offset_start``/``offset_end`` — полуоткрытый интервал [start, end)
    относительно тела секции КАК ОНО СЕРИАЛИЗУЕТСЯ В .md (entry.content после
    ``---``). Round-trip инвариант (§3.2:130): write→read возвращает те же
    offsets. Л1 provenance: спаны вычисляются в точке извлечения и никогда
    не выдумываются — «нет спанов → нет полей».
    """

    locator: Locator
    offset_start: int
    offset_end: int

    def to_dict(self) -> dict:
        return {
            "locator": self.locator.to_dict(),
            "offset_start": self.offset_start,
            "offset_end": self.offset_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LocatorSpan:
        loc = data["locator"]
        return cls(
            locator=Locator(
                kind=loc["kind"],
                start=loc["start"],
                end=loc["end"],
                display=loc["display"],
            ),
            offset_start=data["offset_start"],
            offset_end=data["offset_end"],
        )

    def intersects(self, char_start: int, char_end: int) -> bool:
        """span ∩ [char_start, char_end) ≠ ∅ (полуоткрытые интервалы).

        Касание границ (span [0,100), диапазон [100,110)) — НЕ пересечение.
        """
        return self.offset_start < char_end and char_start < self.offset_end


def spans_to_meta(spans: list[LocatorSpan]) -> list[dict]:
    """Сериализация спанов для Section.meta / frontmatter (locator_spans)."""
    return [s.to_dict() for s in spans]


def spans_from_meta(meta: dict) -> list[LocatorSpan] | None:
    """Прочитать спаны из meta секции / frontmatter.

    Возвращает None, если ключа ``locator_spans`` нет — «нет спанов → нет
    полей» (Л1): отсутствующее поле ≠ пустой список-заглушка.
    Повреждённые элементы игнорируются с предупреждением (не фабрикуем).
    """
    raw = meta.get("locator_spans")
    if raw is None:
        return None
    spans: list[LocatorSpan] = []
    for item in raw:
        try:
            spans.append(LocatorSpan.from_dict(item))
        except (KeyError, TypeError) as e:
            logger.warning("Corrupt locator span skipped: %s (%r)", e, item)
    return spans


def locators_for_range(
    spans: list[LocatorSpan],
    char_start: int,
    char_end: int,
) -> list[Locator]:
    """Маппинг по плану §3.2:147: ``{L : span(L) ∩ [char_start, char_end) ≠ ∅}``.

    - Чанк внутри одного спана → один локатор.
    - Чанк на стыке kind / стыке диапазонов → СПИСОК локаторов без
      схлопывания (разные kind и соседние диапазоны НЕ мержатся).
    - Overlap-чанк (диапазон покрывает несколько спанов) → объединение:
      все пересечённые локаторы входят (множество, дубликаты схлопываются —
      формула плана задаёт множество локаторов).
    - Порядок детерминирован: по (offset_start, offset_end, kind, start).
    """
    seen: set[Locator] = set()
    result: list[Locator] = []
    for span in sorted(
        spans,
        key=lambda s: (s.offset_start, s.offset_end, s.locator.kind, s.locator.start),
    ):
        if span.intersects(char_start, char_end) and span.locator not in seen:
            seen.add(span.locator)
            result.append(span.locator)
    return result


# ── Ф2b3: арифметика продюсера спанов (единственное место, без дублирования) ──


def page_spans_for_text(
    segments: list[Segment], separator: str = "\n\n"
) -> list[LocatorSpan]:
    """Спаны сегментов в координатах текста, собранного ``separator.join``.

    Ф2b3 (продюсер): full_text PDF-декомпозиции собирается из сегментов
    (``PDFLocatorExtractor.extract_segments`` — единственный источник
    разбиения по страницам). Эта функция единожды фиксирует арифметику
    «позиция сегмента в full_text», чтобы продюсер её не дублировал.
    """
    spans: list[LocatorSpan] = []
    pos = 0
    for seg in segments:
        spans.append(LocatorSpan(seg.locator, pos, pos + len(seg.text)))
        pos += len(seg.text) + len(separator)
    return spans


def clip_spans_to_range(
    spans: list[LocatorSpan], start: int, end: int
) -> list[LocatorSpan]:
    """Пересчитать спаны к поддиапазону [start, end) координат исходного текста.

    Ф2b3: тело секции занимает известный диапазон в координатах full_text
    (границы — нормализованного ``.strip()``-ом тела, КАК ОНО СЕРИАЛИЗУЕТСЯ
    в .md — контракт Ф2b1 round-trip). Пересекающие диапазон спаны
    пересчитываются относительно начала тела; непересекающиеся и пустые
    пересечения отбрасываются (Л1: не фабриковать).
    """
    clipped: list[LocatorSpan] = []
    for span in spans:
        new_start = max(span.offset_start, start)
        new_end = min(span.offset_end, end)
        if new_start < new_end:
            clipped.append(
                LocatorSpan(span.locator, new_start - start, new_end - start)
            )
    return clipped


def locators_for_chunk(chunk) -> list[Locator]:
    """Локаторы чанка: маппинг пересечения по его char-диапазону.

    Duck-typing: подходит models.Chunk и любой объект с атрибутами
    ``char_start``/``char_end``/``locator_spans`` (список dict-спанов секции).
    Л1: нет полей (спанов у секции не было / границы неизвестны) → []
    без фабрикации.
    """
    spans_raw = getattr(chunk, "locator_spans", None)
    char_start = getattr(chunk, "char_start", None)
    char_end = getattr(chunk, "char_end", None)
    if not spans_raw or char_start is None or char_end is None:
        return []
    spans: list[LocatorSpan] = []
    for item in spans_raw:
        try:
            spans.append(LocatorSpan.from_dict(item))
        except (KeyError, TypeError) as e:
            logger.warning("Corrupt locator span skipped: %s (%r)", e, item)
    return locators_for_range(spans, char_start, char_end)
