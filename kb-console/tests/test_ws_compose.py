"""Статические ассерты compose.workspace.yml (Ф2 5b) + unit фабрики ws-redis.

Инварианты: I12 (отдельный ws-redis, noeviction+AOF, pinned-образ, volume),
I6 (порты НЕ публикуются — internal-only), I11 (WS_REDIS_URL → ws-redis).
Фабрика — без реального подключения и без установленного пакета: ``redis``
подменяется стабом в ``sys.modules`` (контракт фабрики — ленивый импорт).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from kb_console.core.redis_client import (
    ENV_WS_REDIS_URL,
    get_ws_redis,
    make_ws_redis,
    reset_ws_redis,
    ws_redis_health,
)

COMPOSE = Path(__file__).resolve().parents[2] / "compose.workspace.yml"

WS_ENV_KEYS = (
    "WS_MCP_KEY=",
    "WS_MCP_IMPORT_KEY=",
    "CONSOLE_USERS_FILE=",
    "CONSOLE_ADMIN_USER=",
    "CONSOLE_ADMIN_PASSWORD=",
)


@pytest.fixture(scope="module")
def doc() -> Any:
    assert COMPOSE.exists(), f"нет {COMPOSE}"
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture(autouse=True)
def _clean_singleton() -> Any:
    reset_ws_redis()
    yield
    reset_ws_redis()


# ── I12: ws-redis ───────────────────────────────────────────────────


def test_ws_redis_service_exists(doc: Any) -> None:
    assert "ws-redis" in doc["services"]
    assert doc["services"]["ws-redis"]["image"].startswith("redis:")


def test_noeviction_and_aof(doc: Any) -> None:
    cmd = doc["services"]["ws-redis"]["command"]
    assert cmd[cmd.index("--appendonly") + 1] == "yes"
    assert cmd[cmd.index("--maxmemory-policy") + 1] == "noeviction"


def test_image_pinned_by_digest(doc: Any) -> None:
    """I12 pin: образ адресован digest'ом (не плавающим тегом)."""
    assert "@sha256:" in doc["services"]["ws-redis"]["image"]


def test_aof_named_volume(doc: Any) -> None:
    volumes = doc["services"]["ws-redis"]["volumes"]
    assert any(str(v).startswith("ws-redis-data:") for v in volumes)
    assert "ws-redis-data" in doc["volumes"]


# ── I6: internal-only ───────────────────────────────────────────────


@pytest.mark.parametrize("svc", ["ws-redis", "workspace"])
def test_no_published_ports(doc: Any, svc: str) -> None:
    assert "ports" not in doc["services"][svc], f"{svc}: публикация портов запрещена (I6)"


# ── workspace: env / depends_on / состав ────────────────────────────


def test_workspace_env(doc: Any) -> None:
    env = doc["services"]["workspace"]["environment"]
    assert "WS_REDIS_URL=redis://ws-redis:6379/0" in env
    for key in WS_ENV_KEYS:
        assert any(str(e).startswith(key) for e in env), f"нет {key}"


def test_workspace_env_endpoints(doc: Any) -> None:
    """Ф2 #2b-2b: WS_LLM_URL (litellm, та же сеть) / WS_MCP_URL (host-gateway)
    / LITELLM_MASTER_KEY — прокинуты в workspace."""
    env = doc["services"]["workspace"]["environment"]
    assert "WS_LLM_URL=http://litellm:4000/v1" in env
    assert "WS_MCP_URL=http://host.docker.internal:8000" in env
    assert any(str(e).startswith("LITELLM_MASTER_KEY=") for e in env)


def test_ws_mcp_import_key_wired(doc: Any) -> None:
    """Ф2 #6b: import-ключ вложений прокинут в workspace (fail-closed ``:-``)."""
    env = doc["services"]["workspace"]["environment"]
    assert "WS_MCP_IMPORT_KEY=${WS_MCP_IMPORT_KEY:-}" in env


def test_workspace_depends_on_ws_redis(doc: Any) -> None:
    assert "ws-redis" in doc["services"]["workspace"]["depends_on"]


def test_services_exactly_ws_redis_and_workspace(doc: Any) -> None:
    """Ф3+ (litellm/queue/admission) — НЕ в этом файле; здесь только 5b."""
    assert set(doc["services"]) == {"ws-redis", "workspace"}


# ── Ф2 #6b-2: проводка attach_upload на странице «Чат» ──────────────


def test_chat_page_mounts_attach_upload() -> None:
    """Структурный guard: chat.py монтирует build_attach_upload(role)."""
    chat_src = (
        Path(__file__).resolve().parents[1]
        / "src" / "kb_console" / "pages" / "chat.py"
    )
    assert chat_src.exists(), f"нет {chat_src}"
    assert "build_attach_upload(role)" in chat_src.read_text()


# ── фабрика (unit; mock, без реального подключения) ─────────────────


class _FakeClient:
    def __init__(self, url: str, kwargs: dict) -> None:
        self.url = url
        self.kwargs = kwargs
        self.ping_error: Exception | None = None

    def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> list:
    """Стаб ``redis`` в sys.modules; возвращает список созданных клиентов."""
    created: list = []
    mod = types.ModuleType("redis")

    class _Redis:
        @staticmethod
        def from_url(url: str, **kwargs: dict) -> _FakeClient:
            client = _FakeClient(url, kwargs)
            created.append(client)
            return client

    mod.Redis = _Redis
    monkeypatch.setitem(sys.modules, "redis", mod)
    return created


def test_make_ws_redis_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch)
    monkeypatch.setenv(ENV_WS_REDIS_URL, "redis://stub-ws:6379/1")
    client = make_ws_redis()
    assert client is created[0]
    assert client.url == "redis://stub-ws:6379/1"
    assert client.kwargs.get("decode_responses") is True


def test_make_ws_redis_explicit_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch)
    monkeypatch.setenv(ENV_WS_REDIS_URL, "redis://env:6379/0")
    client = make_ws_redis("redis://explicit:6379/2")
    assert client.url == "redis://explicit:6379/2"


def test_make_ws_redis_no_url_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch)
    monkeypatch.delenv(ENV_WS_REDIS_URL, raising=False)
    with pytest.raises(RuntimeError, match=ENV_WS_REDIS_URL):
        make_ws_redis()


def test_get_ws_redis_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch)
    monkeypatch.setenv(ENV_WS_REDIS_URL, "redis://stub-ws:6379/0")
    assert get_ws_redis() is get_ws_redis()
    assert len(created) == 1
    reset_ws_redis()
    assert get_ws_redis() is created[1]


def test_ws_redis_health_ok_vs_error() -> None:
    ok = _FakeClient("redis://x:6379/0", {})
    assert ws_redis_health(ok) == {"ok": True}
    down = _FakeClient("redis://x:6379/0", {})
    down.ping_error = ConnectionError("Connection refused (host:6379)")
    health = ws_redis_health(down)
    # без URL/паролей: только класс исключения, str(exc) не выносится
    assert health == {"ok": False, "error": "ConnectionError"}
