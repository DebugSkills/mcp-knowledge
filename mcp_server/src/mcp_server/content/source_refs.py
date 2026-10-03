"""Ф3c2a (trace code-2026-10-02-bibliography): source_refs-хелперы import-пути.

План §3.4: при batch-write секций import_content собирает из Section.meta
(``source_id`` + ``locator_spans`` — формат content.locator.spans_to_meta,
locator.py:437-439) ссылки Knowledge-записи на Source — поле frontmatter
``source_refs``:

    [{"source_id": "src-<sha256_16>",
      "locator": {"kind": "page", "start": 3, "end": 5}}]

Правила (Л1 provenance — «нет данных → нет ключа»):
- группировка по ``(source_id, kind)`` → ОДИН покрывающий диапазон на группу:
  ``{start: min, end: max}`` по всем спанам группы; ``display`` НЕ копируется
  (производная от диапазона — пересчитывается потребителем);
- ``source_id`` без спанов → ``{"source_id": sid}`` БЕЗ ключа ``locator``:
  ``exclude_none`` в MarkdownStore._write_file не рекурсирует в list[dict],
  поэтому «пустой локатор» обязан отсутствовать как ключ, а не быть null;
- нет ``source_id`` → ``None`` (ключ source_refs не пишется в YAML).

Merge при переимпорте (детерминированные make_knowledge_id → те же ids):
дедуп по ``(source_id, kind|None)``, существующие refs выигрывают
(existing-wins — ручные/Ф3c2b правки не затираются), чужие Source-ссылки
(foreign source_id) сохраняются.
"""

from __future__ import annotations

# Ключ дедупа/группировки ref-а: (source_id, locator.kind|None).
# ref без locator (source_id без спанов) → kind=None — отдельный ключ.
RefKey = tuple[str, "str | None"]


def refs_from_section_meta(meta: dict | None) -> list[dict] | None:
    """Section.meta → список source_refs для frontmatter секции.

    Args:
        meta: Section.meta продюсера декомпозиции (pdf_preprocessor и др.).
            Учитываются ключи ``source_id`` и ``locator_spans``.

    Returns:
        - ``None`` — ``source_id`` нет (Л1: поле source_refs не пишется);
        - ``[{"source_id": sid}]`` — source есть, спанов нет;
        - список ref-ов по одному на ``(source_id, kind)`` с покрывающим
          диапазоном — спаны есть.
    """
    meta = meta or {}
    source_id = meta.get("source_id")
    if not source_id:
        return None

    by_key: dict[RefKey, dict] = {}
    for span in meta.get("locator_spans") or []:
        loc = (span or {}).get("locator") or {}
        kind = loc.get("kind")
        start, end = loc.get("start"), loc.get("end")
        if not kind or start is None or end is None:
            continue  # Л1: малформированный спан не фабрикует ref
        ref = by_key.get((source_id, kind))
        if ref is None:
            by_key[(source_id, kind)] = {
                "source_id": source_id,
                "locator": {"kind": kind, "start": start, "end": end},
            }
        else:
            covering = ref["locator"]
            covering["start"] = min(covering["start"], start)
            covering["end"] = max(covering["end"], end)

    if not by_key:
        return [{"source_id": source_id}]
    return list(by_key.values())


def _ref_key(ref: dict | None) -> RefKey:
    loc = (ref or {}).get("locator") or {}
    return (ref.get("source_id") or "", loc.get("kind"))


def merge_source_refs(
    existing: list[dict] | None,
    incoming: list[dict] | None,
) -> list[dict] | None:
    """Дедуп по ``(source_id, kind|None)``; existing-wins; None-безопасно.

    Порядок: существующие первыми (в своём порядке — foreign не сдвигаются),
    затем новые ключи в порядке входа. Оба аргумента пусты (None/[]) →
    ``None`` (ключ source_refs в YAML не пишется — Л1).
    """
    existing = existing or None
    incoming = incoming or None
    if existing is None and incoming is None:
        return None
    if existing is None:
        return list(incoming)
    if incoming is None:
        return list(existing)

    seen = {_ref_key(r) for r in existing}
    merged = list(existing)
    for ref in incoming:
        key = _ref_key(ref)
        if key in seen:
            continue  # existing-wins: входящий диапазон того же ключа отбрасывается
        seen.add(key)
        merged.append(ref)
    return merged
