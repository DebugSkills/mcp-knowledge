"""Защитный тест: утечка инфраструктурных данных в открытый репо (038).

Сканирует ТРЕКНУТЫЕ (`git ls-files`) текстовые файлы и падает, если найдены:
- IPv4 ВНЕ приватных диапазонов (10/8, 172.16/12, 192.168/16, 127/8);
- логин `ch@`/`ladmin@`;
- порт прокси `:3128`.

Allowlist — только легитимные случаи: фейковые тестовые фикстуры, wildcard-адрес,
документированные плейсхолдеры (jump-логин `ch@jump`), и сам файл стража
(SELF_EXCLUDE — его собственные TestScanner-фикстуры, не реальная инфраструктура).
Реальное обнаружение (внешние IPv4, логины ch@/ladmin@, :3128) в остальных трекаемых
файлах НЕ ослабляется.
"""

import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]

# wildcard-адрес (bind на все интерфейсы) — не идентифицирует хост
SAFE_IPS = {"0.0.0.0"}
# GIN-логи теста — фиктивный клиентский IP 1.2.3.4 (не реальная инфраструктура)
IPV4_ALLOW = ("tests/test_errors_lib.py",)
# тесты используют фейковые фикстуры прокси (10.9.9.9:3128, proxy.local:3128)
PORT_3128_ALLOW = ("tests/",)
# Сам страж содержит фейковые данные в TestScanner-фикстурах (1.2.3.4, 8.8.8.8,
# ch@, ladmin@) — это его собственные юнит-тесты сканера, не реальная инфраструктура.
# Самореференсный скан давал бы ложные срабатывания на собственных тестовых данных.
SELF_EXCLUDE = ("tests/test_no_infra_leaks.py",)
# test_airgap_bundle_ship.py использует фейковый jump-логин `ch@jump` — документированный
# плейсхолдер `<jump-user>@<jump-host>` (docs/operations/airgap-first-install.private.md.example).
LOGIN_ALLOW = ("tests/test_airgap_bundle_ship.py",)

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LOGIN_RE = re.compile(r"\b(?:ch|ladmin)@")
PORT_RE = re.compile(r":3128\b")


def _is_private(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # не IP — не флаг
    return any(addr in net for net in PRIVATE_NETS)


def scan_text(text, path=""):
    """→ список нарушений в тексте (внешний IPv4 / логин / :3128)."""
    hits = []
    for m in IPV4_RE.finditer(text):
        ip = m.group(0)
        if ip in SAFE_IPS or _is_private(ip):
            continue
        if any(path.startswith(p) for p in IPV4_ALLOW):
            continue
        hits.append(f"{path}: внешний IPv4 {ip}")
    for m in LOGIN_RE.finditer(text):
        if any(path.startswith(p) for p in LOGIN_ALLOW):
            continue
        hits.append(f"{path}: логин {m.group(0)}")
    if not any(path.startswith(p) for p in PORT_3128_ALLOW):
        for m in PORT_RE.finditer(text):
            hits.append(f"{path}: :3128")
    return hits


def _tracked_files():
    r = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                       capture_output=True, check=False)
    return [p for p in r.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _read_text(rel):
    p = ROOT / rel
    try:
        if not p.is_file() or p.stat().st_size > 1_000_000:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8000]:
        return None  # бинарный
    return data.decode("utf-8", errors="replace")


class TestNoInfraLeaks:
    def test_tracked_files_clean(self):
        leaks = []
        for rel in _tracked_files():
            if rel in SELF_EXCLUDE:
                continue  # сам страж: его фикстуры — собственные юнит-тесты сканера
            text = _read_text(rel)
            if text is not None:
                leaks.extend(scan_text(text, rel))
        assert not leaks, "Утечки инфраструктуры:\n" + "\n".join(leaks)


class TestScanner:
    def test_detects_external_ip(self):
        assert scan_text("хост 8.8.8.8 тут", "x.md")

    def test_allows_private_and_safe_ips(self):
        hits = [h for h in scan_text(
            "10.0.0.1 172.17.0.5 192.168.1.1 127.0.0.1 0.0.0.0", "x.md")]
        assert hits == []

    def test_detects_login(self):
        assert scan_text("ch@host", "x.md")
        assert scan_text("ladmin@host", "x.md")

    def test_detects_proxy_port_except_tests(self):
        assert [h for h in scan_text("http://x:3128", "src/y.sh") if ":3128" in h]
        assert not [h for h in scan_text("http://10.9.9.9:3128", "tests/z.py")]

    def test_ipv4_allowlist_path(self):
        assert not [h for h in scan_text("1.2.3.4", "tests/test_errors_lib.py")
                    if "IPv4" in h]
