"""Страница «Документы» — администрирование хранилища документов (bibliography Ф5c1).

Admin-only (min_role="admin" в ROUTES + runtime-гейт is_admin(), паттерн
tokens.py/users_page.py). Инструменты — серверные admin-only (mcp_server):

- `documents_stats()`   — квота (used/max/used_pct), blobs (total/orphans),
                          jobs, grace_days.
- `documents_check(create_issues=False)` — проверка целостности;
                          read-only дефолт (create_issues=False).
- `documents_rebuild()`  — пересборка реестра (идемпотентна, confirm).
- `documents_gc(dry_run=True)` — GC; dry-run дефолт (безопасно);
                          фактическое удаление — только после confirm.
- `documents_retry(source_id)` — ретрай канонизации.

Ключ — `mcp_api_key()` (роль запроса), НЕ base (MCP_API_KEY): паттерн
search.py:55 / identity.py:136 (Ф4-fix1 P1-1). Confirm-диалоги — bare await,
non-persistent (quality.py).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ..config import MCP_SERVER_URL
from ..core.identity import is_admin, mcp_api_key
from ..core.mcp_client import MCPClient
from ..core.utils import canonical_reason_text, human_size


def _client() -> MCPClient:
    """Клиент с роль-ключом сессии (mcp_api_key), НЕ base-ключ."""
    return MCPClient(base_url=MCP_SERVER_URL, api_key=mcp_api_key())


def _human_bytes(value: Any) -> str:
    """Байты → человекочитаемая строка (fail-soft на None/нечисло)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0 or unit == "ТБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024.0
    return "—"


# ── Действия (module-level — тестируемо, паттерн quality.py) ──


async def _run_stats() -> dict[str, Any]:
    client = _client()
    try:
        return await client.documents_stats()
    finally:
        await client.close()


async def _run_check(create_issues: bool = False) -> dict[str, Any]:
    """Проверка целостности: read-only дефолт (create_issues=False)."""
    client = _client()
    try:
        return await client.documents_check(create_issues=create_issues)
    finally:
        await client.close()


async def _run_rebuild() -> dict[str, Any]:
    client = _client()
    try:
        return await client.documents_rebuild()
    finally:
        await client.close()


async def _run_gc(dry_run: bool = True) -> dict[str, Any]:
    """GC: dry_run=True — безопасный дефолт (превью кандидатов)."""
    client = _client()
    try:
        return await client.documents_gc(dry_run=dry_run)
    finally:
        await client.close()


async def _run_retry(source_id: str) -> dict[str, Any]:
    client = _client()
    try:
        return await client.documents_retry(source_id=source_id)
    finally:
        await client.close()


# ── Рендер-хелперы (чистые, тестируемы на спарс-данных) ──────


def _render_storage_stats(stats: dict[str, Any] | None) -> None:
    """Блок «Хранилище»: квота + прогресс-бар + blobs/jobs/grace_days.

    Спарс-данные (нет quota/blobs/jobs/grace_days) — fail-soft: строки
    с отсутствующими ключами не рендерятся, ничего не падает.
    """
    stats = stats if isinstance(stats, dict) else {}
    quota = stats.get("quota") if isinstance(stats.get("quota"), dict) else {}
    blobs = stats.get("blobs") if isinstance(stats.get("blobs"), dict) else {}
    jobs = stats.get("jobs") if isinstance(stats.get("jobs"), dict) else {}

    used_bytes = quota.get("used_bytes")
    max_bytes = quota.get("max_bytes")
    used_pct = quota.get("used_pct")

    with ui.card().classes("w-full q-mb-md"):
        ui.label("Хранилище документов").classes("text-h6 q-mb-sm")
        if used_bytes is not None or max_bytes is not None:
            ui.label(
                f"Использовано: {_human_bytes(used_bytes)}"
                f" из {_human_bytes(max_bytes)}"
            ).classes("text-body1")
        if isinstance(used_pct, (int, float)):
            ui.linear_progress(min(max(used_pct, 0.0), 1.0)).props("rounded").classes("w-full")
            ui.label(f"{used_pct * 100:.1f}%").classes("text-caption text-grey")

        with ui.row().classes("gap-4 q-mt-sm"):
            ui.label(f"Blobs всего: {blobs.get('total', 0)}").classes("text-body2")
            if "orphans" in blobs:
                ui.label(f"Orphans: {blobs['orphans']}").classes(
                    "text-body2 text-orange" if blobs["orphans"] else "text-body2"
                )
            if jobs:
                job_text = ", ".join(f"{k}: {v}" for k, v in jobs.items())
                ui.label(f"Jobs: {job_text}").classes("text-body2")
        if "grace_days" in stats:
            ui.label(f"Grace days: {stats['grace_days']}").classes("text-body2")


