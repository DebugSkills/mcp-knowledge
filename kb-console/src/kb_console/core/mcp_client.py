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
    ) -> dict[str, Any]:
        """Разрешить quality issue (resolve/deprecate/restore/merge/ignore).

        Args:
            action: merge | deprecate | restore | resolve | ignore
            issue_id: ID issue (опционально, если указан knowledge_id)
            knowledge_id: прямая операция на запись (Фаза 13.14)
            cascade: применить к дочерним секциям книги
            reason: причина решения

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
        return await self.tools_call("resolve_quality_issue", params)

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
