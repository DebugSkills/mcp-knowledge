"""MCPClient — JSON-RPC 2.0 клиент для MCP Knowledge Server.

Общается с сервером через POST /mcp (классический JSON-RPC, не SSE).
Поддерживает инжектируемый httpx.AsyncClient для E2E-тестов (ASGITransport).
"""

# ruff: noqa: ASYNC230
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Self

import httpx

logger = logging.getLogger("kb_console.mcp_client")


class MCPClient:
    """Асинхронный JSON-RPC 2.0 клиент для MCP Knowledge Server.

    Args:
        base_url: URL сервера (default: http://localhost:8000).
        api_key: Ключ для заголовка X-API-Key (если пустой — заголовок не слать).
        timeout: Таймаут HTTP-запросов в секундах.
        client: Инжектируемый httpx.AsyncClient (для тестов через ASGITransport).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str = "",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._own_client = False

        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
            self._own_client = True

        self._request_id = 0

    async def close(self) -> None:
        """Закрыть HTTP-клиент (только если создан внутри MCPClient)."""
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Low-level JSON-RPC call ─────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Выполнить JSON-RPC 2.0 вызов.

        Args:
            method: JSON-RPC метод.
            params: Параметры вызова.
            timeout: Per-call таймаут в секундах (переопределяет client-level).
                     None = использовать client-level таймаут.

        Returns:
            result из ответа.

        Raises:
            RuntimeError: при ошибках соединения, HTTP, или JSON-RPC error.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }

        url = f"{self.base_url}/mcp"

        try:
            kwargs: dict[str, Any] = {"json": payload, "headers": self._headers()}
            if timeout is not None:
                kwargs["timeout"] = httpx.Timeout(timeout)
            response = await self._client.post(url, **kwargs)
        except httpx.ConnectError as e:
            msg = f"Сервер недоступен: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except httpx.TimeoutException as e:
            msg = f"Таймаут запроса к серверу: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        # HTTP-level errors
        if response.status_code == 401:
            raise RuntimeError("Ошибка аутентификации: неверный API-ключ (401)")
        if response.status_code == 403:
            raise RuntimeError("Доступ запрещён: недостаточно прав (403)")
        if response.status_code == 429:
            raise RuntimeError("Слишком много запросов: превышен rate limit (429)")
        if response.status_code == 503:
            raise RuntimeError("Сервер временно недоступен: degraded (503)")

        if response.status_code != 200:
            raise RuntimeError(
                f"Ошибка HTTP {response.status_code}: {response.text[:200]}"
            )

        # JSON-RPC level
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON response: %s", response.text[:200])
            raise RuntimeError(f"Некорректный JSON-ответ от сервера: {e}") from e

        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            code = err.get("code", -1)
            logger.error("JSON-RPC error code=%s: %s", code, msg)
            raise RuntimeError(f"Ошибка JSON-RPC ({code}): {msg}")

        return data.get("result", {})

    def _unwrap_result(self, result: Any) -> Any:
        """Развернуть MCP content envelope если есть.

        mcp_handler.py:206 оборачивает результат tools/call в:
        {"content": [{"type": "text", "text": json.dumps(result)}]}

        Этот метод извлекает настоящий result, или возвращает как есть
        (backward-compat для initialize/tools_list, где конверта нет).
        """
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if not isinstance(content, list) or len(content) == 0:
            return result
        first = content[0]
        if not isinstance(first, dict) or first.get("type") != "text":
            return result
        text = first.get("text", "")
        if not isinstance(text, str):
            return result
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return result

    # ── High-level MCP methods ─────────────────────────────

    async def initialize(self) -> dict[str, Any]:
        """Выполнить initialize handshake.

        Returns:
            Результат инициализации (protocolVersion, serverInfo, ...).
        """
        return await self._call("initialize")

    async def tools_list(self) -> list[dict[str, Any]]:
        """Получить список всех MCP-инструментов.

        Returns:
            Список инструментов (name, description, inputSchema).
        """
        result = await self._call("tools/list")
        return result.get("tools", [])

    async def tools_call(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Вызвать MCP-инструмент.

        Args:
            name: Имя инструмента (search_knowledge, import_content, ...).
            params: Параметры вызова.
            timeout: Per-call таймаут в секундах (None = client-level).

        Returns:
            Результат выполнения инструмента (автоматически разворачивает
            MCP content envelope).
        """
        raw = await self._call(
            "tools/call",
            {"name": name, "arguments": params or {}},
            timeout=timeout,
        )
        return self._unwrap_result(raw)

    async def get_progress(self, import_id: str) -> dict[str, Any] | None:
        """GET /imports/{import_id}/progress — снапшот живого прогресса импорта.

        Args:
            import_id: Идентификатор импорта (UUID, сгенерированный клиентом).

        Returns:
            Словарь прогресса (imported, total, failed, status, messages[], ...)
            или None при любой ошибке (404 / endpoint отсутствует / сеть) —
            никогда не бросает.
        """
        url = f"{self.base_url}/imports/{import_id}/progress"
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None

    async def get_import_log(self, import_id: str) -> dict[str, Any] | None:
        """GET /imports/{import_id}/log — построчный лог импорта из ring-буфера.

        Args:
            import_id: Идентификатор импорта.

        Returns:
            {"import_id": "...", "log": [{"ts": "...", "level": "...", "text": "..."}]}
            или None при любой ошибке (404 / endpoint отсутствует / сеть) —
            никогда не бросает.
        """
        url = f"{self.base_url}/imports/{import_id}/log"
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None

    # ── Variant A (13.10): хелперы для «Книги» + информативный поиск ──

    async def get_entry(self, knowledge_id: str) -> dict[str, Any]:
        """Получить полную запись (frontmatter + content + TOC children)."""
        return await self.tools_call("get_entry", {"knowledge_id": knowledge_id})

    async def search_knowledge(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Семантический поиск; возвращает список results (обогащённый payload)."""
        params["query"] = query
        raw = await self.tools_call("search_knowledge", params)
        if isinstance(raw, dict):
            return raw.get("results", [])
        return raw if isinstance(raw, list) else []

    async def list_collections(self, **params: Any) -> list[dict[str, Any]]:
        """Список книг/коллекций; возвращает список results."""
        raw = await self.tools_call("list_collections", params)
        if isinstance(raw, dict):
            return raw.get("results", [])
        return raw if isinstance(raw, list) else []

    async def update_entry(self, knowledge_id: str, content: str) -> dict[str, Any]:
        """Обновить запись (контент body) через update_entry тул.

        Args:
            knowledge_id: ID записи для обновления.
            content: Новый markdown-контент (заменяет body, frontmatter сохраняется).

        Returns:
            Результат update_entry (словарь с knowledge_id, title и др.).

        Note:
            Использует per-call timeout 60s — серверный update_entry может
            занимать >10s на больших книгах (git commit YAML с 7032 children).
        """
        return await self.tools_call(
            "update_entry",
            {"knowledge_id": knowledge_id, "content": content},
            timeout=60.0,
        )

    # ── Fragment operations (Фаза 13.23) ────────────────────────

    async def add_fragment(
        self, collection_id: str, title: str, content: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Добавить раздел в книгу.

        Args:
            collection_id: ID книги-коллекции.
            title: Заголовок нового раздела.
            content: Содержание раздела (Markdown).
            tags: Дополнительные теги (опционально).

        Returns:
            {"fragment_id": ..., "collection_id": ..., "sequence_number": ..., "indexed": True}
        """
        params: dict[str, Any] = {
            "collection_id": collection_id,
            "title": title,
            "content": content,
        }
        if tags:
            params["tags"] = tags
        return await self.tools_call("add_fragment", params, timeout=60.0)

    async def update_fragment(
        self,
        fragment_id: str,
        content: str | None = None,
        title: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Обновить раздел книги с optimistic locking.

        Args:
            fragment_id: ID секции.
            content: Новое содержание (Markdown).
            title: Новый заголовок.
            version: Ожидаемая версия (optimistic locking).

        Returns:
            {"fragment_id": ..., "version": ..., "updated_at": ...}
            Или {"conflict": True, ...} при VersionConflict.

        Note:
            Per-call timeout 60s — как у update_entry (Фаза 13.16).
        """
        params: dict[str, Any] = {"fragment_id": fragment_id}
        if content is not None:
            params["content"] = content
        if title is not None:
            params["title"] = title
        if version is not None:
            params["version"] = version
        return await self.tools_call("update_fragment", params, timeout=60.0)

    async def delete_fragment(self, fragment_id: str) -> dict[str, Any]:
        """Удалить раздел книги (soft-delete → .trash/ + Qdrant).

        Args:
            fragment_id: ID секции.

        Returns:
            {"fragment_id": ..., "deleted": True}
        """
        return await self.tools_call(
            "delete_fragment",
            {"fragment_id": fragment_id},
            timeout=60.0,
        )

    async def find_fragment(
        self, collection_id: str, query: str, limit: int = 5,
    ) -> dict[str, Any]:
        """Найти разделы внутри книги по семантическому запросу.

        Args:
            collection_id: ID книги-коллекции.
            query: Поисковый запрос.
            limit: Максимальное число результатов (default 5, max 50).

        Returns:
            {"collection_id": ..., "query": ..., "fragments": [...], "total": N}
        """
        return await self.tools_call(
            "find_fragment",
            {"collection_id": collection_id, "query": query, "limit": min(limit, 50)},
        )

    async def resources_list(self) -> list[dict[str, Any]]:
        """Получить список MCP-ресурсов."""
        result = await self._call("resources/list")
        return result.get("resources", [])

    async def prompts_list(self) -> list[dict[str, Any]]:
        """Получить список MCP-промптов."""
        result = await self._call("prompts/list")
        return result.get("prompts", [])

    # ── Scan progress (Фаза 13.15) ───────────────────────────

    async def get_data_version(self) -> int:
        """GET /data-version — монотонный счётчик мутаций данных (Task 1).

        Используется DataCache для гибридной инвалидации (TTL + version check).

        Returns:
            Текущая версия данных (0 если ошибка).
        """
        url = f"{self.base_url}/data-version"
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return 0
        if response.status_code != 200:
            return 0
        try:
            return response.json().get("data_version", 0)
        except (json.JSONDecodeError, ValueError):
            return 0

    async def get_scan_progress(self) -> dict[str, Any] | None:
        """GET /quality/scan/progress — снапшот живого прогресса quality scan.

        Returns:
            Словарь прогресса (scan_id, status, phase, imported, total,
            messages[], started_at, updated_at, summary{metrics?}) или None
            при любой ошибке (404 / endpoint отсутствует / сеть) —
            никогда не бросает.
        """
        url = f"{self.base_url}/quality/scan/progress"
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None

    # ── Quality tools (Фаза 13.14) ────────────────────────────

    async def review_queue_books(
        self, domain: str | None = None, subject: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Топ устаревших КНИГ (агрегат по parent_knowledge_id).

        Returns:
            {"books": [...], "total_books": N, "total_stale_sections": M}
        """
        params: dict[str, Any] = {"limit": limit}
        if domain:
            params["domain"] = domain
        if subject:
            params["subject"] = subject
        return await self.tools_call("review_queue_books", params)

    async def list_quality_issues(
        self,
        types: list[str] | None = None,
        status: str = "open",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Список quality issues с фильтрацией (дубликаты, edit-wars, битые ссылки).

        Args:
            types: список типов (duplicate, missing_field, edit_war, broken_link,
                conflicting) — опционально.
            status: open | resolved | ignored (default "open").
            limit: макс. число (default 50, max 200 на сервере).

        Returns:
            {"issues": [...], "total": N}
        """
        params: dict[str, Any] = {"status": status, "limit": limit}
        if types:
            params["types"] = types
        return await self.tools_call("list_quality_issues", params)

    async def run_quality_scan(self, domain: str | None = None) -> dict[str, Any]:
        """Запустить quality scan (13.15: фоновая задача, мгновенный ответ).

        Новый контракт (13.15):
            {"scanned": true, "status": "started", "scan_id": "..."}
            {"scanned": false, "status": "already_running", "scan_id": "..."}
            {"scanned": false, "status": "error", "error": "..."}

        Прогресс: get_scan_progress() → GET /quality/scan/progress.
        """
        params: dict[str, Any] = {}
        if domain:
            params["domain"] = domain
        return await self.tools_call("run_quality_scan", params)

    async def cancel_quality_scan(self) -> dict[str, Any]:
        """Отменить активный quality scan (13.18).

        Returns:
            {"cancelled": True, "scan_id": "..."}  — отмена отправлена
            {"cancelled": False, "reason": "no active scan"}  — нечего отменять
        """
        return await self.tools_call("cancel_quality_scan", {})

    async def resolve_quality_issue(
        self,
        action: str,
        issue_id: str = "",
        knowledge_id: str | None = None,
        cascade: bool = False,
        reason: str = "",
        marks_fp: bool | None = None,
    ) -> dict[str, Any]:
        """Разрешить quality issue (resolve/deprecate/restore/merge/ignore).

        Args:
            action: merge | deprecate | restore | resolve | ignore
            issue_id: ID issue (опционально, если указан knowledge_id)
            knowledge_id: прямая операция на запись (Фаза 13.14)
            cascade: применить к дочерним секциям книги
            reason: причина решения
            marks_fp: явный FP-сигнал «не дубль» для action=resolve (Фаза 3)

        Returns:
            {"resolved": True/False, "cascade_affected": N, "side_effects": [...]}
        """
        params: dict[str, Any] = {"action": action, "reason": reason}
        if issue_id:
            params["issue_id"] = issue_id
        if knowledge_id:
            params["knowledge_id"] = knowledge_id
        if cascade:
            params["cascade"] = cascade
        if marks_fp is not None:
            params["marks_fp"] = marks_fp
        return await self.tools_call("resolve_quality_issue", params)

    async def bulk_resolve_issues(
        self,
        types: list[str] | None = None,
        knowledge_id: str | None = None,
        action: str = "ignore",
        reason: str = "",
        status: str = "open",
    ) -> dict[str, Any]:
        """Пакетно резолвить/игнорировать issues по фильтру (P0).

        Чистит накопленный шум (например ложные дубли) за один вызов.
        Меняет только issues.jsonl, не контент.

        Args:
            types: список типов (duplicate, missing_field, orphaned, ...)
            knowledge_id: фильтр по записи (опционально)
            action: ignore (обратимо) | resolve
            reason: причина
            status: исходный статус для выборки (default "open")

        Returns:
            {"resolved": True/False, "action": ..., "count": N, "total": N}
        """
        params: dict[str, Any] = {"action": action, "reason": reason, "status": status}
        if types:
            params["types"] = types
        if knowledge_id:
            params["knowledge_id"] = knowledge_id
        return await self.tools_call("bulk_resolve_issues", params)

    async def bulk_deprecate_duplicates(
        self,
        issue_ids: list[str] | None = None,
        knowledge_id: str | list[str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Пакетно deprecate записи-дубликаты (Фаза 1 dedup).

        Скрывает из поиска (обратимо через restore), закрывает все dup-issues,
        пишет в audit.jsonl. Контент .md не трогается.

        Args:
            issue_ids: список issue_id (deprecate их knowledge_id)
            knowledge_id: прямой ID записи (или список)
            reason: причина

        Returns:
            {"resolved": True, "deprecated_count": N, "issues_closed": M, "side_effects": [...]}
        """
        params: dict[str, Any] = {"reason": reason}
        if issue_ids:
            params["issue_ids"] = issue_ids
        if knowledge_id:
            params["knowledge_id"] = knowledge_id
        return await self.tools_call("bulk_deprecate_duplicates", params)

    async def review_duplicate_pairs(
        self, limit: int = 200, filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ревью-очередь dup-пар (Фаза 2 dedup): 🟢/🟡/🔴 ранжирование.

        Read-only: возвращает green_batch (пачка «Утвердить все») и
        yellow_pairs (сомнительные со сниппетами для diff-просмотра).

        Args:
            limit: макс. число open dup-issues.
            filter: {"hash_only": True} — только строгие R1 (exact hash), Фаза 3.

        Returns:
            {"green_batch": [...], "yellow_pairs": [...], "red_skipped": N, "total_open": M}
        """
        params: dict[str, Any] = {"limit": limit}
        if filter is not None:
            params["filter"] = filter
        return await self.tools_call("review_duplicate_pairs", params)

    async def list_audit_log(
        self,
        action: str | None = None,
        actor: str | None = None,
        knowledge_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Журнал действий по качеству + статус авто-гейта (Фаза 3).

        Read-only: записи audit.jsonl (deprecate/restore/bulk/auto/fp_rejection/
        scan_completed) + fp_stats + auto_dedup_enabled/config.

        Args:
            action: фильтр по действию (опционально).
            actor: фильтр по actor (опционально, "auto" для только-авто).
            knowledge_id: фильтр по записи (опционально).
            limit: макс. число записей (default 50, max 200 на сервере).

        Returns:
            {"records": [...], "fp_stats": {...}, "auto_dedup_enabled": bool,
             "auto_dedup_config": {...}}
        """
        params: dict[str, Any] = {"limit": limit}
        if action is not None:
            params["action"] = action
        if actor is not None:
            params["actor"] = actor
        if knowledge_id is not None:
            params["knowledge_id"] = knowledge_id
        return await self.tools_call("list_audit_log", params)

    async def delete_entry(
        self, knowledge_id: str, cascade: bool = False
    ) -> dict[str, Any]:
        """Удалить запись (soft-delete → .trash/ + Qdrant).

        Args:
            knowledge_id: ID записи
            cascade: удалить также дочерние секции книги

        Returns:
            {"knowledge_id": ..., "deleted": True, "cascade_deleted": N}
        """
        params: dict[str, Any] = {"knowledge_id": knowledge_id}
        if cascade:
            params["cascade"] = cascade
        return await self.tools_call("delete_entry", params)

    # ── 13.21: PDF import ──────────────────────────────────

    async def upload_pdf(self, file_path: str, filename: str = "") -> dict[str, Any]:
        """POST /upload — загрузить PDF через multipart.

        Args:
            file_path: путь к локальному PDF-файлу.
            filename: имя файла (опционально).

        Returns:
            {"pdf_path": str, "content_hash": str, "size": int, "upload_id": str}
        """
        url = f"{self.base_url}/upload"
        name = filename or Path(file_path).name
        with open(file_path, "rb") as f:
            response = await self._client.post(
                url,
                files={"file": (name, f, "application/pdf")},
                headers={"X-API-Key": self.api_key},
                timeout=120.0,
            )
        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("detail", response.text[:200])
            except (json.JSONDecodeError, ValueError):
                detail = response.text[:200]
            raise RuntimeError(f"PDF upload failed ({response.status_code}): {detail}")
        return response.json()

    async def list_imports(self) -> list[dict[str, Any]]:
        """GET /imports — список всех импортов в очереди."""
        url = f"{self.base_url}/imports"
        response = await self._client.get(url, headers=self._headers(), timeout=10.0)
        if response.status_code != 200:
            return []
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return []

    async def start_convert(self, pdf_path: str, base_id: str) -> dict[str, Any]:
        """POST /imports/convert — операция «Преобразовать» (PDF→текст).

        Создаёт карточку очереди + фоновую задачу на сервере.
        Returns: {"import_id": "{base_id}:convert", "status": "started"|"queued"}.
        """
        url = f"{self.base_url}/imports/convert"
        response = await self._client.post(
            url,
            json={"pdf_path": pdf_path, "base_id": base_id},
            headers=self._headers(),
            timeout=10.0,
        )
        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("detail", response.text[:200])
            except (json.JSONDecodeError, ValueError):
                detail = response.text[:200]
            raise RuntimeError(f"start_convert failed ({response.status_code}): {detail}")
        return response.json()

    async def start_analyze(self, content: str, base_id: str) -> dict[str, Any]:
        """POST /imports/analyze — операция «Обработать» (AI-классификация).

        Создаёт карточку очереди + фоновую задачу на сервере.
        Returns: {"import_id": "{base_id}:analyze", "status": "started"}.
        """
        url = f"{self.base_url}/imports/analyze"
        response = await self._client.post(
            url,
            json={"content": content, "base_id": base_id},
            headers=self._headers(),
            timeout=10.0,
        )
        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("detail", response.text[:200])
            except (json.JSONDecodeError, ValueError):
                detail = response.text[:200]
            raise RuntimeError(f"start_analyze failed ({response.status_code}): {detail}")
        return response.json()

    async def get_imports_active(self) -> dict[str, Any]:
        """GET /imports/active — текущий running-импорт (F5-recovery)."""
        url = f"{self.base_url}/imports/active"
        response = await self._client.get(url, headers=self._headers(), timeout=10.0)
        if response.status_code != 200:
            return {"active": False}
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return {"active": False}

    async def cancel_import(self, import_id: str) -> dict[str, Any]:
        """POST /imports/{import_id}/cancel — отменить импорт."""
        url = f"{self.base_url}/imports/{import_id}/cancel"
        response = await self._client.post(url, headers=self._headers(), timeout=10.0)
        if response.status_code != 200:
            return {"cancelled": False, "reason": f"HTTP {response.status_code}"}
        return response.json()

    async def remove_import(self, import_id: str) -> dict[str, Any]:
        """POST /imports/{import_id}/remove — удалить запись из очереди."""
        url = f"{self.base_url}/imports/{import_id}/remove"
        response = await self._client.post(url, headers=self._headers(), timeout=10.0)
        if response.status_code != 200:
            return {"removed": False, "reason": f"HTTP {response.status_code}"}
        return response.json()

    async def remove_finished(self) -> dict[str, Any]:
        """POST /imports/remove-finished — удалить все завершённые записи."""
        url = f"{self.base_url}/imports/remove-finished"
        response = await self._client.post(url, headers=self._headers(), timeout=10.0)
        if response.status_code != 200:
            return {"removed": 0, "reason": f"HTTP {response.status_code}"}
        return response.json()
