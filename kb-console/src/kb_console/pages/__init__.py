"""Реестр страниц kb-console.

Масштабируемость: новая функция = новый модуль + строка в ROUTES.
ROUTES используется для навигации (header + @ui.page регистрация).
PAGES оставлен для обратной совместимости (builders без путей).

kb-console-roles Ф3.2: 4-й элемент — min_role (admin|editor|contributor).
Пункт скрыт из навигации, если роль текущего пользователя ниже. Роли:
legacy (пустой users-стор) = admin → видно всё (режим 002 бит-ин-бит).
"""

from __future__ import annotations

from collections.abc import Callable

from . import books, import_page, quality, search, status, tokens, users_page

# ROUTES: (путь, метка, функция-построитель, min_role)
# Используется header.py и app.py для регистрации @ui.page.
ROUTES: list[tuple[str, str, Callable[[], None], str]] = [
    ("/status", "Статус", status.build_status, "contributor"),
    ("/books", "Книги", books.build_books, "contributor"),
    ("/import", "Импорт", import_page.build_import, "contributor"),
    ("/search", "Поиск", search.build_search, "contributor"),
    ("/quality", "Качество", quality.build_quality, "contributor"),  # Фаза 13.14
    ("/tokens", "Токены", tokens.build_tokens, "admin"),  # W5; admin-only — У-3/Ф3.2
    ("/users", "Пользователи", users_page.build_users, "admin"),  # Ф3.2
]

# PAGES оставлен для обратной совместимости (если где-то ещё используется).
PAGES: list[tuple[str, Callable[[], None]]] = [
    (label, builder) for _, label, builder, _mr in ROUTES
]
