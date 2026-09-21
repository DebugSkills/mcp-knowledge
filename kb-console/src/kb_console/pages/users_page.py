"""Страница «Пользователи» — admin-only (Ф3.2, kb-console-roles B2).

Список / создание (одноразовый показ пароля) / деактивация / reset
пароля / смена роли — через UserStore API с actor=<текущий пользователь>
(users_audit.jsonl — Ф3.3).

Матрица «роль × страница/действие» (Ф3.4, докстринг-SSOT):
| Действие                          | admin | editor | contributor | legacy |
|-----------------------------------|-------|--------|-------------|--------|
| /status /books /search /quality   |  ✅   |   ✅   |     ✅      |   ✅   |
| /import (без replace-селектора)   |  ✅   |   ✅   |     ✅      |   ✅   |
| /import replace                   |  ✅   |   ✅   |     ❌      |   ✅   |
| bulk-кнопки quality (🟢/⊘/📦)     |  ✅   |   ❌   |     ❌      |   ✅   |
| /tokens                           |  ✅   |   ❌   |     ❌      |   ✅   |
| /users (эта страница)             |  ✅   |   ❌   |     ❌      |   ✅   |
legacy = пустой users-стор (режим 002, все гейты открыты, бит-ин-бит).
Серверная защита: EDITOR_TOOLS/IMPORT_TOOLS + replace-гейт (Ф1).
"""

from __future__ import annotations

import secrets

from nicegui import ui

from ..core.identity import current_actor, current_identity, is_admin
from ..core.users import ROLES, UserStore


def _users_store() -> UserStore:
    from ..app import USERS_STORE

    return USERS_STORE


def _gen_password() -> str:
    """Одноразовый пароль: 16 hex-символов (показ один раз, потом только reset)."""
    return secrets.token_hex(8)


def is_last_active_admin(
    store: UserStore, username: str, *, new_active: bool | None = None,
    new_role: str | None = None,
) -> bool:
    """True, если операция над username оставит консоль БЕЗ активного админа.

    Guard от self-lockout: деактивация/понижение последнего активного
    админа запрещена (пустой стор ≠ выход: has_users остаётся true,
    но войти некому — konsole становится неуправляемой).
    """
    rec = store.get(username)
    if rec is None or not rec.active or rec.role != "admin":
        return False  # цель и так не активный админ
    others = [
        r for r in store.list_users()
        if r.username != username and r.active and r.role == "admin"
    ]
    if others:
        return False
    if new_active is False:
        return True
    return bool(new_role is not None and new_role != "admin")


def _show_one_time_password(username: str, password: str, what: str) -> None:
    with ui.dialog() as dlg, ui.card():
        ui.label(f"🔑 {what}: {username}").classes("text-h6")
        ui.label("Пароль показывается ОДИН раз — скопируйте и передайте пользователю:").classes(
            "text-body2"
        )
        ui.label(password).classes("text-bold text-primary").tooltip("Скопируйте и сохраните")
        with ui.row().classes("items-center gap-2"):
            ui.button("📋 Копировать", on_click=lambda: ui.clipboard.write(password))
            ui.button("Закрыть", on_click=dlg.close)
    dlg.open()


