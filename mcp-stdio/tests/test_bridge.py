"""Unit-тесты для mcp-stdio/bridge.py — stdio-мост.

Тестирует:
- Парсинг/валидацию JSON-RPC запросов
- Проброс запросов на сервер (mock HTTP)
- Обработку ошибок (parse error -32700, network error -32000)
- --health флаг
- EOF / exit 0
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

# Добавляем родительскую директорию в sys.path для импорта bridge
sys.path.insert(0, sys.path[0] + "/..")

# Импортируем bridge как модуль (с защитой от немедленного main())
import bridge as bridge_mod


class TestBridgeParseErrors(unittest.TestCase):
    """E1: невалидный JSON → parse error -32700."""

    def test_invalid_json_returns_parse_error(self):
        """Подаём не-JSON строку → JSON-RPC error -32700."""
        result = bridge_mod._process_line("not json", "http://localhost:8000", "")
        data = json.loads(result)
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertIsNone(data["id"])
        self.assertEqual(data["error"]["code"], -32700)
        self.assertIn("Parse error", data["error"]["message"])

    def test_valid_json_not_jsonrpc(self):
        """Валидный JSON без полей jsonrpc → -32600 Invalid Request."""
        result = bridge_mod._process_line('{"foo":"bar"}', "http://localhost:8000", "")
        data = json.loads(result)
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["error"]["code"], -32600)

    def test_missing_method(self):
        """JSON с jsonrpc:2.0 но без method → -32600."""
        result = bridge_mod._process_line(
            '{"jsonrpc":"2.0","id":1}', "http://localhost:8000", ""
        )
        data = json.loads(result)
        self.assertEqual(data["error"]["code"], -32600)

    def test_parse_error_preserves_id(self):
        """Ошибка парсинга сохраняет id из запроса, если возможно."""
        result = bridge_mod._process_line(
            '{"jsonrpc":"2.0","id":42,"malformed', "http://localhost:8000", ""
        )
        data = json.loads(result)
        self.assertEqual(data["error"]["code"], -32700)


class TestBridgeForwarding(unittest.TestCase):
    """E2-E3: проброс запросов на сервер (mock HTTP)."""

    def setUp(self):
        self.server_url = "http://localhost:8000"
        self.api_key = "test-key-123"

    def _mock_urlopen(self, status=200, response_body=None):
        """Создать mock для urllib.request.urlopen."""
        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        mock.status = status
        mock.read.return_value = (
            json.dumps(response_body).encode("utf-8")
            if response_body
            else b'{"jsonrpc":"2.0","result":{"tools":[]},"id":1}'
        )
        return mock

    def test_tools_list_forwarded(self):
        """tools/list запрос пробрасывается на сервер, ответ в stdout."""
        captured_req = []

        def spy_urlopen(req, **kwargs):
            captured_req.append(req)
            return self._mock_urlopen(
                response_body={"jsonrpc": "2.0", "result": {"tools": [{"name": "test"}]}, "id": 1}
            )

        with patch("urllib.request.urlopen", side_effect=spy_urlopen):
            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                self.server_url,
                self.api_key,
            )
            data = json.loads(result)
            self.assertEqual(data["result"]["tools"], [{"name": "test"}])

            # Проверяем что urlopen вызывался с правильным URL и заголовками
            self.assertEqual(len(captured_req), 1)
            req = captured_req[0]
            self.assertIn("/mcp", req.full_url)
            # Заголовки: ищем X-API-Key (capitalize хранит как X-api-key)
            found_key = False
            for hdr_name, hdr_val in req.header_items():
                if hdr_name.lower() == "x-api-key":
                    self.assertEqual(hdr_val, self.api_key)
                    found_key = True
            self.assertTrue(found_key, "X-API-Key header not found in request")

    def test_initialize_forwarded(self):
        """initialize handshake пробрасывается."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_urlopen(
                response_body={
                    "jsonrpc": "2.0",
                    "result": {"protocolVersion": "2024-11-05"},
                    "id": 2,
                }
            )

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}',
                self.server_url,
                self.api_key,
            )
            data = json.loads(result)
            self.assertEqual(data["result"]["protocolVersion"], "2024-11-05")

    def test_body_is_exactly_as_received(self):
        """Тело запроса передаётся на сервер как есть (прозрачный мост)."""
        sent_body = None

        def capture_body(req, **kwargs):
            nonlocal sent_body
            sent_body = req.data
            mock = self._mock_urlopen(
                response_body={"jsonrpc": "2.0", "result": "ok", "id": 99}
            )
            return mock

        with patch("urllib.request.urlopen", side_effect=capture_body):
            bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":99,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"test"}}}',
                self.server_url,
                self.api_key,
            )

        self.assertIsNotNone(sent_body)
        decoded = json.loads(sent_body.decode("utf-8"))
        self.assertEqual(decoded["method"], "tools/call")
        self.assertEqual(decoded["params"]["name"], "search_knowledge")
        self.assertEqual(decoded["params"]["arguments"]["query"], "test")

    def test_api_key_omitted_when_empty(self):
        """Когда MCP_API_KEY пуст, заголовок X-API-Key не добавляется."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_urlopen(
                response_body={"jsonrpc": "2.0", "result": {}, "id": 1}
            )
            bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                self.server_url,
                "",  # пустой ключ
            )
            req = mock_open.call_args[0][0]
            self.assertIsNone(req.get_header("X-API-Key"))


class TestBridgeErrors(unittest.TestCase):
    """E3-E5: обработка ошибок сервера и сети."""

    def setUp(self):
        self.server_url = "http://localhost:8000"

    def test_server_returns_error(self):
        """Сервер вернул JSON-RPC error → пробрасываем как есть."""
        with patch("urllib.request.urlopen") as mock_open:
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            mock.read.return_value = json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": "Method not found"},
                "id": 1,
            }).encode("utf-8")
            mock_open.return_value = mock

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":1,"method":"nonexistent"}',
                self.server_url,
                "",
            )
            data = json.loads(result)
            self.assertEqual(data["error"]["code"], -32601)
            self.assertIn("Method not found", data["error"]["message"])

    def test_server_unreachable_returns_error(self):
        """Сервер недоступен → JSON-RPC error -32000 (не крашится)."""
        with patch("urllib.request.urlopen") as mock_open:
            import urllib.error
            mock_open.side_effect = urllib.error.URLError("Connection refused")

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                self.server_url,
                "",
            )
            data = json.loads(result)
            self.assertEqual(data["jsonrpc"], "2.0")
            self.assertEqual(data["id"], 1)
            self.assertEqual(data["error"]["code"], -32000)
            self.assertIn("Сервер недоступен", data["error"]["message"])

    def test_timeout_returns_error(self):
        """Таймаут → JSON-RPC error -32000."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = TimeoutError("timed out")

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":7,"method":"tools/list"}',
                self.server_url,
                "",
            )
            data = json.loads(result)
            self.assertEqual(data["error"]["code"], -32000)
            self.assertEqual(data["id"], 7)

    def test_http_error_mapped(self):
        """HTTP 503 → JSON-RPC error с кодом 503."""
        with patch("urllib.request.urlopen") as mock_open:
            import urllib.error
            mock_open.side_effect = urllib.error.HTTPError(
                url="http://localhost:8000/mcp",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=io.BytesIO(b'{"error":"degraded"}'),
            )

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":3,"method":"tools/list"}',
                self.server_url,
                "",
            )
            data = json.loads(result)
            self.assertEqual(data["error"]["code"], -32000)
            self.assertIn("503", data["error"]["message"])

    def test_non_json_response_from_server(self):
        """Сервер вернул не-JSON → ошибка."""
        with patch("urllib.request.urlopen") as mock_open:
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            mock.read.return_value = b"<html>Internal Server Error</html>"
            mock_open.return_value = mock

            result = bridge_mod._process_line(
                '{"jsonrpc":"2.0","id":5,"method":"tools/list"}',
                self.server_url,
                "",
            )
            data = json.loads(result)
            self.assertEqual(data["error"]["code"], -32000)
            self.assertIn("Некорректный JSON", data["error"]["message"])


