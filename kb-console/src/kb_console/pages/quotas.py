"""Страница «Квоты» — генератор host-команды/патча (Ф4.5c-2, admin-only).

Консоль НЕ читает и НЕ пишет ``ai_workspace/registry/quotas.yaml`` (в образ
консоли реестр не попадает — осознанное решение Ф4.5c-1): страница только
ГЕНЕРИРУЕТ (а) команду ``make quotas-set … --apply``, (б) dry-run команду,
(в) минимальный YAML-патч. Применяет ОПЕРАТОР на хосте; рантайм подхватит
правку по mtime (рестарт не нужен); git-коммит — за оператором.

Две «роли» — не путать: UI-роль доступа консоли (admin/editor/contributor,
гейт страницы через ``is_admin`` — прецедент tokens.py) ≠ participant-роль
верстака из quotas.yaml (admin/member/guest — выбирается формой).

Fail-soft: страница полностью статична — нет redis, нет MCP-сервера;
невалидное состояние формы → ``ui.notify`` negative, страница жива.

Clipboard: кнопка «Скопировать» → ``ui.clipboard.write`` (NiceGUI ≥ 2.10;
в образе 3.15). Clipboard API браузера работает только в secure context
(HTTPS/localhost); на plain-HTTP консоли кнопка может не сработать — поэтому
каждый результат показан и в read-only textarea (текст выделяем и копируем
вручную): фолбэк всегда перед глазами, а не вместо кнопки.
"""

from __future__ import annotations

from nicegui import ui

from ..core.identity import is_admin
from ..core.quotas_patch import (
    MODEL_CLASSES,
    PARTICIPANT_ROLES,
    QUOTA_PRIORITIES,
    UNSET,
    build_quotas_patch,
)

# ── Чистые хелперы формы (тестируются без UI) ───────────────────────────


def tri_state_number(
    enabled: bool,
    value: float | None,
    none_marked: bool,
    *,
    field: str,
    minimum: int,
) -> int | None | object:
    """Tri-state числового поля (tokens/conc) → вход билдера.

    - не включено (и без none) → ``UNSET`` — флаг опускается («не менять»);
    - none-чекбокс → ``None`` — CLI ``none`` → YAML null («только общий K»);
    - включено + число → значение (валидацию диапазона делает билдер);
    - включено, но ни числа, ни none → ``ValueError`` с именем поля.
    """
    if none_marked:
        return None
    if not enabled:
        return UNSET
    if value is None:
        raise ValueError(
            f"{field}: отмечено «изменить», но не задано число (или отметьте none)"
        )
    return value


def optional_number(
    enabled: bool, value: float | None, *, field: str
) -> int | None | object:
    """Числовое поле БЕЗ none-варианта (budget-ext: CLI принимает только
    число > 0) → ``UNSET`` | число. Включено без числа → ``ValueError``."""
    if not enabled:
        return UNSET
    if value is None:
        raise ValueError(f"{field}: отмечено «изменить», но не задано число")
    return value


def copy_to_clipboard(text: str) -> None:
    """«Скопировать»: ``ui.clipboard.write`` + positive-notify (см. докстринг
    модуля про secure-context ограничение и textarea-фолбэк)."""
    ui.clipboard.write(text)
    ui.notify("Скопировано в буфер", type="positive")


def _result_block(title: str, text: str, *, caption: str = "") -> None:
    """Блок результата: подпись (+caption), read-only textarea, «Скопировать»."""
    ui.label(title).classes("text-subtitle2 q-mt-md")
    if caption:
        ui.label(caption).classes("text-caption text-grey")
    ui.textarea(value=text).props("readonly autogrow").classes("w-full font-mono")
    ui.button("Скопировать", on_click=lambda t=text: copy_to_clipboard(t)).props(
        "flat"
    )


def render_result(result: dict[str, str]) -> None:
    """Показать (а) команду с --apply, (б) dry-run, (в) YAML-патч.

    Отдельная функция (не замыкание) — тестируется на mock-UI; ``result`` —
    словарь билдера ``build_quotas_patch``.
    """
    _result_block(
        "Команда с --apply (бэкап .trash + атомарная запись + пост-валидация)",
        result["command"],
        caption="Запустить на ХОСТЕ репозитория. Сначала прогоните dry-run!",
    )
    _result_block(
        "Команда dry-run (файл НЕ изменяется — прогоните её первой)",
        result["dry_run_command"],
    )
    _result_block(
        "Минимальный YAML-патч (что изменится в quotas.yaml)",
        result["patch"],
        caption="CLI правит значения точечно: комментарии и порядок ключей "
        "сохраняются байт-в-байт.",
    )


