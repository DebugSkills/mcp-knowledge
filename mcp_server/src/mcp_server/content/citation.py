"""Citation-ядро (bibliography Ф4b1, план §3.4:179-192) — чистый модуль, без проводки в тулзы.

Два уровня цитирования (auth — параметр запроса; предикаты НЕ дублируются —
переиспользуются из tools/availability.py):
- **level_a** (метаданные, без проверки blob для viewer): Source доступен
  (`ref_available`: зона/статус/license) ∧ CSL достаточен (title ∧ authors) →
  `citation = {source_id, zone, title, authors, locator?, formatted(ГОСТ)}`.
- **level_b** (viewer): canonical blob физически есть (`canonical_present`) ∧
  `blob_available_indexed` (least-strict по ВСЕМ refs из SourceRefIndex) →
  дополнительно `{viewer_url, sha256}`.

Контракт §3.4:185-190: citation либо полон по уровню, либо ОТСУТСТВУЕТ ЦЕЛИКОМ
(`build_citation` → None; обёртка НЕ добавляет ключ) — частичный объект и
null-поле запрещены. URL-only Source (blobs нет) → level_a без viewer_url.
Недостаточный CSL / недоступный Source / нет source_id → citation отсутствует.

`viewer_url` строится из шаблона `viewer_url_tpl` (должен содержать `{sha256}`;
консоль-прокси и конфигурация — Ф4c, здесь только базовый `/documents/{sha256}`).

Никакой денормализации CSL в Qdrant payload: модуль читает SSOT-frontmatter
Source (dict) и ничего не пишет — правка Source (`update_source`) не требует
переиндексации.

`zone` — read-time поле citation из SSOT Source (Ф4-fix1 P1-1): консольный
viewer-гейт маркирует private-ссылки `?zone=private`; в Qdrant payload не
пишется (как и весь citation).

Импорты из `..tools.*` — lazy внутри функций (Ф4-fix1 P2-1): top-level импорт
создавал цикл `tools/__init__` → read/search → `citation_enrich` → этот модуль
(partially initialized); прямой `import mcp_server.content.citation` падал.

Persisted-причина отсутствия canonical (write-path канонизации пишет,
ядро только читает): `blobs.canonical_error = {reason, ...}`. Очередь pending
ВЫВОДИМА из SSOT (§4.6:322): canonical IS NULL ∧ формат документный ∧
ingest_policy_applied=normalize ∧ blob_exists(original) → `queued`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .canonicalizer import is_document_format

if TYPE_CHECKING:  # P2-1: разрыв цикла — runtime-импорты tools.* lazy в функциях
    from ..tools.source_ref_index import SourceRef, SourceRefIndex

#: Базовый шаблон viewer-ссылки (роут Ф4); консоль-прокси — Ф4c.
DEFAULT_VIEWER_URL_TPL = "/documents/{sha256}"

#: Persisted-причины отказа канонизации, входящие в 9 причин §3.4:192.
_CANONICAL_ERROR_REASONS = frozenset(
    {
        "queued",
        "conversion_failed",
        "conversion_timeout",
        "converter_unavailable",
        "quota_exceeded",
    }
)

_YEAR_RE = re.compile(r"\d{4}")


# ── Решение ────────────────────────────────────────────────


@dataclass(frozen=True)
class CitationDecision:
    """Структурированное решение: уровень цитирования + готовый citation.

    `level`/`citation` = None ⇔ citation опущен ЦЕЛИКОМ (контракт §3.4:185).
    """

    level: str | None  # "a" | "b" | None
    citation: dict | None

    @property
    def omitted(self) -> bool:
        """True ⇔ citation отсутствует (не частичный — отсутствует)."""
        return self.citation is None


def _ref_from_fm(source_fm: dict) -> SourceRef:
    """SourceRef (структура, не предикат) из SSOT-frontmatter dict Source."""
    from ..tools.source_ref_index import SourceRef  # P2-1: lazy — разрыв цикла

    return SourceRef(
        source_id=str(source_fm.get("source_id") or source_fm.get("knowledge_id") or ""),
        zone=source_fm.get("zone", "private"),
        status=source_fm.get("status", "published"),
        public_allowed=source_fm.get("public_allowed"),
        license=source_fm.get("license"),
    )


def decide_citation(
    source_fm: dict,
    locator: dict | None,
    auth,
    *,
    exists_fn,
    index: SourceRefIndex,
    viewer_url_tpl: str = DEFAULT_VIEWER_URL_TPL,
) -> CitationDecision:
    """Полное решение цитирования (уровень + citation).

    Шаги:
    1. Source доступен? (`ref_available` — зона/статус/license; И1) — иначе опущен.
    2. CSL достаточен? (`render_gost` ≠ None ⇔ title ∧ authors) — иначе опущен.
    3. level_b ⇔ `canonical_present` ∧ `blob_available_indexed` (least-strict
       по всем refs индекса; stale-индекс без разрешающего ref → level_a).
    """
    from ..tools.availability import (  # P2-1: lazy — разрыв цикла
        blob_available_indexed,
        canonical_present,
        ref_available,
    )

    ref = _ref_from_fm(source_fm)
    if not ref.source_id or not ref_available(ref, auth):
        return CitationDecision(None, None)

    formatted = render_gost(source_fm.get("bibliography"))
    if formatted is None:
        return CitationDecision(None, None)

    csl = source_fm.get("bibliography") or {}
    citation: dict = {
        "source_id": ref.source_id,
        "zone": ref.zone,  # P1-1: read-time зона Source для консольного viewer-гейта
        "title": str(csl.get("title") or "").strip(),
        "authors": _author_strings(csl),
        "formatted": formatted,
    }
    if isinstance(locator, dict) and locator:
        citation["locator"] = locator  # Л1: прокидывается как есть, не выдумывается

    blobs = source_fm.get("blobs")
    level = "a"
    if canonical_present(blobs, exists_fn):
        canonical = (blobs or {}).get("canonical") or {}
        sha256 = canonical.get("sha256")
        if sha256 and blob_available_indexed(sha256, auth, exists_fn=exists_fn, index=index):
            citation["viewer_url"] = viewer_url_tpl.format(sha256=sha256)
            citation["sha256"] = sha256
            level = "b"
    return CitationDecision(level, citation)


def build_citation(
    source_fm: dict,
    locator: dict | None,
    auth,
    *,
    exists_fn,
    index: SourceRefIndex,
    viewer_url_tpl: str = DEFAULT_VIEWER_URL_TPL,
) -> dict | None:
    """Citation для обёртки ответа: dict уровня a/b или None (опущен целиком).

    Обёртка (search/find_fragment/get_entry — проводка Ф4b2) НЕ добавляет ключ
    citation при None — это контракт §3.4 (частичный рендер запрещён).
    """
    # fmt: off
    return decide_citation(
        source_fm, locator, auth,
        exists_fn=exists_fn, index=index, viewer_url_tpl=viewer_url_tpl,
    ).citation
    # fmt: on


# ── ГОСТ-рендер ────────────────────────────────────────────


def _author_strings(csl: dict) -> list[str]:
    """CSL author[] → отображаемые строки («Фамилия И. О.» / literal as-is)."""
    authors = csl.get("author")
    if not isinstance(authors, list):
        return []
    out: list[str] = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        literal = str(author.get("literal") or "").strip()
        if literal:
            out.append(literal)
            continue
        family = str(author.get("family") or "").strip()
        given = str(author.get("given") or "").strip()
        if not family and not given:
            continue
        if family and given:
            initials = " ".join(f"{token[0]}." for token in given.split() if token)
            out.append(f"{family} {initials}".strip())
        else:
            out.append(family or given)
    return out


def _gost_author_list(authors: list[str]) -> str:
    """ГОСТ-список: ≤3 автора — через запятую; ≥4 — первый + «[и др.]»."""
    if len(authors) > 3:
        return f"{authors[0]} [и др.]"
    return ", ".join(authors)


def _extract_year(issued) -> str | None:
    """Год из CSL issued ({date-parts}|{literal}|str|int); None — не найден."""
    if issued is None:
        return None
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            return _extract_year(parts[0][0])
        return _extract_year(issued.get("literal"))
    match = _YEAR_RE.search(str(issued))
    return match.group(0) if match else None


def render_gost(csl_or_fm: dict | None) -> str | None:
    """«Авторы. Название. — Год. — Изд-во/URL» — устойчив к частичным данным.

    Принимает CSL-dict или frontmatter-dict Source (распаковывает `bibliography`).
    Обязательные поля — authors ∧ title: чего-то нет → None (не фабриковать).
    Год/изд-во/URL — опциональные сегменты « — …»; изд-во приоритетнее URL.
    """
    csl = csl_or_fm
    if isinstance(csl, dict) and isinstance(csl.get("bibliography"), dict):
        csl = csl["bibliography"]
    if not isinstance(csl, dict):
        return None

    title = str(csl.get("title") or "").strip()
    authors = _author_strings(csl)
    if not title or not authors:
        return None

    authors_str = _gost_author_list(authors)
    # ГОСТ-разделитель авторы→заглавие: «. » если блок ещё не кончается точкой.
    if not authors_str.endswith("."):
        authors_str += "."
    segments = [f"{authors_str} {title}"]
    year = _extract_year(csl.get("issued"))
    if year:
        segments.append(year)
    publisher = str(csl.get("publisher") or "").strip()
    url = str(csl.get("URL") or csl.get("url") or "").strip()
    if publisher:
        segments.append(publisher)
    elif url:
        segments.append(f"URL: {url}")
    return ". — ".join(segments)


# ── Reason-классификация canonical_present=false (§3.4:192) ──


def classify_reason(source_fm: dict, *, exists_fn, index: SourceRefIndex) -> str | None:
    """Ровно одна из 9 причин отсутствия canonical; None ⇔ canonical есть.

    Приоритет детерминирован (убывание диагностической ценности; integrity
    аномалии громче проектных причин — потеря данных важнее оси формата):

    1. `canonical_present` (auth-free, переиспользован) → None.
    2. `blob_missing` — FM обещает blob (canonical.sha256 или original.sha256),
       которого нет в сторе: integrity-инцидент §3.4:192.
    3. `url_no_blobs` — original в FM отсутствует целиком (URL-only §3.1:120;
       физического обещания нет).
    4. `outside_pdf_axis` — формат вне PDF-оси (media/код/таблицы §4.1:245).
    5. `policy_pdf_only` — аварийный fallback pdf_only (журнал
       ingest_policy_applied, §4.6:324).
    6. `blobs.canonical_error.reason` ∈ whitelist {queued, conversion_failed,
       conversion_timeout, converter_unavailable, quota_exceeded} — persisted
       отказ канонизации; непредставимые субкоды → conversion_failed.
    7. `queued` — SSOT-derived pending §4.6:322: документный ∧ normalize ∧
       original жив ∧ canonical нет.
    8. Fallback `blob_missing` — аномальное состояние (pdf без canonical и пр.)
       = integrity-инцидент.

    `index` принят для единообразия инъекционной тройки проводки Ф4b2
    (exists_fn/index); причины выводимы из SSOT-frontmatter + exists_fn.
    """
    from ..tools.availability import canonical_present  # P2-1: lazy — разрыв цикла

    blobs = source_fm.get("blobs")
    if canonical_present(blobs, exists_fn):
        return None

    canonical = (blobs or {}).get("canonical") if isinstance(blobs, dict) else None
    canonical_sha = canonical.get("sha256") if isinstance(canonical, dict) else None
    original = blobs.get("original") if isinstance(blobs, dict) else None
    original_sha = original.get("sha256") if isinstance(original, dict) else None

    # 2. integrity-инцидент: обещанный blob потерян.
    if (canonical_sha and not exists_fn(canonical_sha)) or (original_sha and not exists_fn(original_sha)):
        return "blob_missing"
    # 3. физического обещания нет вообще.
    if not original_sha:
        return "url_no_blobs"

    fmt = source_fm.get("format")
    # 4. ось формата — вне канонизации.
    if fmt != "pdf" and not is_document_format(fmt):
        return "outside_pdf_axis"
    # 5. аварийный fallback политики.
    if source_fm.get("ingest_policy_applied") == "pdf_only":
        return "policy_pdf_only"
    # 6. persisted отказ канонизации.
    error = blobs.get("canonical_error") if isinstance(blobs, dict) else None
    if isinstance(error, dict) and error.get("reason"):
        reason = str(error["reason"])
        return reason if reason in _CANONICAL_ERROR_REASONS else "conversion_failed"
    # 7. SSOT-derived pending (§4.6:322).
    if fmt != "pdf" and is_document_format(fmt) and source_fm.get("ingest_policy_applied") == "normalize":
        return "queued"
    # 8. аномалия → integrity-инцидент.
    return "blob_missing"
