"""Subprocess-тесты shell-хелперов 016: errors_notify_import.sh + errors_cron.sh.

Спека §7.5/§7.6: AC-import-1 (0600/владелец/fingerprint/идемпотентность/
exit≠0 с именем переменной) · AC-host-4 (host: --host / hostname -s) · R5
(секреты не в stdout/stderr) · AC-cron-1 (3 джобы, маркер-блок, бэкап .trash,
config-оверлей cron_logs, дедуп, --remove обратимо, абсолютность путей/R8) ·
AC-collect-1(а) (collect_cron_logs на [CRON] exit=1 → событие cron_log) ·
AC-host-3 (jinja2-рендер errors-notify.json.j2 с fake inventory).

Герметичность: только файлы-модели (tmp_path), НАСТОЯЩИЙ crontab не трогаем;
sudo не требуется (SUDO_USER моделируется env).
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMPORT_SH = ROOT / "scripts" / "errors_notify_import.sh"
CRON_SH = ROOT / "scripts" / "errors_cron.sh"
J2 = ROOT / "ansible" / "templates" / "errors-notify.json.j2"

TOKEN = "9999888877:AAfake_import_token_Zz"
CHAT = "-100111222333"
PROXY_URL = "http://10.9.9.9:3128"


def sh(script, *args, env_over=None, check=False):
    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env.update(env_over or {})
    return subprocess.run(["bash", str(script), *args], capture_output=True,
                          text=True, env=env, timeout=60, check=check)


def fake_env(tmp_path, *, token=TOKEN, chat=CHAT, https_proxy=PROXY_URL,
             http_proxy=None, no_proxy="localhost,127.0.0.1"):
    lines = []
    if token is not None:
        lines.append(f"TG_TOKEN={token}")
    if chat is not None:
        lines.append(f"TG_CHAT={chat}")
    if https_proxy is not None:
        lines.append(f"HTTPS_PROXY={https_proxy}")
    if http_proxy is not None:
        lines.append(f"HTTP_PROXY={http_proxy}")
    if no_proxy is not None:
        lines.append(f"NO_PROXY={no_proxy}")
    p = tmp_path / "backup-status.env"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── errors_notify_import.sh (AC-import-1 / AC-host-4 / R5) ──

class TestNotifyImport:
    def test_happy_path(self, tmp_path):
        envf = fake_env(tmp_path)
        out = tmp_path / "notify.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out),
               env_over={"SUDO_USER": os.environ.get("USER", "ladmin")})
        assert r.returncode == 0, r.stderr
        data = json.loads(out.read_text())
        assert data["bot_token"] == TOKEN
        assert data["chat_id"] == CHAT
        assert data["proxy"] == PROXY_URL
        assert data["no_proxy"] == "localhost,127.0.0.1"
        assert data["host"]  # непусто (hostname -s)
        assert oct(os.stat(out).st_mode & 0o777)[2:] == "600"  # 0600
        fp = hashlib.sha256(CHAT.encode()).hexdigest()[:12]
        assert f"chat={fp}" in r.stdout
        assert "proxy=set" in r.stdout

    def test_no_secrets_in_output(self, tmp_path):
        """R5/AC-import-1: токен/прокси-креды НИКОГДА не в stdout/stderr."""
        envf = fake_env(tmp_path)
        out = tmp_path / "notify.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out))
        combined = r.stdout + r.stderr
        assert TOKEN not in combined
        assert PROXY_URL not in combined
        assert CHAT not in combined

    def test_host_flag_and_default(self, tmp_path):
        """AC-host-4: --host aikb → host=aikb; без --host → hostname -s."""
        envf = fake_env(tmp_path)
        out_a, out_b = tmp_path / "a.json", tmp_path / "b.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out_a), "--host", "aikb")
        assert r.returncode == 0, r.stderr
        assert json.loads(out_a.read_text())["host"] == "aikb"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out_b))
        assert r.returncode == 0, r.stderr
        import socket
        assert json.loads(out_b.read_text())["host"] == socket.gethostname().split(".")[0]

    def test_idempotent(self, tmp_path):
        envf = fake_env(tmp_path)
        out = tmp_path / "notify.json"
        sh(IMPORT_SH, "--env", str(envf), "--out", str(out))
        first = out.read_text()
        r2 = sh(IMPORT_SH, "--env", str(envf), "--out", str(out))
        assert r2.returncode == 0
        assert out.read_text() == first  # diff ∅ (детерминирован)

    def test_missing_env_file(self, tmp_path):
        r = sh(IMPORT_SH, "--env", str(tmp_path / "nope.env"), "--out", str(tmp_path / "o.json"))
        assert r.returncode != 0
        assert str(tmp_path / "nope.env") in r.stderr

    def test_missing_variable_named(self, tmp_path):
        envf = fake_env(tmp_path, token=None)  # нет TG_TOKEN
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(tmp_path / "o.json"))
        assert r.returncode != 0
        assert "TG_TOKEN" in r.stderr
        assert TOKEN not in r.stderr and CHAT not in r.stderr  # без значений

    def test_missing_chat_named(self, tmp_path):
        envf = fake_env(tmp_path, chat=None)
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(tmp_path / "o.json"))
        assert r.returncode != 0
        assert "TG_CHAT" in r.stderr

    def test_missing_proxy_fails_loud(self, tmp_path):
        """§7.8: политика = прокси; отсутствие обоих → exit≠0 (не молчать)."""
        envf = fake_env(tmp_path, https_proxy=None, http_proxy=None)
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(tmp_path / "o.json"))
        assert r.returncode != 0
        assert "PROXY" in r.stderr

    def test_http_proxy_fallback(self, tmp_path):
        envf = fake_env(tmp_path, https_proxy=None, http_proxy="http://10.0.0.1:8080")
        out = tmp_path / "notify.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out))
        assert r.returncode == 0, r.stderr
        assert json.loads(out.read_text())["proxy"] == "http://10.0.0.1:8080"
        assert "proxy=set" in r.stdout

    def test_no_proxy_optional(self, tmp_path):
        envf = fake_env(tmp_path, no_proxy=None)
        out = tmp_path / "notify.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out))
        assert r.returncode == 0
        assert "no_proxy" not in json.loads(out.read_text())

    def test_sudo_user_empty_warning(self, tmp_path):
        """P3: SUDO_USER пуст → owner НЕ меняется + warning в stderr (не падать)."""
        envf = fake_env(tmp_path)
        out = tmp_path / "notify.json"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out))  # SUDO_USER удалён
        assert r.returncode == 0  # файл валиден — не падать
        assert "SUDO_USER" in r.stderr  # warning задокументирован
        assert json.loads(out.read_text())["bot_token"] == TOKEN

    def test_chown_to_sudo_user(self, tmp_path):
        """Владелец = $SUDO_USER (тест: непривилегированный self-chown)."""
        envf = fake_env(tmp_path)
        out = tmp_path / "notify.json"
        me = os.environ.get("USER") or "ladmin"
        r = sh(IMPORT_SH, "--env", str(envf), "--out", str(out), env_over={"SUDO_USER": me})
        assert r.returncode == 0, r.stderr
        assert out.stat().st_uid == os.getuid()  # chown на себя прошёл
