"""Общие утилиты kb-console (чистые функции + shared рендер-хелперы)."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

# Расширения, поддерживаемые файловым импортом (.md/.txt/.pdf).
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}
# Маркер для PDF-файлов — не декодируются как текст (13.21)
PDF_BINARY_MARKER = "__PDF_BINARY__"
# Максимальный размер файла для текстового импорта, байт (50 МБ).
# PDF-файлы передаются через multipart — лимит на сервере (100MB).
MAX_FILE_SIZE = 52_428_800

# Уровни логов импорта → CSS-классы (канонический источник, синхронизирован
# с components/progress_panel.py:_LEVEL_COLORS). Единый SSOT для всех рендеров
# прогресса (import_page, replace_dialog).
_IMPORT_LEVEL_COLORS: dict[str, str] = {
    "info": "text-grey",
    "warning": "text-orange",
    "error": "text-negative",
}


# Ф5a2: 5 whitelist-причин отсутствия canonical (citation.py:_CANONICAL_ERROR_REASONS, §3.4:192).
# Непредставимые субкоды сервер маппит в conversion_failed; здесь — человекочитаемый текст.
_CANONICAL_REASON_TEXT: dict[str, str] = {
    "queued": "канонизация поставлена в очередь",
    "conversion_failed": "конвертация в PDF не удалась",
    "conversion_timeout": "конвертация превысила таймаут",
    "converter_unavailable": "конвертер недоступен",
    "quota_exceeded": "квота хранилища документов превышена",
}


def canonical_reason_text(reason: str | None) -> str:
    """Человекочитаемая причина отсутствия canonical (Ф5a2).

    Неизвестная/отсутствующая причина возвращается как есть (не падает —
    forward-compatible; KeyError не бросаем).
    """
    if not reason:
        return ""
    return _CANONICAL_REASON_TEXT.get(reason, reason)


def human_size(n: float | None) -> str:
    """Человекочитаемый размер в байтах (Ф5c2): B/KB/MB/GB/TB.

    None/нечисло → «—» (sparse-безопасно: не выдумываем размер).
    """
    if n is None:
        return "—"
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return "—"


def short_sha(sha: str | None) -> str:
    """Короткий вид sha256 (Ф5c2): первые 12 символов + «…». Пусто/None → «»."""
    if not isinstance(sha, str) or not sha:
        return ""
    return sha[:12] + "…"


def _blob_line(kind: str, blob: dict) -> str:
    """Строка блоба (Ф5c2): present/available + mime + size + короткий sha.

    Sparse-безопасно: size/mime отсутствуют → соответствующие части пропускаются.
    """
    flags = "present" if blob.get("present") is True else "absent"
    flags += " · " + ("available" if blob.get("available") is True else "unavailable")
    parts: list[str] = [kind, flags]
    mime = blob.get("mime")
    if mime:
        parts.append(str(mime))
    size = human_size(blob.get("size"))
    if size != "—":
        parts.append(size)
    sha = short_sha(blob.get("sha256"))
    if sha:
        parts.append(sha)
    return " · ".join(parts)


def _canonical_error_line(canonical_error: dict | None) -> str:
    """Бейдж отсутствия canonical (Ф5c2): human-причина через canonical_reason_text."""
    reason = None
    if isinstance(canonical_error, dict):
        reason = canonical_error.get("reason")
    reason_text = canonical_reason_text(reason)
    line = "⚠ canonical недоступен"
    if reason_text:
        line += f": {reason_text}"
    return line


def render_source_card(source: dict | None, container: ui.element) -> None:
    """Ф5c2: карточка Source — метаданные + оба блоба + canonical_error.

    `source=None` (сетевой сбой) или `{"error": ...}` (гейт сервера) →
    нейтральный «нет данных» БЕЗ раскрытия существования/зоны/лицензии/причины
    (никакого «недоступно из-за license=unknown» — утечки нет).

    Sparse-поля не фабрикуются: нет ключа → нет строки (без «нет данных»-шума
    для отсутствующих метаданных/блоба).
    """
    container.clear()
    with container:
        if not isinstance(source, dict) or "error" in source:
            ui.label("нет данных").classes("text-caption text-grey")
            return

        title = source.get("title") or source.get("source_id") or "—"
        ui.label(f"📄 {title}").classes("text-subtitle2")

        meta: list[str] = []
        if source.get("format"):
            meta.append(f"format: {source['format']}")
        if source.get("license"):
            meta.append(f"license: {source['license']}")
        zone = source.get("zone")
        if zone:
            meta.append("🌍 public" if zone == "public" else "🔒 private")
        if source.get("status"):
            meta.append(f"status: {source['status']}")
        if meta:
            ui.label(" · ".join(meta)).classes("text-caption text-grey")

        blobs = source.get("blobs")
        if isinstance(blobs, dict):
            for kind in ("original", "canonical"):
                blob = blobs.get(kind)
                if not isinstance(blob, dict):
                    continue  # sparse: блоба нет — не фабрикуем
                ui.label(_blob_line(kind, blob)).classes("text-caption font-mono")

        canonical_error = source.get("canonical_error")
        if canonical_error:
            ui.label(_canonical_error_line(canonical_error)).classes(
                "text-caption text-orange"
            )
            ui.label(
                "Цитаты и постраничный просмотр могут быть недоступны — "
                "исходник сохранён, но канонический PDF отсутствует (pdf_only-фоллбэк)."
            ).classes("text-caption text-grey")


def _render_provenance(snapshot: dict) -> None:
    """Ф5a2: рендер провенанса canonical (sparse — только при наличии ключей).

    Ключа нет → статус неизвестен/неприменим — ничего не добавляем
    (без «нет данных»-шума).
    """
    source_id = snapshot.get("source_id")
    canonical_present = snapshot.get("canonical_present")
    canonical_sha256 = snapshot.get("canonical_sha256")
    canonical_error = snapshot.get("canonical_error")

    if source_id:
        ui.label(f"источник: {source_id}").classes("text-caption text-grey")

    if canonical_present is True:
        text = "✅ canonical"
        if canonical_sha256:
            text += f" ({canonical_sha256[:12]}…)"
        ui.label(text).classes("text-caption")
    elif canonical_present is False or canonical_error:
        reason = None
        if isinstance(canonical_error, dict):
            reason = canonical_error.get("reason")
        reason_text = canonical_reason_text(reason)
        label = "⚠ canonical недоступен"
        if reason_text:
            label += f": {reason_text}"
        ui.label(label).classes("text-caption text-orange")
        ui.label(
            "Цитаты и постраничный просмотр могут быть недоступны — "
            "исходник сохранён, но канонический PDF отсутствует (pdf_only-фоллбэк)."
        ).classes("text-caption text-grey")


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
    done = status in ("done", "error", "cancelled")
    with container:
        ui.label(
            f"📊 Секция {imported}/{total} ({percent:.0f}%)"
            + (f"  ·  ошибок: {failed}" if failed else "")
            + (f"  ·  {status}" if done else "")
        ).classes("text-body2")
        ui.linear_progress(
            value=(imported / total) if total else 0,
        ).props("rounded").classes("w-full")
        _render_provenance(snapshot)
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
    """Прочитать загруженный файл в текст (или PDF-маркер для .pdf).

    Returns:
        (content, error): success → (text, None); PDF → (PDF_BINARY_MARKER, None); failure → (None, error_msg).
    """
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None, f"Неподдерживаемый тип файла «{ext or 'без расширения'}». Ожидаются: .md, .markdown, .txt, .pdf"
    if ext == ".pdf":
        return PDF_BINARY_MARKER, None
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
