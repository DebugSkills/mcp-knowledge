#!/usr/bin/env python3
r"""Тесты V6 verify-deploy.sh: documents blob-store mount (Фаза 0).

V6 проверяет:
  - статически: compose-файлы объявляют маунт /app/data/documents (гейт A1,
    якорный grep — закомментированная строка не считается);
  - runtime (при живом контейнере): bind-mount активен, не overlay (гейт A2).

Тестируемость: пути compose — VERIFY_COMPOSE_DEV/PROD; runtime — fake-docker
shim в PATH (bind → PASS, volume → FAIL); docker-демон не трогается.
Запуск: .venv/bin/python -m pytest tests/test_verify_deploy_documents.py -v
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-deploy.sh"

# Пустой PATH без docker → runtime-часть V6 уходит в SKIP, статическая — детерминирована.
BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "VERIFY_WAIT": "0",
    "SERVER_HEALTH_URL": "http://127.0.0.1:9/health",
    "MCP_URL": "http://127.0.0.1:9/mcp",
    "CONSOLE_URL": "http://127.0.0.1:9/",
}

MOUNT_RE = re.compile(r"^[ \t]*-[ \t]+.*:/app/data/documents")


def _good_compose(tmp_path: Path) -> Path:
    f = tmp_path / "good.yml"
    f.write_text("services:\n  mcp-server:\n    volumes:\n"
                 "      - ./data/documents:/app/data/documents\n", encoding="utf-8")
    return f


def _commented_compose(tmp_path: Path) -> Path:
    f = tmp_path / "commented.yml"
    f.write_text("services:\n  mcp-server:\n    volumes:\n"
                 "      # - ./data/documents:/app/data/documents\n", encoding="utf-8")
    return f


def _bad_compose(tmp_path: Path) -> Path:
    f = tmp_path / "bad.yml"
    f.write_text("services:\n  mcp-server:\n    volumes:\n"
                 "      - ./data/dlq:/app/data/dlq\n", encoding="utf-8")
    return f


def _fake_docker(tmp_path: Path, mount_type: str) -> Path:
    """Fake-docker shim: `ps` → id, `inspect` → mount_type. PATH-препозиторий."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  ps) echo abc123def456 ;;\n"
        f"  inspect) echo \"{mount_type}\" ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return d


def _run_static(tmp_path: Path, dev: Path, prod: Path) -> str:
    env = dict(BASE_ENV)
    env["VERIFY_COMPOSE_DEV"] = str(dev)
    env["VERIFY_COMPOSE_PROD"] = str(prod)
    env["VERIFY_NO_DOCKER"] = "1"  # тест-хук: не трогаем живой docker-демон
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.stdout + proc.stderr


def _run_runtime(tmp_path: Path, mount_type: str) -> str:
    """Runtime-ветка V6 с fake-docker; V2 уводится на чистый лог-файл."""
    log = tmp_path / "clean.log"
    log.write_text("", encoding="utf-8")
    env = dict(BASE_ENV)
    env["PATH"] = f"{_fake_docker(tmp_path, mount_type)}:/usr/bin:/bin"
    env["VERIFY_COMPOSE_DEV"] = str(_good_compose(tmp_path))
    env["VERIFY_COMPOSE_PROD"] = str(_good_compose(tmp_path))
    env["VERIFY_LOG_FILE"] = str(log)   # V2 читает файл, docker logs не дёргается
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.stdout + proc.stderr


def test_v6_static_pass_then_skip_without_docker(tmp_path):
    """Оба compose с mount, docker нет → A1 пройдена, runtime SKIP."""
    out = _run_static(tmp_path, _good_compose(tmp_path), _good_compose(tmp_path))
    assert "[FAIL] V6" not in out
    assert "[SKIP] V6 documents mount runtime" in out


def test_v6_static_fail_when_mount_missing(tmp_path):
    """Dev без mount → FAIL (не тихий skip)."""
    out = _run_static(tmp_path, _bad_compose(tmp_path), _good_compose(tmp_path))
    assert "[FAIL] V6 documents mount" in out
    assert "dev" in out


def test_v6_static_fail_when_prod_missing(tmp_path):
    """Prod без mount → FAIL."""
    out = _run_static(tmp_path, _good_compose(tmp_path), _bad_compose(tmp_path))
    assert "[FAIL] V6 documents mount" in out
    assert "prod" in out


def test_v6_static_fail_when_mount_commented(tmp_path):
    """Закомментированный mount (P2-4) → FAIL: якорный grep не должен его признать."""
    out = _run_static(tmp_path, _commented_compose(tmp_path), _good_compose(tmp_path))
    assert "[FAIL] V6 documents mount" in out
    assert "dev" in out


def test_v6_runtime_bind_passes(tmp_path):
    """fake-docker inspect=bind → V6 PASS (не overlay)."""
    out = _run_runtime(tmp_path, "bind")
    assert "[PASS] V6 documents mount" in out
    assert "[FAIL] V6" not in out


def test_v6_runtime_volume_fails(tmp_path):
    """fake-docker inspect=volume → V6 FAIL (volume ≠ bind)."""
    out = _run_runtime(tmp_path, "volume")
    assert "[FAIL] V6 documents mount" in out


def test_repo_compose_files_declare_documents_mount():
    """Гард: реальные compose-файлы содержат НЕзакомментированный mount documents."""
    for rel in ("docker-compose.yml", "docker-compose.prod.yml"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert any(MOUNT_RE.match(ln) for ln in text.splitlines()), \
            f"{rel}: нет незакомментированного mount /app/data/documents"
