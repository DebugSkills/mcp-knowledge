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


# ── errors_cron.sh (AC-cron-1 / R8 / R9; только --file модель, БЕЗ crontab) ──

MARK_BEGIN = "# mcp-knowledge errors-notify (code-2026-09-24-016)"
MARK_END = "# mcp-knowledge errors-notify (end)"


class TestCronInstall:
    def test_install_three_jobs(self, tmp_path):
        """AC-cron-1: 3 джобы в маркер-блоке, collector ПЕРВЫМ (P3-c)."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("17 3 * * * /usr/bin/existing-job\n", encoding="utf-8")
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        assert r.returncode == 0, r.stderr
        content = cf.read_text()
        lines = [ln for ln in content.splitlines() if ln.strip()]
        block = [i for i, ln in enumerate(lines) if ln == MARK_BEGIN]
        assert len(block) == 1
        b = block[0]
        assert lines[b + 1].startswith("*/5")  # collector */5 ПЕРВЫМ
        assert "errors_collect.py" in lines[b + 1]
        assert "errors_alert.py" in lines[b + 2]
        assert lines[b + 3].startswith("2 10 * * 1")  # weekly Пн 10:02
        assert "errors_report.py" in lines[b + 3]
        assert lines[b + 4] == MARK_END
        assert "17 3 * * * /usr/bin/existing-job" in lines  # чужое не тронуто

    def test_absolute_paths_and_cd(self, tmp_path):
        """R8: команды через cd <абсолютный BASE>, все пути абсолютные."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        assert r.returncode == 0, r.stderr
        for ln in cf.read_text().splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            fields = ln.split(None, 5)
            assert len(fields) == 6, ln
            cmd = fields[5]
            assert cmd.startswith("cd /"), f"не абсолютный cd: {cmd}"
            # каждый путь-токен после cd — абсолютный (no ../no bare scripts/)
            body = cmd.split("&&", 1)[1]
            toks = [t for t in body.split() if "/" in t and not t.startswith(">>") and t != "2>&1"]
            for t in toks:
                assert t.startswith("/"), f"относительный путь: {t} в {cmd}"

    def test_backup_created_before_edit(self, tmp_path):
        """Бэкап crontab в .trash/ ДО правки (R6-прецедент 008)."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("# old\n", encoding="utf-8")
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        assert r.returncode == 0, r.stderr
        backups = list((ROOT / ".trash").glob("crontab-backup-*.txt"))
        assert backups, "нет бэкапа в .trash/"
        latest = max(backups, key=lambda p: p.stat().st_mtime)
        assert latest.read_text() == "# old\n"  # состояние ДО правки

    def test_install_idempotent_no_dupes(self, tmp_path):
        """Повторный --install = 0 дублей (блок один, строк блока — по одному)."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        args = ("--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        sh(CRON_SH, *args)
        r2 = sh(CRON_SH, *args)
        assert r2.returncode == 0, r2.stderr
        content = cf.read_text()
        assert content.count(MARK_BEGIN) == 1
        assert content.count(MARK_END) == 1
        assert content.count("errors_collect.py") == 1
        assert content.count("errors_alert.py") == 1
        assert content.count("errors_report.py") == 1

    def test_config_overlay_cron_logs(self, tmp_path):
        """R9: config-оверлей cron_logs — union с существующими (чужие не терять)."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        dr = tmp_path / "data"
        cfgp = dr / "logs" / "errors" / "config.json"
        cfgp.parent.mkdir(parents=True)
        cfgp.write_text(json.dumps(
            {"cron_logs": ["/var/log/mcp-backup.log", "/var/log/mcp-quality.log"]}), encoding="utf-8")
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(dr))
        assert r.returncode == 0, r.stderr
        merged = json.loads(cfgp.read_text())
        cl = merged["cron_logs"]
        assert "/var/log/mcp-backup.log" in cl  # чужое сохранено
        for name in ("collector", "alerts", "weekly"):
            assert any(name in p for p in cl), f"нет {name}.log в cron_logs"
        assert len([p for p in cl if "collector" in p]) == 1  # без дублей

    def test_config_overlay_backup(self, tmp_path):
        """R9: старый config.json бэкапится до мерджа."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        dr = tmp_path / "data"
        cfgp = dr / "logs" / "errors" / "config.json"
        cfgp.parent.mkdir(parents=True)
        original = {"cron_logs": ["/var/log/mcp-backup.log"]}
        cfgp.write_text(json.dumps(original), encoding="utf-8")
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(dr))
        assert r.returncode == 0, r.stderr
        backups = list((ROOT / ".trash").glob("errors-config-backup-*.json"))
        assert backups
        latest = max(backups, key=lambda p: p.stat().st_mtime)
        assert json.loads(latest.read_text()) == original

    def test_remove_restores_original(self, tmp_path):
        """--remove: вынимает ТОЛЬКО свой блок + свои 3 пути, чужое не трогает."""
        cf = tmp_path / "crontab.txt"
        original = "17 3 * * * /usr/bin/existing-job\n"
        cf.write_text(original, encoding="utf-8")
        args = ("--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        sh(CRON_SH, *args)
        r = sh(CRON_SH, "--remove", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        assert r.returncode == 0, r.stderr
        assert cf.read_text() == original  # бит-в-бит исходник

    def test_remove_orphan_lines_outside_block(self, tmp_path):
        """Наши строки вне блока (ручная вставка) тоже вынимаются --remove."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("*/5 * * * * cd /x && /x/scripts/errors_collect.py\n", encoding="utf-8")
        r = sh(CRON_SH, "--remove", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        assert r.returncode == 0, r.stderr
        assert "errors_collect.py" not in cf.read_text()

    def test_status(self, tmp_path):
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        args = ("--file", str(cf), "--data-root", str(tmp_path / "data"))
        r0 = sh(CRON_SH, "--status", *args)
        assert r0.returncode == 0
        assert "not installed" in r0.stdout.lower()
        sh(CRON_SH, "--install", *args)
        r1 = sh(CRON_SH, "--status", *args)
        assert r1.returncode == 0
        assert "errors_collect.py" in r1.stdout
        assert "3" in r1.stdout  # счётчик джобов

    def test_validator_rejects_relative(self, tmp_path):
        """R8 (unit): --validate на файле с относительной строкой → exit≠0."""
        bad = tmp_path / "bad-cron.txt"
        bad.write_text("*/5 * * * * cd rel && ./scripts/errors_collect.py\n", encoding="utf-8")
        r = sh(CRON_SH, "--validate", str(bad))
        assert r.returncode != 0
        assert "r8 fail" in (r.stdout + r.stderr).lower()

    def test_validator_accepts_ours(self, tmp_path):
        cf = tmp_path / "crontab.txt"
        cf.write_text("", encoding="utf-8")
        sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(tmp_path / "data"))
        r = sh(CRON_SH, "--validate", str(cf))
        assert r.returncode == 0, r.stdout + r.stderr

    def test_install_rejects_before_write(self, tmp_path):
        """R8 (интеграционно): кривой контент НЕ записывается (валидация до mv)."""
        cf = tmp_path / "crontab.txt"
        cf.write_text("original\n", encoding="utf-8")
        # имитируем генерацию относительного пути через BASE_REL env-хук
        r = sh(CRON_SH, "--install", "--file", str(cf), "--data-root", str(tmp_path / "data"),
               env_over={"ERRORS_CRON_FORCE_REL": "1"})
        assert r.returncode != 0
        assert cf.read_text() == "original\n"  # файл НЕ тронут


# ── AC-collect-1(а): юнит collect_cron_logs на [CRON] exit=1 ──

class TestCollectCronLogsUnit:
    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("errors_collect", ROOT / "scripts" / "errors_collect.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cron_exit_nonzero_is_event(self, tmp_path):
        """[CRON] job=collector exit=1 → событие source=cron_log, P0-признак."""
        mod = self._mod()
        logf = tmp_path / "collector.log"
        logf.write_text("[CRON] job=collector exit=1 dur=2.5 ts=2026-09-24T10:00:00+00:00\n",
                        encoding="utf-8")
        sink = tmp_path / "sink"
        sink.mkdir()
        cfg = {"cron_logs": [str(logf)]}
        state: dict = {}
        events = mod.collect_cron_logs(sink, state, cfg)
        assert len(events) == 1
        ev = events[0]
        assert ev["source"] == "cron_log"
        assert ev["level"] == "ERROR"  # exit≠0 → ERROR
        assert ev.get("priority_hint") == "cron_nonzero"  # P0-признак (E3)
        assert ev["actor_id"] == "cron:collector"
        # повторный прогон: byte-offset дедуп — не дублирует
        again = mod.collect_cron_logs(sink, state, cfg)
        assert again == []

    def test_cron_exit_zero_not_collected(self, tmp_path):
        mod = self._mod()
        logf = tmp_path / "collector.log"
        logf.write_text("[CRON] job=collector exit=0 dur=1.0 ts=2026-09-24T10:00:00+00:00\n",
                        encoding="utf-8")
        sink = tmp_path / "sink"
        sink.mkdir()
        events = mod.collect_cron_logs(sink, {}, {"cron_logs": [str(logf)]})
        assert len(events) == 1
        ev = events[0]
        assert ev["level"] == "INFO"  # exit=0 → INFO
        assert ev.get("priority_hint") is None  # БЕЗ P0-признака — алерты не триггерит


# ── AC-host-3: jinja2-рендер errors-notify.json.j2 (без деплоя) ──

class TestJ2Render:
    def _render(self, inventory_hostname, proxy="http://10.9.9.9:3128"):
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(J2.parent)),
            keep_trailing_newline=True,
            autoescape=False,
        )
        tpl = env.get_template(J2.name)
        return tpl.render(
            vault_telegram_bot_token="123:FAKE",
            vault_telegram_chat_id="-100fake",
            vault_telegram_proxy=proxy,
            inventory_hostname=inventory_hostname,
        )

    @pytest.mark.parametrize("host", ["lup", "aikb"])
    def test_host_rendered_and_valid_json(self, host):
        """AC-host-3: host из inventory_hostname; вывод — валидный JSON."""
        rendered = self._render(host)
        data = json.loads(rendered)
        assert data["host"] == host
        assert data["proxy"] == "http://10.9.9.9:3128"
        assert data["bot_token"] == "123:FAKE"
        assert data["chat_id"] == "-100fake"

    def test_proxy_default_empty(self):
        """vault_telegram_proxy без default → пустая строка (graceful skip)."""
        rendered = self._render("lup", proxy="")
        data = json.loads(rendered)
        assert data["proxy"] == ""  # пусто → sender идёт напрямую (fallback)

    def test_inventory_hostname_fallback_empty(self):
        """inventory_hostname не задан → host='' → sender возьмёт gethostname()."""
        import jinja2
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(J2.parent)),
                                 keep_trailing_newline=True, autoescape=False)
        rendered = env.get_template(J2.name).render(
            vault_telegram_bot_token="t", vault_telegram_chat_id="c",
            vault_telegram_proxy="p",
        )  # inventory_hostname НЕ передан
        assert json.loads(rendered)["host"] == ""