def _render_check_result(result: dict[str, Any] | None) -> None:
    """Блок результата проверки целостности (РЕАЛЬНАЯ форма сервера Ф5b2).

    Сервер `documents_check` отдаёт (НЕ list — dict по категориям):
      {"ok": bool, "counts": {...},
       "issues": {category: count}, "samples": {category: [items ≤20]}}.
    Показать: сумму проблем (или «проблем нет»), счётчики по категориям,
    краткие samples (уже лимитированы сервером). Sparse-безопасно.
    """
    result = result if isinstance(result, dict) else {}
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    issues = result.get("issues") if isinstance(result.get("issues"), dict) else {}
    samples = result.get("samples") if isinstance(result.get("samples"), dict) else {}

    total_problems = sum(
        int(v) for v in issues.values() if isinstance(v, (int, float))
    )

    if total_problems == 0:
        ui.label("Проблем не обнаружено").classes("text-body1 text-positive")
    else:
        ui.label(f"Найдено проблем: {total_problems}").classes("text-body1 text-orange")
        for category, n in issues.items():
            if n:
                ui.label(f"• {category}: {n}").classes("text-caption text-grey")

    sample_count = sum(len(v) for v in samples.values() if isinstance(v, list))
    if sample_count:
        ui.label(f"Сэмплы: {sample_count}").classes("text-caption text-grey")

    if counts:
        ui.label(
            "Счётчики: " + ", ".join(f"{k}={v}" for k, v in counts.items())
        ).classes("text-body1")


def _render_gc_result(result: dict[str, Any] | None, dry_run: bool) -> None:
    """Блок результата GC (dry-run: счётчик/reclaimable; real: deleted/freed)."""
    result = result if isinstance(result, dict) else {}
    if dry_run:
        total = result.get("candidates_total")
        if total is None:
            cands = result.get("candidates") or []
            total = len(cands) if isinstance(cands, list) else 0
        ui.label(
            f"Кандидатов на удаление: {total}, "
            f"освободится: {human_size(result.get('reclaimable_bytes'))}"
        ).classes("text-body1")
        ui.label(
            f"Сохранено (свежие): {result.get('kept_fresh', 0)}, "
            f"сохранено (ссылочные): {result.get('kept_referenced', 0)}"
        ).classes("text-caption text-grey")
    else:
        ui.label(
            f"Удалено: {result.get('deleted', 0)}, "
            f"освобождено: {_human_bytes(result.get('freed_bytes'))}"
        ).classes("text-body1")
        errors = result.get("errors") or []
        if errors:
            ui.label(f"Ошибок: {len(errors)}").classes("text-body1 text-negative")


def _render_retry_result(result: dict[str, Any] | None) -> None:
    """Блок результата retry (status/reason через canonical_reason_text)."""
    result = result if isinstance(result, dict) else {}
    status = result.get("status", "?")
    reason = canonical_reason_text(result.get("reason"))
    ui.label(f"Статус: {status}").classes("text-body1")
    if reason:
        ui.label(f"Причина: {reason}").classes("text-caption text-grey")
    if result.get("canonical_sha256"):
        ui.label(
            f"Canonical: {result['canonical_sha256'][:12]}…"
        ).classes("text-caption text-grey")


# ── Сборка страницы ──────────────────────────────────────────


def build_documents() -> None:
    """Построить admin-страницу «Документы» (Ф5c1).

    Admin-only: runtime-гейт is_admin() (навигация уже скрыта min_role="admin").
    """
    if not is_admin():
        ui.label("⛔ 403: управление документами доступно только администраторам.").classes(
            "text-h6 text-negative"
        )
        ui.label("Обратитесь к администратору консоли.").classes("text-body1 text-grey")
        return

    ui.label("Документы (хранилище)").classes("text-h4 q-mb-md")

    stats: dict[str, Any] = {}

    @ui.refreshable
    def render_stats() -> None:
        _render_storage_stats(stats)

    async def load_stats() -> None:
        nonlocal stats
        try:
            stats = await _run_stats()
        except Exception as exc:  # Transport/Auth — не роняем страницу
            ui.notify(f"Ошибка загрузки статистики: {exc}", type="negative")
            stats = {}
        render_stats.refresh()

    # ── Хранилище ──
    with ui.row().classes("items-center gap-2 q-mb-md"):
        ui.button("🔄 Обновить статистику", on_click=load_stats).props("flat")
    render_stats()

    # ── Проверка целостности ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Проверка целостности").classes("text-h6 q-mb-sm")
        check_out = ui.column().classes("w-full")
        with ui.row().classes("gap-2"):
            ui.button(
                "🔍 Проверить (read-only)",
                on_click=lambda: _do_check(create_issues=False, out=check_out),
            ).props("flat")
            ui.button(
                "📌 Зафиксировать проблемы",
                on_click=lambda: _confirm_check(check_out),
            ).props("flat color=warning")

    # ── Пересобрать реестр ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Реестр документов").classes("text-h6 q-mb-sm")
        ui.button("🔧 Пересобрать реестр", on_click=_confirm_rebuild).props("flat")

    # ── GC ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Сборка мусора (GC)").classes("text-h6 q-mb-sm")
        gc_out = ui.column().classes("w-full")
        with ui.row().classes("gap-2"):
            ui.button(
                "🧹 Проверить мусор (dry-run)",
                on_click=lambda: _do_gc(dry_run=True, out=gc_out),
            ).props("flat")
            ui.button(
                "🗑 Удалить мусор",
                on_click=lambda: _confirm_gc_delete(gc_out),
            ).props("flat color=negative")

    # ── Retry ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Ретрай канонизации").classes("text-h6 q-mb-sm")
        retry_out = ui.column().classes("w-full")
        source_input = ui.input("source_id", placeholder="идентификатор источника").props(
            "dense"
        ).classes("w-96")
        ui.button(
            "↻ Retry",
            on_click=lambda: _do_retry(source_input.value, retry_out),
        ).props("flat")

    ui.timer(0.1, load_stats, once=True)


