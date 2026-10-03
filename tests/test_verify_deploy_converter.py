#!/usr/bin/env python3
r"""Тесты V7 verify-deploy.sh: sidecar-канонизатор kb-converter.

V7 проверяет: probe CONVERTER_HEALTH_URL (дефолт http://localhost:8660/health)
→ валидный JSON со статусом; отсутствие/невалидный → FAIL.
VERIFY_SKIP_CONVERTER=1 — явный SKIP (не fail, не молчаливый pass).

Дополнительно (г): healthcheck kb-converter объявлен в обоих compose-файлах
и пробует /health (8660) — чтобы `docker compose ps` показывал состояние.

Запуск: .venv/bin/python -m pytest tests/test_verify_deploy_converter.py -v
"""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-deploy.sh"

# Пустой PATH без docker → V2/V6-runtime уходят в SKIP; мёртвые URL → V1/V4 FAIL
# (не мешают проверке маркеров V7). V5 без CONSOLE_LAN_IP → SKIP, V3 без ключа → SKIP.
BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "VERIFY_WAIT": "0",
    "SERVER_HEALTH_URL": "http://127.0.0.1:9/health",
    "MCP_URL": "http://127.0.0.1:9/mcp",
    "CONSOLE_URL": "http://127.0.0.1:9/",
    "CONVERTER_HEALTH_URL": "http://127.0.0.1:9/health",  # мёртвый порт → V7 FAIL
}


def _run(**overrides):
    env = dict(BASE_ENV)
    env["VERIFY_NO_DOCKER"] = "1"  # тест-хук: не трогаем живой docker-демон
    env.update(overrides)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.stdout + proc.stderr


def _fake_curl_ok(tmp_path: Path) -> Path:
    """Fake-curl shim: на URL с маркером conv-health отдаёт {"status":"ok"},
    на всё остальное — exit 1 (имитация недоступности). PATH-препозиторий."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "curl"
    shim.write_text(
        "#!/bin/bash\n"
        "for a in \"$@\"; do\n"
        "  case \"$a\" in\n"
        "    *conv-health*) printf '{\"status\": \"ok\"}'; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return d


# ── (а) статика: V-шаг, env-дефолт, сводка ──
def test_v7_step_env_and_summary_present():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'CONVERTER_HEALTH_URL="${CONVERTER_HEALTH_URL:-http://localhost:8660/health}"' in text
    assert "v7_converter() {" in text            # функция определена
    assert "\nv7_converter\n" in text            # вызывается внизу (не мёртвый код)
    assert "V7  converter" in text               # шапка/сводка упоминает converter


# ── (б) детектор: нет ответа → FAIL (не молчаливый пропуск) ──
def test_v7_fail_when_no_response():
    out = _run()
    assert "[FAIL] V7 converter" in out
    assert "[SKIP] V7" not in out


# ── (б+) зелёный путь: валидный JSON со статусом → PASS ──
def test_v7_pass_when_healthy(tmp_path):
    env = dict(BASE_ENV)
    env["PATH"] = f"{_fake_curl_ok(tmp_path)}:/usr/bin:/bin"
    env["CONVERTER_HEALTH_URL"] = "http://127.0.0.1:9/conv-health"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    out = proc.stdout + proc.stderr
    assert "[PASS] V7 converter /health" in out
    assert "[FAIL] V7" not in out


# ── (в) VERIFY_SKIP_CONVERTER=1 → явный SKIP (не fail, не тихий pass) ──
def test_v7_skip_when_flagged():
    out = _run(VERIFY_SKIP_CONVERTER="1")
    assert "[SKIP] V7 converter" in out
    assert "[FAIL] V7" not in out
    assert "[PASS] V7" not in out


# ── (г) healthcheck kb-converter в обоих compose-файлах ──
def test_converter_healthcheck_in_both_composes():
    for rel in ("docker-compose.yml", "docker-compose.prod.yml"):
        data = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        svc = data["services"]["kb-converter"]
        assert "healthcheck" in svc, f"{rel}: kb-converter без healthcheck"
        test_cmd = svc["healthcheck"].get("test")
        assert test_cmd, f"{rel}: healthcheck.test отсутствует"
        joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
        assert "8660/health" in joined, f"{rel}: probe не /health (8660): {joined}"
