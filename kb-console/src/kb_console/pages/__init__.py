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

from . import (
    books,
    chat,
    documents,
    import_page,
    quality,
    queue,
    quotas,
    requests_page,
    search,
    status,
    tokens,
    users_page,
)

# ROUTES: (путь, метка, функция-построитель, min_role)
# Используется header.py и app.py для регистрации @ui.page.
ROUTES: list[tuple[str, str, Callable[[], None], str]] = [
    ("/status", "Статус", status.build_status, "contributor"),
    ("/books", "Книги", books.build_books, "contributor"),
    ("/import", "Импорт", import_page.build_import, "contributor"),
    ("/search", "Поиск", search.build_search, "contributor"),
    ("/chat", "Чат", chat.build_chat, "contributor"),  # Ф2 ai-workspace #1
    ("/queue", "Очередь", queue.build_queue, "contributor"),  # Ф4.4b ai-workspace
    ("/quality", "Качество", quality.build_quality, "contributor"),  # Фаза 13.14
    ("/tokens", "Токены", tokens.build_tokens, "admin"),  # W5; admin-only — У-3/Ф3.2
    ("/users", "Пользователи", users_page.build_users, "admin"),  # Ф3.2
    ("/requests", "Заявки", requests_page.build_requests, "admin"),  # 036 Ф2
    ("/documents", "Документы", documents.build_documents, "admin"),  # bibliography Ф5c1
    ("/quotas", "Квоты", quotas.build_quotas, "admin"),  # Ф4.5c-2 ai-workspace
]

# PAGES оставлен для обратной совместимости (если где-то ещё используется).
PAGES: list[tuple[str, Callable[[], None]]] = [
    (label, builder) for _, label, builder, _mr in ROUTES
]
