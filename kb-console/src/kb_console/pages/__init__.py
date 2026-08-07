"""Реестр страниц kb-console.

Масштабируемость: новая функция = новый модуль + строка в PAGES.
"""

from __future__ import annotations

from collections.abc import Callable

from . import books, import_page, search, status

# Каждый элемент: (название_вкладки, функция_построения)
PAGES: list[tuple[str, Callable[[], None]]] = [
    ("Статус", status.build_status),
    ("Книги", books.build_books),
    ("Импорт", import_page.build_import),
    ("Поиск", search.build_search),
]
