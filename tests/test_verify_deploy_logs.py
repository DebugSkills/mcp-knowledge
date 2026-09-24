#!/usr/bin/env python3
r"""Регресс-тест гейта V2 verify-deploy.sh: FAIL по УРОВНЮ лога, не по подстроке.

Дефект (2026-09-24, make push на живом стеке): INFO-сводка reconcile
`... [INFO] ... errors=1` ловилась grep'ом подстроки `error` → ложный FAIL
`[FAIL] V2 server logs · 1 строк error/traceback/critical` при здоровом стеке.

Гейт V2 матчит только признаки РЕАЛЬНОГО сбоя: `[ERROR]`, `[CRITICAL]`,
голый `CRITICAL`, `Traceback (most recent call last)`, `[FATAL]`
(формат лога: `YYYY-MM-DD HH:MM:SS,mmm [LEVEL] name: msg`).

Тестируемость: источник лога V2 переопределяется `VERIFY_LOG_FILE`
(fixture-файл); V1/V3/V4 уводятся на закрытый порт 127.0.0.1:9 с
`VERIFY_WAIT=0` — тест не зависит от живого стека, результаты V1/V3/V4 не
ассертятся. Секреты не читаются и не печатаются (ключ V3 остаётся в .env,
в fixture-окружение не попадает).

Запуск: `.venv/bin/python -m pytest tests/test_verify_deploy_logs.py -v`
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-deploy.sh"

# Формат лога проекта: YYYY-MM-DD HH:MM:SS,mmm [LEVEL] name: msg
CLEAN_LOG = (
    "2026-09-24 18:27:54,185 [INFO] mcp_knowledge.reconcile: ✅ RECONCILE complete:"
    " checked=8407, reindexed=0, skipped=8406, orphans=0, orphaned_detected=0, errors=1\n"
    "2026-09-24 18:27:55,000 [WARNING] mcp_knowledge.qdrant: collection NotFound,"
    " error_count=2, will recreate\n"
    "2026-09-24 18:27:56,000 [INFO] mcp_knowledge.api: GET /health 200\n"
)

BAD_LOG = (
    "2026-09-24 18:27:54,185 [INFO] mcp_knowledge.reconcile: ✅ RECONCILE complete: errors=1\n"
    "2026-09-24 18:28:00,111 [ERROR] mcp_knowledge.importer: Oops\n"
    "Traceback (most recent call last):\n"
    '  File "/app/mcp_server/src/mcp_knowledge/x.py", line 1, in <module>\n'
)


def run_v2(tmp_path: Path, log_text: str) -> str:
    """verify-deploy.sh с подменённым источником лога V2 (fixture-файл).

    Выход = полный stdout+stderr (exit-код не важен: V1/V3/V4 заведомо
    падают на закрытом порту — ассертим только строки V2).
    """
    fixture = tmp_path / "fixture.log"
    fixture.write_text(log_text, encoding="utf-8")
    env = {
        "VERIFY_LOG_FILE": str(fixture),
        "VERIFY_WAIT": "0",  # V1: без стартового окна ожидания
        "SERVER_HEALTH_URL": "http://127.0.0.1:9/health",  # 9 = discard, refused
        "MCP_URL": "http://127.0.0.1:9/mcp",
        "CONSOLE_URL": "http://127.0.0.1:9/",
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.stdout + proc.stderr


def test_v2_pass_info_and_warning_with_error_substring(tmp_path):
    """INFO `errors=1` / WARNING `error_count=`, `NotFound` → V2 PASS (не FAIL)."""
    out = run_v2(tmp_path, CLEAN_LOG)
    assert "[PASS] V2 server logs" in out
    assert "[FAIL] V2" not in out


def test_v2_fail_error_level_and_traceback(tmp_path):
    """`[ERROR]` и `Traceback (most recent call last)` → V2 FAIL, строки с `|`."""
    out = run_v2(tmp_path, BAD_LOG)
    assert "[FAIL] V2 server logs" in out
    assert "| 2026-09-24 18:28:00,111 [ERROR] mcp_knowledge.importer: Oops" in out
    assert "| Traceback (most recent call last):" in out
    # INFO-строка с errors=1 не попадает в вывод плохих строк
    assert "| 2026-09-24 18:27:54,185 [INFO]" not in out


def test_v2_fail_critical_and_fatal(tmp_path):
    """`[CRITICAL]`, голый `CRITICAL`, `[FATAL]` → V2 FAIL."""
    out = run_v2(
        tmp_path,
        "2026-09-24 18:29:00,000 [CRITICAL] mcp_knowledge.core: boom\n"
        "2026-09-24 18:29:01,000 [INFO] app: entering CRITICAL section\n"
        "2026-09-24 18:29:02,000 [FATAL] app: dead\n",
    )
    assert "[FAIL] V2 server logs" in out


def test_v2_missing_log_file_is_fail_not_silent_pass(tmp_path):
    """Нечитаемый VERIFY_LOG_FILE → явный FAIL, а не тихий PASS по пустоте."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "VERIFY_LOG_FILE": str(tmp_path / "no-such.log"),
            "VERIFY_WAIT": "0",
            "SERVER_HEALTH_URL": "http://127.0.0.1:9/health",
            "MCP_URL": "http://127.0.0.1:9/mcp",
            "CONSOLE_URL": "http://127.0.0.1:9/",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True, text=True, timeout=120, check=False,
    )
    out = proc.stdout + proc.stderr
    assert "[FAIL] V2" in out
    assert "VERIFY_LOG_FILE" in out
