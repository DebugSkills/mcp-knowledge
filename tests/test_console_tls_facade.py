"""Инварианты TLS-фасада kb-console для доступа из локальной сети.

Трасса ``code-2026-09-26-030``. Проверяем не «файл существует», а свойства,
которые делают доступ безопасным:

* консоль НЕ публикуется в сеть (остаётся 127.0.0.1), в сеть смотрит только фасад;
* фасад слушает ТОЛЬКО LAN-адрес (на хосте есть virbr*/docker0 — ``0.0.0.0`` запрещён);
* TLS обязателен (``tls internal``) + allow-list подсети + HSTS;
* нет скрытого HTTP-листенера ``:80`` (авто-redirect Caddy);
* fail-closed: без ``CONSOLE_LAN_IP`` фасад не стартует;
* verify-deploy умеет per-user креды (V4) и проверяет фасад в LAN (V5);
* ansible кладёт LAN-адрес консоли в ``NO_PROXY`` (иначе прокси → 403).

Комментарии в конфигах не считаются конфигурацией: все ассерты идут по «значимым»
строкам. Функциональная проверка (``caddy validate``) выполняется, если доступен
docker и локально есть образ ``caddy:2-alpine``; иначе — skip (герметичность без сети).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "kb-console" / "caddy" / "Caddyfile"
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "docker-compose.prod.yml")
VERIFY = ROOT / "scripts" / "verify-deploy.sh"
GROUP_VARS = ROOT / "ansible" / "inventory" / "group_vars" / "all.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CADDY_IMAGE = "caddy:2-alpine"


def _effective_lines(text: str) -> list[str]:
    """Значимые строки: без пустых и без комментариев."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _service_block(text: str, name: str) -> str:
    """Блок сервиса верхнего уровня из compose (линейно, без regex-магии)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line == f"  {name}:"), None)
    assert start is not None, f"сервис {name!r} не найден в compose"
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break  # ключ верхнего уровня (volumes:, networks:)
        if re.match(r"^  [a-z0-9][a-z0-9-]*:$", line):
            break  # следующий сервис
        out.append(line)
    return "\n".join(out)


# ── Caddyfile ────────────────────────────────────────────────────────────────


class TestCaddyfile:
    def test_no_plain_console_publish(self) -> None:
        """8085 встречается ровно один раз — как цель reverse_proxy (не как site)."""
        lines = [
            line for line in _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
            if "8085" in line
        ]
        assert len(lines) == 1, f"8085 должен быть только в reverse_proxy: {lines}"
        assert "reverse_proxy 127.0.0.1:8085" in lines[0]

    def test_tls_internal(self) -> None:
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert "tls internal" in [line.strip() for line in lines]

    def test_site_is_lan_env_https_8443(self) -> None:
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert any(line.startswith("https://{$CONSOLE_LAN_IP}:8443") for line in lines)

    def test_bind_only_lan_ip(self) -> None:
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert "bind {$CONSOLE_LAN_IP}" in [line.strip() for line in lines]
        assert not any("0.0.0.0" in line for line in lines), (
            "0.0.0.0 в host-сети выставит консоль во все сети хоста"
        )

    def test_allow_list_by_cidr(self) -> None:
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert "@lan remote_ip {$CONSOLE_LAN_CIDR}" in [line.strip() for line in lines]

    def test_deny_branch_responds_403(self) -> None:
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert any(re.search(r"respond .*403", line) for line in lines), "нет deny-ветки 403"

    def test_no_http_redirect_listener(self) -> None:
        """Авто-redirect Caddy открыл бы :80 на всех интерфейсах (host-сеть)."""
        lines = _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        assert "auto_https disable_redirects" in [line.strip() for line in lines]

    def test_admin_api_off(self) -> None:
        assert any(
            line.strip() == "admin off"
            for line in _effective_lines(CADDYFILE.read_text(encoding="utf-8"))
        )

    def test_security_headers(self) -> None:
        text = CADDYFILE.read_text(encoding="utf-8")
        assert "Strict-Transport-Security" in text
        assert "X-Content-Type-Options" in text
        assert "-Server" in text


# ── compose (dev + prod) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda p: p.name)
class TestCompose:
    def test_facade_service_present(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert "image: caddy:2-alpine" in block

    def test_facade_host_network(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert "network_mode: host" in block, "фасад должен видеть консоль на loopback"

    def test_facade_mounts_config_and_state(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert "./kb-console/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in block
        assert re.search(r"[/\w.-]*caddy/data:/data", block), (
            "CA и сертификаты должны жить в volume (переживать пересоздание)"
        )

    def test_facade_env_vars(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert "CONSOLE_LAN_IP=${CONSOLE_LAN_IP:-}" in block
        assert "CONSOLE_LAN_CIDR=" in block

    def test_fail_closed_guard(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert 'test -n "$$CONSOLE_LAN_IP"' in block
        assert "exit 1" in block

    def test_console_stays_loopback(self, compose: Path) -> None:
        """Регрессия к audit P1 У-1: консоль не публикуется в сеть."""
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console")
        assert "CONSOLE_HOST=127.0.0.1" in block
        assert "CONSOLE_HOST=0.0.0.0" not in block

    def test_facade_depends_on_healthy_console(self, compose: Path) -> None:
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        assert "service_healthy" in block

    def test_facade_healthcheck_is_tcp_liveness(self, compose: Path) -> None:
        """wget на ответ 401 отдаёт exit≠0 → ложный unhealthy; liveness = `nc -z`."""
        block = _service_block(compose.read_text(encoding="utf-8"), "kb-console-tls")
        effective = "\n".join(_effective_lines(block))
        assert "nc -z" in effective
        assert "wget" not in effective, "wget как healthcheck даёт ложный unhealthy на 401"


# ── verify-deploy ────────────────────────────────────────────────────────────


class TestVerifyDeploy:
    def test_v5_defined_and_called(self) -> None:
        content = VERIFY.read_text(encoding="utf-8")
        assert "v5_console_tls()" in content
        assert re.search(r"^v5_console_tls$", content, re.MULTILINE)

    def test_v5_uses_noproxy(self) -> None:
        """Без --noproxy внутренний адрес уходит в корпоративный прокси → 403."""
        body = VERIFY.read_text(encoding="utf-8").split("v5_console_tls()", 1)[1]
        assert "--noproxy '*'" in body

    def test_v5_skips_without_lan_ip(self) -> None:
        body = VERIFY.read_text(encoding="utf-8").split("v5_console_tls()", 1)[1]
        assert "CONSOLE_LAN_IP не задан" in body
        assert "say_skip 5" in body

    def test_v4_prefers_per_user_admin(self) -> None:
        """Per-user стор отклоняет legacy-пароль → V4 берёт bootstrap-админа."""
        body = VERIFY.read_text(encoding="utf-8").split("v4_console()", 1)[1]
        assert "env_val CONSOLE_ADMIN_USER" in body
        assert "env_val CONSOLE_ADMIN_PASSWORD" in body
        assert re.search(r'-u "\$\{user\}:\$\{passwd\}"', body)

    def test_script_syntax(self) -> None:
        if not shutil.which("bash"):
            pytest.skip("нет bash")
        res = subprocess.run(
            ["bash", "-n", str(VERIFY)], capture_output=True, text=True, check=False
        )
        assert res.returncode == 0, res.stderr


# ── ansible: NO_PROXY для офиса ──────────────────────────────────────────────


class TestAnsibleNoProxy:
    def test_console_lan_ip_in_no_proxy(self) -> None:
        text = GROUP_VARS.read_text(encoding="utf-8")
        assert re.search(r"^console_lan_ip:", text, re.MULTILINE)
        no_proxy_line = next(
            line
            for line in text.splitlines()
            if line.startswith("mcp_kb_host_prepare__no_proxy")
        )
        assert "console_lan_ip" in no_proxy_line, (
            "LAN-адрес консоли обязан входить в NO_PROXY, иначе прокси отдаёт 403"
        )


# ── .env.example ─────────────────────────────────────────────────────────────


class TestEnvExample:
    def test_lan_vars_documented(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert re.search(r"^CONSOLE_LAN_IP=", text, re.MULTILINE)
        assert re.search(r"^CONSOLE_LAN_CIDR=", text, re.MULTILINE)
        assert "0.0.0.0 НЕДОПУСТИМ" in text


# ── функциональная валидация Caddyfile (docker, без сети) ────────────────────


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "image", "inspect", CADDY_IMAGE],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


class TestCaddyValidate:
    @pytest.mark.parametrize(
        ("lan_ip", "cidr"),
        [("192.168.2.3", "192.168.2.0/24"), ("10.1.2.3", "10.1.0.0/16")],
        ids=["home-lan", "office-lan"],
    )
    def test_config_is_valid(self, lan_ip: str, cidr: str) -> None:
        """Конфиг валиден для разных LAN-адресов (переносимость в офис)."""
        if not _docker_ready():
            pytest.skip(f"нет docker или локального образа {CADDY_IMAGE}")
        res = subprocess.run(
            [
                "docker", "run", "--rm",
                "-e", f"CONSOLE_LAN_IP={lan_ip}",
                "-e", f"CONSOLE_LAN_CIDR={cidr}",
                "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
                CADDY_IMAGE,
                "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
                "--adapter", "caddyfile",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert "Valid configuration" in res.stdout, f"{res.stdout}\n{res.stderr}"
