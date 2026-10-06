"""Общий загрузчик секций ``-- @script {name}`` из SSOT Lua-файлов (P2-5).

trace_id: arch-2026-10-05-ai-workspace (ревизия критика Ф4, P2-5). До
консолидации идентичный ``_script()`` жил в ``queue.py`` и
``scheduler/admission.py``, а свой вариант ``_section()`` (с префиксом
``_COMMON``) — в ``slots.py``: три копии одного парсера. Один загрузчик,
семантика прежняя: секция — от строки-маркера до следующего маркера/конца
файла, тело ``strip()`` + ``"\\n"``; ``prefix`` (общие Lua-хелперы, slots)
добавляется к каждой секции.
"""

from __future__ import annotations

MARKER = "-- @script "
"""Строка-маркер начала секции (``-- @script <name>``)."""


def extract_sections(
    source: str,
    *,
    prefix: str = "",
    source_name: str = "<lua>",
) -> dict[str, str]:
    """Нарезать Lua-файл на секции ``{name: prefix + тело}``.

    ``source_name`` — только для сообщений об ошибках (fail-loud при
    дубликате маркера: файл — SSOT, дубликаты скрывали бы секции друг
    друга). Маркер признаётся только в начале строки.
    """
    sections: dict[str, str] = {}
    pos = 0
    while True:
        start = source.find(MARKER, pos)
        if start < 0:
            break
        if start != 0 and source[start - 1] != "\n":
            pos = start + len(MARKER)
            continue  # упоминание маркера в комментарии, не секция
        line_end = source.find("\n", start)
        name = source[start + len(MARKER) : line_end].strip()
        if name in sections:
            raise RuntimeError(f"{source_name}: дубликат секции {MARKER}{name}")
        next_marker = source.find("\n-- @script ", line_end)
        body_end = next_marker if next_marker > 0 else len(source)
        sections[name] = prefix + source[line_end + 1 : body_end].strip() + "\n"
        pos = body_end
    return sections


def section_of(
    sections: dict[str, str],
    name: str,
    *,
    source_name: str = "<lua>",
) -> str:
    """Секция по имени; ``RuntimeError`` (fail-loud), если отсутствует."""
    try:
        return sections[name]
    except KeyError:
        raise RuntimeError(
            f"{source_name}: секция {MARKER}{name!r} не найдена"
        ) from None