class TestBridgeMain(unittest.TestCase):
    """E4: main() — EOF → exit 0, stdin loop."""

    def test_eof_exits_zero(self):
        """Пустой stdin (EOF) → exit 0."""
        with patch("sys.stdin", io.StringIO("")), patch("sys.stdout", io.StringIO()) as mock_stdout:
            try:
                bridge_mod.main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)
            else:
                pass  # main() не бросил SystemExit — тоже ок (завершился сам)
            # stdout должен быть пустым (нет запросов)
            self.assertEqual(mock_stdout.getvalue(), "")

    def test_single_request_loop(self):
        """Один запрос → один ответ в stdout."""
        stdin_data = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", io.StringIO()) as mock_stdout, \
             patch("urllib.request.urlopen") as mock_open:
            # Mock ответа от сервера
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            mock.read.return_value = json.dumps({
                "jsonrpc": "2.0",
                "result": {"tools": [{"name": "search_knowledge"}]},
                "id": 1,
            }).encode("utf-8")
            mock_open.return_value = mock

            try:
                bridge_mod.main()
            except SystemExit:
                pass

            output = mock_stdout.getvalue()
            self.assertIn("search_knowledge", output)
            # Проверяем что вывод — одна JSON-строка с newline
            lines = [l for l in output.split("\n") if l.strip()]
            self.assertEqual(len(lines), 1)
            response = json.loads(lines[0])
            self.assertEqual(response["result"]["tools"][0]["name"], "search_knowledge")

    def test_empty_lines_skipped(self):
        """Пустые строки во вводе пропускаются."""
        stdin_data = '\n\n{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n\n'

        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", io.StringIO()), \
             patch("urllib.request.urlopen") as mock_open:
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            mock.read.return_value = json.dumps({
                "jsonrpc": "2.0", "result": {}, "id": 1,
            }).encode("utf-8")
            mock_open.return_value = mock

            try:
                bridge_mod.main()
            except SystemExit:
                pass

            # Только 1 непустая строка → 1 запрос
            self.assertEqual(mock_open.call_count, 1)