# ── Страница ────────────────────────────────────────────────────────────


def build_quotas() -> None:
    """Построить страницу «Квоты» (admin-only; генерация — без записи SSOT)."""
    if not is_admin():
        ui.label("⛔ 403: генерация правок квот доступна только администраторам.").classes(
            "text-h6 text-negative"
        )
        ui.label("Обратитесь к администратору консоли.").classes("text-body1 text-grey")
        return

    ui.label("Квоты верстака (генератор правки)").classes("text-h4 q-mb-xs")
    ui.label(
        "Консоль не читает и не пишет quotas.yaml: она генерирует команду, а "
        "применяет её ОПЕРАТОР на хосте. Рантайм подхватит правку по mtime "
        "(Registry.reload_if_changed) — рестарт не нужен; git-коммит — за "
        "оператором."
    ).classes("text-caption text-grey q-mb-md")
    with ui.card().classes("w-full q-mb-md bg-orange-1"):
        ui.label(
            "⚠ Порядок: сначала dry-run (без --apply) и проверка diff, затем "
            "команда с --apply. Exit-коды CLI: 0 — ок; 2 — usage/IO; "
            "3 — валидация отказала (файл не тронут/откат из бэкапа); "
            "4 — изменений нет (идемпотентно)."
        ).classes("text-body2 text-orange-9")

    with ui.card().classes("w-full"):
        ui.label("Что меняем").classes("text-h6")
        with ui.row().classes("w-full items-center"):
            role_sel = ui.select(
                list(PARTICIPANT_ROLES), value="member", label="participant-роль"
            )
            prio_sel = ui.select(
                list(QUOTA_PRIORITIES), value="med", label="приоритет (MULT-очередь)"
            )
        ui.label(
            "participant-роль верстака (quotas.yaml) — не UI-роль консоли."
        ).classes("text-caption text-grey")

        ui.separator()
        ui.label("Лимиты (неотмеченное — не трогаем)").classes("text-subtitle2")
        with ui.row().classes("w-full items-center"):
            tokens_enable = ui.checkbox("изменить tokens/day")
            tokens_input = ui.number("tokens_per_day", min=0, step=1)
            tokens_none = ui.checkbox("none (null: только общий бюджет K)")
        with ui.row().classes("w-full items-center"):
            conc_enable = ui.checkbox("изменить conc")
            conc_input = ui.number("conc", min=1, step=1)
            conc_none = ui.checkbox("none (null: только общий бюджет K)")
        ui.separator()
        with ui.row().classes("w-full items-center"):
            budget_enable = ui.checkbox("изменить budget-ext (₽, budgets.ext.limit)")
            budget_input = ui.number("budget-ext", min=1, step=1)
        ui.label(
            "У бюджета нет none: CLI принимает только число > 0 "
            "(глобальный hard-limit, D4)."
        ).classes("text-caption text-grey")

        ui.separator()
        grants_sel = ui.select(
            list(MODEL_CLASSES),
            multiple=True,
            label="классы моделей (grants; пусто = не менять)",
        )
        ui.label(
            "SSOT классов — ai_workspace/registry/model_classes.yaml "
            "(heavy/fast/local-only); менять синхронно."
        ).classes("text-caption text-grey")

        ui.button("Сгенерировать", on_click=lambda: _generate()).classes("q-mt-md")

    result_container = ui.column().classes("w-full")

    def _generate() -> None:
        """Собрать вход билдера → команда/патч; ошибки формы → notify."""
        try:
            result = build_quotas_patch(
                role=role_sel.value,
                priority=prio_sel.value,
                tokens=tri_state_number(
                    tokens_enable.value,
                    tokens_input.value,
                    tokens_none.value,
                    field="tokens",
                    minimum=0,
                ),
                conc=tri_state_number(
                    conc_enable.value,
                    conc_input.value,
                    conc_none.value,
                    field="conc",
                    minimum=1,
                ),
                grants=(grants_sel.value or []) if grants_sel.value is not None else [],
                budget_ext=optional_number(
                    budget_enable.value,
                    budget_input.value,
                    field="budget-ext",
                ),
                apply=True,
            )
        except ValueError as exc:
            ui.notify(f"Не сгенерировано: {exc}", type="negative")
            return
        result_container.clear()
        with result_container:
            render_result(result)
        ui.notify("Команда сгенерирована; применяет оператор на хосте", type="positive")
