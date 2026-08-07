"""Реестр страниц kb-console.

Масштабируемость: новая функция = новый модуль + строка в ROUTES.
ROUTES используется для навигации (header + @ui.page регистрация).
PAGES оставлен для обратной совместимости (builders без путей).
"""

from __future__ import annotations

from collections.abc import Callable

from . import books, import_page, search, status

# ROUTES: (путь, метка, функция-построитель)
# Используется header.py и app.py для регистрации @ui.page.
ROUTES: list[tuple[str, str, Callable[[], None]]] = [
    ("/status", "Статус", status.build_status),
    ("/books", "Книги", books.build_books),
    ("/import", "Импорт", import_page.build_import),
    ("/search", "Поиск", search.build_search),
]

# PAGES оставлен для обратной совместимости (если где-то ещё используется).
PAGES: list[tuple[str, Callable[[], None]]] = [
    (label, builder) for _, label, builder in ROUTES
]
