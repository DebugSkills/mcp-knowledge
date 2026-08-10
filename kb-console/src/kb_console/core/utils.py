"""Общие утилиты kb-console (чистые функции + shared рендер-хелперы)."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

# Расширения, поддерживаемые файловым импортом (.md/.txt, PDF — в перспективе).
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt"}
# Максимальный размер файла для импорта, байт (50 МБ — учебники).
# Формула: MAX_FILE_SIZE ≤ MCP_MAX_REQUEST_SIZE(128MB) − 20% JSON-overhead = 102MB.
# 50MB — консервативно, с запасом на content_type/metadata/JSON-encoding overhead.
MAX_FILE_SIZE = 52_428_800

# Уровни логов импорта → CSS-классы (канонический источник, синхронизирован
# с components/progress_panel.py:_LEVEL_COLORS). Единый SSOT для всех рендеров
# прогресса (import_page, replace_dialog).
_IMPORT_LEVEL_COLORS: dict[str, str] = {
    "info": "text-grey",
    "warning": "text-orange",
    "error": "text-negative",
}


def render_import_progress(snapshot: dict, container: ui.element) -> None:
    """Отрисовать прогресс-бар импорта и панель логов в заданном контейнере.

    Используется import_page.py и replace_dialog.py — единая реализация
    рендера прогресса import_content (DRY, Фаза 13.24+critic P1-2).

    Args:
        snapshot: Словарь прогресса (поля: imported, total, failed, status, messages[]).
        container: NiceGUI-контейнер для рендера (очищается перед отрисовкой).
    """
    container.clear()
    imported = snapshot.get("imported", 0)
    total = snapshot.get("total", 0)
    failed = snapshot.get("failed", 0)
    status = snapshot.get("status", "running")
    percent = (imported / total * 100) if total else 0
    done = status in ("done", "error")
    with container:
        ui.label(
            f"📊 Секция {imported}/{total} ({percent:.0f}%)"
            + (f"  ·  ошибок: {failed}" if failed else "")
            + (f"  ·  {status}" if done else "")
        ).classes("text-body2")
        ui.linear_progress(
            value=(imported / total) if total else 0,
        ).props("rounded").classes("w-full")
        msgs = snapshot.get("messages", [])
        if msgs:
            with ui.column().classes("w-full q-mt-xs gap-0"):
                for m in msgs[-8:]:
                    level = m.get("level", "info")
                    color = _IMPORT_LEVEL_COLORS.get(level, "text-grey")
                    ui.label(
                        f"[{m.get('t', '')}] {m.get('text', '')}"
                    ).classes(f"text-caption font-mono {color}")


def _read_uploaded_file(name: str, data: bytes) -> tuple[str | None, str | None]:
    """Прочитать загруженный файл в текст.

    Returns:
        (content, error): success → (text, None); failure → (None, error_msg).
    """
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None, f"Неподдерживаемый тип файла «{ext or 'без расширения'}». Ожидаются: .md, .markdown, .txt"
    if len(data) > MAX_FILE_SIZE:
        return None, f"Файл слишком большой (макс. {MAX_FILE_SIZE // 1_048_576} МБ)"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Пробуем windows-1251 для русскоязычных .txt из Windows.
        try:
            text = data.decode("windows-1251")
        except UnicodeDecodeError:
            return None, "Не удалось прочитать файл (кодировка не поддерживается)"
    return text, None


def _sanitize_title(t: str) -> str:
    """Санитизировать название книги: убрать пробелы и ведущие #.

    Args:
        t: Сырое название (может содержать пробелы, ведущие #).

    Returns:
        Чистое название (без ведущих #, без краевых пробелов).
        Пустая строка если после очистки ничего не осталось.
    """
    return t.strip().lstrip("#").strip()