def build_users() -> None:
    """Построить страницу «Пользователи» (admin-only)."""
    if not is_admin():
        ui.label("⛔ 403: управление пользователями доступно только администраторам.").classes(
            "text-h6 text-negative"
        )
        ui.label("Обратитесь к администратору консоли.").classes("text-body1 text-grey")
        return

    store = _users_store()
    actor = current_actor()

    ui.label("Учётные записи консоли").classes("text-h5 q-mb-sm")
    identity = current_identity()
    if identity:
        ui.label(f"Вы: {identity['username']} ({identity['role']})").classes(
            "text-caption text-grey"
        )

    def _refresh() -> None:
        # Простой full-reload страницы: мутации редки, SPA-state не критичен.
        ui.navigate.to("/users")

    # ── Создание ─────────────────────────────────────────────
    with ui.row().classes("q-mb-md"):
        def _open_create() -> None:
            with ui.dialog() as dlg, ui.card().classes("q-pa-md"):
                ui.label("Создать пользователя").classes("text-h6")
                username_in = ui.input("Username").classes("w-full")
                role_sel = ui.select(
                    list(ROLES), value="contributor", label="Роль",
                ).classes("w-full")
                note_in = ui.input("Заметка (опционально)").classes("w-full")

                def _do_create() -> None:
                    username = username_in.value.strip()
                    if not username:
                        ui.notify("Username обязателен", type="warning")
                        return
                    password = _gen_password()
                    try:
                        store.create_user(
                            username, password, role_sel.value,
                            note=note_in.value.strip(), actor=actor,
                        )
                    except ValueError as e:
                        ui.notify(f"Ошибка: {e}", type="negative")
                        return
                    dlg.close()
                    _show_one_time_password(username, password, "Пароль нового пользователя")
                    _refresh()

                with ui.row().classes("gap-2 q-mt-md"):
                    ui.button("Создать", on_click=_do_create).props("color=primary")
                    ui.button("Отмена", on_click=dlg.close).props("flat")
            dlg.open()

        ui.button("➕ Создать пользователя", on_click=_open_create).props("color=primary")

    # ── Таблица ──────────────────────────────────────────────
    users = store.list_users()
    if not users:
        ui.label("Пока нет пользователей (legacy-режим).").classes("text-grey")
        return

    columns = [
        {"name": "username", "label": "Username", "field": "username", "align": "left"},
        {"name": "role", "label": "Роль", "field": "role", "align": "left"},
        {"name": "active", "label": "Статус", "field": "active", "align": "left"},
        {"name": "created", "label": "Создан", "field": "created_at", "align": "left"},
        {"name": "last_login", "label": "Последний вход", "field": "last_login_at", "align": "left"},
        {"name": "actions", "label": "Действия", "field": "actions", "align": "right"},
    ]
    rows = [
        {
            "username": r.username,
            "role": r.role,
            "active": r.active,
            "created_at": (r.created_at or "")[:19].replace("T", " "),
            "last_login_at": (r.last_login_at or "—")[:19].replace("T", " "),
        }
        for r in users
    ]

    table = ui.table(columns=columns, rows=rows, row_key="username").classes("w-full")
    table.props("dense flat")

    table.add_slot(
        "body-cell-role",
        """
        <q-td :props="props">
            <q-badge v-if="props.row.role === 'admin'" color="red">🔴 admin</q-badge>
            <q-badge v-else-if="props.row.role === 'editor'"
              color="purple">🟣 editor</q-badge>
            <q-badge v-else color="orange">🟠 contributor</q-badge>
        </q-td>
        """,
    )
    table.add_slot(
        "body-cell-active",
        """
        <q-td :props="props">
            <q-badge :color="props.row.active ? 'green' : 'grey'">
                {{ props.row.active ? 'активен' : 'деактивирован' }}
            </q-badge>
        </q-td>
        """,
    )

    # ── Действия по строке ───────────────────────────────────
    table.add_slot(
        "body-cell-actions",
        """
        <q-td :props="props">
            <q-btn flat dense icon="vpn_key" color="primary"
              @click="$parent.$emit('reset', props.row)" title="Reset пароля"/>
            <q-btn flat dense icon="swap_horiz" color="teal"
              @click="$parent.$emit('role', props.row)" title="Смена роли"/>
            <q-btn flat dense :icon="props.row.active ? 'block' : 'check'"
              :color="props.row.active ? 'warning' : 'positive'"
              @click="$parent.$emit('toggle', props.row)"
              :title="props.row.active ? 'Деактивировать' : 'Активировать'"/>
        </q-td>
        """,
    )

    def _reset_pw(row: dict) -> None:
        username = row["username"]
        password = _gen_password()
        store.set_password(username, password, actor=actor)
        _show_one_time_password(username, password, "Новый пароль")

    def _change_role(row: dict) -> None:
        username = row["username"]
        with ui.dialog() as dlg, ui.card().classes("q-pa-md"):
            ui.label(f"Роль пользователя {username}").classes("text-h6")
            sel = ui.select(list(ROLES), value=row["role"]).classes("w-full")

            def _do() -> None:
                if is_last_active_admin(store, username, new_role=sel.value):
                    ui.notify(
                        "Нельзя понизить последнего активного админа — консоль "
                        "останется без управления", type="negative",
                    )
                    return
                store.set_role(username, sel.value, actor=actor)
                dlg.close()
                _refresh()

            with ui.row().classes("gap-2 q-mt-md"):
                ui.button("Применить", on_click=_do).props("color=primary")
                ui.button("Отмена", on_click=dlg.close).props("flat")
        dlg.open()

    def _toggle(row: dict) -> None:
        username = row["username"]
        new_active = not row["active"]
        if not new_active and is_last_active_admin(store, username, new_active=False):
            ui.notify(
                "Нельзя деактивировать последнего активного админа — консоль "
                "останется без управления", type="negative",
            )
            return
        store.set_active(username, new_active, actor=actor)
        _refresh()

    table.on("reset", lambda e: _reset_pw(e.args))
    table.on("role", lambda e: _change_role(e.args))
    table.on("toggle", lambda e: _toggle(e.args))