# ── UI-обработчики кнопок (под капотом — module-level действия) ──


async def _do_check(create_issues: bool, out: ui.element) -> None:
    out.clear()
    with out:
        try:
            result = await _run_check(create_issues=create_issues)
            _render_check_result(result)
        except Exception as exc:
            ui.label(f"Ошибка проверки: {exc}").classes("text-negative")


async def _confirm_check(out: ui.element) -> None:
    """Зафиксировать проблемы (create_issues=True) — с подтверждением."""
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("📌 Зафиксировать проблемы").classes("text-h6")
        ui.label(
            "Найденные проблемы целостности будут записаны как issues "
            "(мутирующая операция)."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm(dlg=dialog):
                dlg.close()
                await _do_check(create_issues=True, out=out)

            ui.button("📌 Зафиксировать", on_click=_confirm).props("flat color=warning")
    await dialog


async def _confirm_rebuild() -> None:
    """Пересборка реестра — мутирующая, confirm-диалог."""
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("🔧 Пересобрать реестр").classes("text-h6")
        ui.label(
            "Реестр документов будет пересобран (идемпотентная операция)."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm(dlg=dialog):
                dlg.close()
                try:
                    result = await _run_rebuild()
                    ui.notify(
                        f"added={result.get('added', 0)} updated={result.get('updated', 0)} "
                        f"removed={result.get('removed', 0)}",
                        type="positive",
                    )
                except Exception as exc:
                    ui.notify(f"Ошибка пересборки: {exc}", type="negative")

            ui.button("🔧 Пересобрать", on_click=_confirm).props("flat color=warning")
    await dialog


async def _do_gc(dry_run: bool, out: ui.element) -> None:
    out.clear()
    with out:
        try:
            result = await _run_gc(dry_run=dry_run)
            _render_gc_result(result, dry_run=dry_run)
        except Exception as exc:
            ui.label(f"Ошибка GC: {exc}").classes("text-negative")


async def _confirm_gc_delete(out: ui.element) -> None:
    """Удаление мусора (dry_run=False) — ТОЛЬКО после confirm (HITL).

    Сначала dry-run-превью (сколько будет удалено), затем confirm-диалог,
    и только после подтверждения — фактическое удаление dry_run=False.
    """
    preview: dict[str, Any] = {}
    try:
        preview = await _run_gc(dry_run=True)
    except Exception as exc:
        ui.notify(f"Ошибка предпросмотра GC: {exc}", type="negative")
        return

    total = preview.get("candidates_total")
    if total is None:
        cands = preview.get("candidates") or []
        total = len(cands) if isinstance(cands, list) else 0
    reclaimable = human_size(preview.get("reclaimable_bytes"))

    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("🗑 Удалить мусор").classes("text-h6")
        ui.label(
            f"Будет удалено кандидатов: {total}, освободится: {reclaimable}. "
            f"Операция необратима."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm(dlg=dialog):
                dlg.close()
                await _do_gc(dry_run=False, out=out)

            ui.button("🗑 Удалить", on_click=_confirm).props("flat color=negative")
    await dialog


async def _do_retry(source_id: str, out: ui.element) -> None:
    source_id = (source_id or "").strip()
    out.clear()
    with out:
        if not source_id:
            ui.label("Введите source_id").classes("text-orange")
            return
        try:
            result = await _run_retry(source_id)
            _render_retry_result(result)
        except Exception as exc:
            ui.label(f"Ошибка retry: {exc}").classes("text-negative")
