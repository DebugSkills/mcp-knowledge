"""Статический ingress-гейт: internal-only сервисы, loopback-only публикации.

trace_id: arch-2026-10-05-ai-workspace — Ф2 шаг #4 (MCP internal-only + ingress-тест).

Гейт читает только compose-файлы репо (repo root = родитель kb-console/):
  * compose.gateway.yml   — litellm internal-only (порт 4000 НЕ публикуется, I1/I6);
  * compose.workspace.yml — workspace / ws-redis internal-only (I6);
  * docker-compose.yml    — каждый опубликованный порт ТОЛЬКО на 127.0.0.1 хоста;
  * mcp-server            — network_mode: host = ИЗВЕСТНОЕ исключение
    WS-MCP-HOSTNET (отслеживается в .boardData.md §10): host-сеть нужна, чтобы
    видеть ollama/converter на loopback хоста. Исключение закреплено ЯВНЫМ
    комментарием-маркером в блоке сервиса, чтобы его нельзя было «потерять».

Любая правка compose-файлов в обход гейта (публикация порта наружу, 0.0.0.0-
маппинг, снятие комментария-маркера host-net) обязана ломать этот тест.
Живой (runtime) аналог — scripts/ingress_probe.sh.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

COMPOSE_MAIN = REPO_ROOT / "docker-compose.yml"
COMPOSE_GATEWAY = REPO_ROOT / "compose.gateway.yml"
COMPOSE_WORKSPACE = REPO_ROOT / "compose.workspace.yml"

MCP_SERVICE = "mcp-server"

# Комментарий-маркер host-net исключения (WS-MCP-HOSTNET, §10) в блоке mcp-server.
HOSTNET_MARKER_RE = re.compile(r"#.*(host-сеть|host-net|hostnet)", re.IGNORECASE)

PortMapping = str | int | dict


def _load_compose(path: Path) -> dict:
    """Загрузить compose-файл; молчаливых skip нет — нет файла = явный fail."""
    if not path.is_file():
        pytest.fail(f"compose-файл не найден: {path} (гейт не работает вслепую)")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _get_services(compose: dict) -> dict:
    services = compose.get("services") or {}
    if not services:
        pytest.fail(f"в compose-файле нет секции services: {compose}")
    return services


def mapping_is_loopback(mapping: PortMapping) -> tuple[bool, str]:
    """Проверить, что маппинг публикует порт ТОЛЬКО на loopback хоста.

    Принимает short-строку ("127.0.0.1:6333:6333", "127.0.0.1:6333"),
    long-словарь ({"target": 80, "published": "127.0.0.1:8080"}) и любые
    мусорные значения. Fail-closed: всё, что НЕ явный "127.0.0.1:..." —
    НЕ loopback (0.0.0.0, голый "HOST:CONT", int, [::], long без published).

    Returns:
        (ok, reason): ok=False сопровождается человекочитаемой причиной.
    """
    if isinstance(mapping, str):
        if mapping.startswith("127.0.0.1:"):
            return True, ""
        return False, f"не loopback-маппинг: {mapping!r} (ожидается '127.0.0.1:...')"
    if isinstance(mapping, dict):
        published = mapping.get("published")
        if published is None:
            return False, (
                f"long-syntax без published — неявная публикация: {mapping!r}"
            )
        return mapping_is_loopback(str(published))
    return False, f"неожидаемый тип маппинга: {mapping!r}"


def test_litellm_gateway_has_no_published_ports() -> None:
    """compose.gateway.yml: LiteLLM internal-only — ключ ports отсутствует/пуст."""
    services = _get_services(_load_compose(COMPOSE_GATEWAY))
    svc = services.get("litellm")
    assert isinstance(svc, dict), "сервис litellm не найден в compose.gateway.yml"
    assert svc.get("ports") in (None, []), (
        "LiteLLM не должен публиковать порты (I1/I6): "
        f"ports={svc.get('ports')!r}"
    )


def test_workspace_services_internal_only() -> None:
    """compose.workspace.yml: workspace и ws-redis — без published ports (I6)."""
    services = _get_services(_load_compose(COMPOSE_WORKSPACE))
    for name in ("workspace", "ws-redis"):
        svc = services.get(name)
        assert isinstance(svc, dict), f"сервис {name} не найден в compose.workspace.yml"
        assert svc.get("ports") in (None, []), (
            f"{name}: порты публиковаться не должны (I6): ports={svc.get('ports')!r}"
        )


def test_main_compose_published_ports_loopback_only() -> None:
    """docker-compose.yml: каждый published-маппинг начинается с '127.0.0.1:'."""
    services = _get_services(_load_compose(COMPOSE_MAIN))
    violations: list[str] = []
    for name, svc in services.items():
        ports = (svc or {}).get("ports")
        if not ports:
            continue
        for mapping in ports:
            ok, reason = mapping_is_loopback(mapping)
            if not ok:
                violations.append(f"{name}: {reason}")
    assert not violations, (
        "наружу можно публиковать только на loopback хоста:\n  "
        + "\n  ".join(violations)
    )


def test_mcp_server_host_network_is_explicit_tracked_exception() -> None:
    """mcp-server: network_mode=host И явный комментарий-маркер исключения.

    Исключение WS-MCP-HOSTNET (§10): host-сеть нужна для доступа к
    loopback-портам ollama/converter. Тест гарантирует, что исключение
    нельзя «потерять» молча: убрал network_mode ИЛИ убрал комментарий-маркер
    в блоке сервиса — гейт падает.
    """
    services = _get_services(_load_compose(COMPOSE_MAIN))
    svc = services.get(MCP_SERVICE)
    assert isinstance(svc, dict), f"сервис {MCP_SERVICE} не найден в docker-compose.yml"
    assert svc.get("network_mode") == "host", (
        f"{MCP_SERVICE}: network_mode={svc.get('network_mode')!r}, ожидается 'host' "
        "(изменился контракт WS-MCP-HOSTNET — обнови §10 и этот тест осознанно)"
    )

    lines = COMPOSE_MAIN.read_text(encoding="utf-8").splitlines()
    start = None
    for idx, line in enumerate(lines):
        if re.match(rf"^  {re.escape(MCP_SERVICE)}:\s*$", line):
            start = idx
            break
    assert start is not None, f"не найден заголовок сервиса {MCP_SERVICE} в тексте файла"
    block: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^  [\w.-]+:\s*$", line):  # заголовок следующего сервиса
            break
        block.append(line)
    assert any(HOSTNET_MARKER_RE.search(l) for l in block), (
        f"в блоке {MCP_SERVICE} нет явного комментария-маркера host-net "
        "(WS-MCP-HOSTNET, §10) — исключение должно быть видимым в самом файле"
    )


def test_mapping_checker_rejects_non_loopback_mutants() -> None:
    """Зубы парсера: кейс-мутант 0.0.0.0 и голые порты обязаны отвергаться."""
    # Мутант-фикстура: внешняя публикация workspace-порта (то, что защищаем).
    mutant_wildcard = "0.0.0.0:8085:8085"
    ok, reason = mapping_is_loopback(mutant_wildcard)
    assert not ok, f"мутант {mutant_wildcard!r} ошибочно принят"
    assert "0.0.0.0" in reason

    # Голый HOST:CONT (docker публикует на 0.0.0.0) — тоже отказ.
    ok, _ = mapping_is_loopback("8085:8085")
    assert not ok, "голый '8085:8085' (=> 0.0.0.0) ошибочно принят"

    # int-порт и long-syntax без published — отказ (fail-closed).
    assert not mapping_is_loopback(8085)[0]
    assert not mapping_is_loopback({"target": 80, "protocol": "tcp"})[0]

    # Эталонные loopback-формы принимаются.
    assert mapping_is_loopback("127.0.0.1:8085:8085")[0]
    assert mapping_is_loopback("127.0.0.1:8085")[0]
    assert mapping_is_loopback({"target": 80, "published": "127.0.0.1:8080"})[0]