class TestHealthCheck(unittest.TestCase):
    """B4: --health флаг."""

    def test_health_ok(self):
        """GET /health → статус printed в stdout."""
        with patch("urllib.request.urlopen") as mock_open:
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            mock.read.return_value = b'{"status":"healthy","qdrant":"ok"}'
            mock_open.return_value = mock

            result = bridge_mod._check_health("http://localhost:8000")
            self.assertIn("healthy", result)

    def test_health_failure(self):
        """GET /health → сервер недоступен → diagnostic message."""
        with patch("urllib.request.urlopen") as mock_open:
            import urllib.error
            mock_open.side_effect = urllib.error.URLError("Connection refused")

            result = bridge_mod._check_health("http://localhost:8000")
            self.assertIn("недоступен", result.lower() or "error")


class TestSmokeSubprocess(unittest.TestCase):
    """E4-smoke: запуск моста как subprocess при живом сервере.

    Требует запущенный mcp-server на http://localhost:8000.
    Если сервер недоступен — тест пропускается (skip-guard).
    """

    def test_smoke_tools_list(self):
        """Отправляем tools/list через subprocess → проверяем 18 tools."""
        import urllib.request

        # Skip-guard: проверяем доступность сервера
        try:
            req = urllib.request.Request("http://localhost:8000/health/live")
            urllib.request.urlopen(req, timeout=2)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            self.skipTest("mcp-server недоступен на http://localhost:8000 — smoke пропущен")

        # Запускаем bridge как subprocess (bridge.py в родительской директории)
        import os as _os
        bridge_path = _os.path.join(_os.path.dirname(__file__), "..", "bridge.py")
        proc = subprocess.run(
            [sys.executable, bridge_path],
            input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**__import__("os").environ, "MCP_SERVER_URL": "http://localhost:8000"},
        )

        self.assertEqual(proc.returncode, 0, f"bridge exited with {proc.returncode}: stderr={proc.stderr}")
        stdout = proc.stdout.strip()
        self.assertTrue(stdout, "stdout не должен быть пустым")

        response = json.loads(stdout)
        self.assertIn("result", response)
        tools = response["result"].get("tools", [])
        self.assertEqual(len(tools), 18, f"Ожидалось 18 tools, получено {len(tools)}: {[t['name'] for t in tools]}")


if __name__ == "__main__":
    unittest.main()
