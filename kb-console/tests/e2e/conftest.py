"""e2e-инфраструктура kb-console (Ф6, arch-2026-10-10-ai-ws-acceptance).

Playwright chromium headless. Два контура:
  * внешний — env KB_CONSOLE_URL (например, живой dev-стек :8085);
  * self (дефолт) — conftest сам поднимает kb-console (python -m
    kb_console.app) на 127.0.0.1:ephemeral в legacy-режиме
    (CONSOLE_AUTH=required + тестовый НЕсекретный пароль): S0 не зависит
    от per-user стора внешнего контура и от состояния живого стека.

Запуск — `make console-e2e`: цель извлекает из ./.env только нужные ключи
(CONSOLE_PASSWORD / KB_CONSOLE_E2E_USER / KB_CONSOLE_E2E_PASSWORD; полный
`source .env` невозможен — в файле есть многострочные значения; секрет
раннером не печатается) и ставит KB_CONSOLE_E2E=1 — без него e2e скипается
при случайной коллекции чужим прогоном (console-test/preflight G3
исключают e2e маркером -m 'not e2e' в kb-console/pyproject.toml).

Контракт входа — из src/kb_console/login_page.py (реальный DOM, не выдумка):
  GET /login — статический HTML: форма #lf, поле #f-pass (legacy-режим без
  #f-user), submit «Войти»; JS шлёт POST /api/login {username, password};
  успех → window.location = sanitize_next(next) | "/status";
  POST /api/logout → {"ok": True} + очистка session-cookie (stateless signed).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import ConsoleMessage, Page, Request

# Пароль self-инстанса — НЕ секрет: слушает только 127.0.0.1:ephemeral,
# живёт на время e2e-сессии (для внешнего контура используйте
# KB_CONSOLE_E2E_PASSWORD / CONSOLE_PASSWORD из env раннера).
_SELF_PASSWORD = "e2e-smoke-pass"

# Прокси-гигиена (по мотивам tests/conftest.py::_local_http): цель — localhost.
os.environ["NO_PROXY"] = ",".join(
    dict.fromkeys(
        [*os.environ.get("NO_PROXY", "").split(","), "127.0.0.1", "localhost"]
    )
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Случайная коллекция e2e без штатного раннера → skip (не падать).

    ВНИМАНИЕ: хук получает ВСЮ коллекцию сессии (область subdir-conftest его
    НЕ ограничивает) — фильтруем только items из этого e2e-каталога.
    Штатный прогон (`make console-e2e`) ставит KB_CONSOLE_E2E=1. Отсутствие
    кредов в штатном прогоне — fail-loud внутри login().
    """
    if os.environ.get("KB_CONSOLE_E2E") != "1":
        skip = pytest.mark.skip(
            reason="e2e kb-console: запускайте через `make console-e2e` "
            "(живой стек + CONSOLE_PASSWORD из ./.env)"
        )
        e2e_dir = Path(__file__).resolve().parent
        for item in items:
            path = getattr(item, "path", None)
            if path is not None and e2e_dir in Path(path).resolve().parents:
                item.add_marker(skip)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str | None]:
    """Self-инстанс kb-console (legacy auth) — если не задан KB_CONSOLE_URL.

    Отдельный процесс `python -m kb_console.app` (CMD из kb-console/Dockerfile):
    CONSOLE_AUTH=required, тестовый пароль, пустой users-стор → legacy-ветка
    входа (контракт src/kb_console/login_page.py). Лог — .trash/ (throwaway).
    """
    if os.environ.get("KB_CONSOLE_URL"):
        yield None
        return

    port = _free_port()
    log_path = Path(".trash") / (
        "e2e-console-server-" + time.strftime("%Y%m%dT%H%M%S") + f"-{port}.log"
    )
    log_path.parent.mkdir(exist_ok=True)
    log = log_path.open("w")
    # PYTEST_*/NICEGUI_* из pytest-окружения ломают ui.run в подпроцессе
    # (helpers.is_pytest() по PYTEST_VERSION → требует NICEGUI_SCREEN_TEST_PORT;
    # nicegui screen-плагин по NICEGUI_SCREEN_TEST) — вычищаем.
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("NICEGUI", "PYTEST"))
    }
    env.update(
        {
            "CONSOLE_AUTH": "required",
            "CONSOLE_PASSWORD": _SELF_PASSWORD,
            "CONSOLE_HOST": "127.0.0.1",
            "CONSOLE_PORT": str(port),
            # Несуществующий users-файл → пустой стор → legacy-режим входа.
            "CONSOLE_USERS_FILE": str(
                tmp_path_factory.mktemp("e2e-console") / "users.jsonl"
            ),
            # Дефолты путей — контейнерные (/app/data/console): уводим в tmp.
            "CONSOLE_ACCESS_REQUESTS_DB": str(
                tmp_path_factory.mktemp("e2e-console-req") / "access_requests.db"
            ),
            # S2 (Ф6-2): детерминизм fail-случая очереди — случайный WS_REDIS_URL
            # из окружения раннера не должен включать очередь в legacy-контур
            # (пустая строка → make_ws_redis RuntimeError → fail-soft баннер).
            "WS_REDIS_URL": "",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_console.app"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    try:
        import httpx

        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                if (
                    httpx.get(f"{url}/login", timeout=1.0, trust_env=False).status_code
                    == 200
                ):
                    break
            except Exception:  # noqa: BLE001 — ждём старт сервера
                time.sleep(0.25)
        else:
            pytest.fail("self-инстанс kb-console не поднялся за 30s", pytrace=False)
        if proc.poll() is not None:
            tail = log_path.read_text()[-2000:]
            pytest.fail(
                f"self-инстанс kb-console упал (rc={proc.returncode}); "
                f"хвост лога {log_path}:\n{tail}",
                pytrace=False,
            )
        # Креды self-инстанса — через тот же контракт env, что и внешний контур.
        os.environ["KB_CONSOLE_E2E_PASSWORD"] = _SELF_PASSWORD
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="session")
def base_url(e2e_server: str | None) -> str:
    """База kb-console: env KB_CONSOLE_URL (внешний контур) или self-инстанс."""
    url = os.environ.get("KB_CONSOLE_URL") or e2e_server
    assert url, "ни KB_CONSOLE_URL, ни self-инстанс недоступны"
    return url.rstrip("/")


@pytest.fixture(autouse=True)
def _default_timeouts(page: Page) -> None:
    """Дефолтные PW-таймауты e2e: действие 10s, навигация 15s."""
    page.set_default_timeout(10_000)
    page.set_default_navigation_timeout(15_000)


@dataclass(frozen=True)
class BrowserError:
    kind: str  # "console" | "pageerror" | "requestfailed"
    detail: str


class ConsoleErrors:
    """Сборщик ошибок браузера: console(type=error) + pageerror + requestfailed.

    Фикстура console_errors падает в teardown при непустом списке — каждый
    e2e-сценарий по умолчанию требует 0 ошибок консоли. Positive-control
    (test_detector_control.py) инъектирует ошибки и снимает их через
    mark_expected(): доказывает, что детектор не вакуумный и не инертный.
    """

    def __init__(self, page: Page) -> None:
        self.errors: list[BrowserError] = []
        self._expected: list[BrowserError] = []
        self._page = page
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("requestfailed", self._on_requestfailed)

    def _on_console(self, msg: ConsoleMessage) -> None:
        if msg.type == "error":
            self.errors.append(BrowserError("console", msg.text))

    def _on_pageerror(self, exc: BaseException) -> None:
        self.errors.append(BrowserError("pageerror", str(exc)))

    def _on_requestfailed(self, request: Request) -> None:
        self.errors.append(
            BrowserError(
                "requestfailed",
                f"{request.method} {request.url} — {request.failure}",
            )
        )

    def wait_for(self, minimum: int = 1, timeout: float = 5.0) -> None:
        """Ждать >= minimum пойманных ошибок (для positive-control)."""
        deadline = time.monotonic() + timeout
        while len(self.errors) < minimum:
            if time.monotonic() > deadline:
                pytest.fail(
                    f"детектор не поймал {minimum} ошибок за {timeout}s; "
                    f"поймано: {self.summary()}",
                    pytrace=False,
                )
            self._page.wait_for_timeout(50)

    def mark_expected(self) -> None:
        """Снять ожидаемые ошибки (positive-control): teardown остаётся зелёным."""
        self._expected.extend(self.errors)
        self.errors.clear()

    def summary(self) -> str:
        if not self.errors:
            return "(пусто — 0 ошибок)"
        return "\n".join(f"[{e.kind}] {e.detail}" for e in self.errors)


@pytest.fixture
def console_errors(page: Page) -> Iterator[ConsoleErrors]:
    """Слушать ошибки браузера весь тест; упасть в teardown, если поймали."""
    watcher = ConsoleErrors(page)
    yield watcher
    if watcher.errors:
        pytest.fail(
            f"ошибки браузера за тест ({len(watcher.errors)}):\n{watcher.summary()}",
            pytrace=False,
        )


def _form_login(page: Page, target: str, username: str, password: str) -> str:
    """Ядро S0-хелпера login(): реальная форма /login (контракт login_page.py).

    username="" допустим только для legacy-контура (нет поля #f-user);
    per-role обёртки (console_as_admin/_contributor, Ф6-1) передают явные
    креды сеянного users-стора.
    """
    page.goto(f"{target}/login")
    page.wait_for_url("**/login**")
    user = page.locator("#f-user")
    if user.count():  # per-user контур: нужен явный логин (legacy-пароль
        # отвергается interlock'ом при непустом users-сторе)
        if not username:
            pytest.fail(
                "контур per-user (на /login есть поле логина), но "
                "username не передан — legacy-вход невозможен",
                pytrace=False,
            )
        user.fill(username)
    page.fill("#f-pass", password)
    page.locator("#lf button[type=submit]").click()
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=15_000)
    except Exception:  # noqa: BLE001 — ошибку ожидания переводим в fail с контекстом
        err = page.locator("#err").inner_text(timeout=1_000)
        pytest.fail(
            f"login: не ушли с /login (url={page.url}; #err={err!r}) — "
            "неверные креды или rate-limit /api/login",
            pytrace=False,
        )
    return page.url


@pytest.fixture(scope="session")
def login(base_url: str) -> Callable[..., str]:
    """Helper: логин реальной формой /login (контракт login_page.py).

    Fail-loud при отсутствующем CONSOLE_PASSWORD (skip запрещён заданием Ф6).
    Возвращает URL после входа — ожидаем аутентифицированную страницу.
    """

    def _login(page: Page, url: str | None = None) -> str:
        target = (url or base_url).rstrip("/")
        password = os.environ.get("KB_CONSOLE_E2E_PASSWORD", "") or os.environ.get(
            "CONSOLE_PASSWORD", ""
        )
        if not password:
            pytest.fail(
                "нет кредов: задайте CONSOLE_PASSWORD (legacy) или "
                "KB_CONSOLE_E2E_PASSWORD (+ KB_CONSOLE_E2E_USER для per-user); "
                "`make console-e2e` извлекает их из ./.env без печати",
                pytrace=False,
            )
        return _form_login(
            page, target, os.environ.get("KB_CONSOLE_E2E_USER", ""), password
        )

    return _login


# ── Per-user контур (Ф6-1, S3): роли admin/contributor ──────────

#: Креды сеянного users-стора — НЕ секреты: контур 127.0.0.1:ephemeral,
#: живёт на время e2e-сессии (пароли идут и в leak-ассерт S3).
E2E_USERS: dict[str, tuple[str, str]] = {
    "admin": ("e2e-admin", "e2e-admin-pass"),
    "contributor": ("e2e-contrib", "e2e-contrib-pass"),
}

#: Живой admin-API калибровки (host-сервис :8700, заголовок X-Calib-Key;
#: ключ — CALIB_API_KEY из окружения раннера, make-цель извлекает из ./.env
#: точечно, не печатая). Страница /calibration — тонкий клиент; недоступность
#: API = fail-soft ветки на карточках, сценарий не падает.
E2E_CALIB_API_URL = "http://127.0.0.1:8700"


#: Живой ws-redis dev-контура — бэкенд страницы «Очередь» (S2, Ф6-2).
#: Публикация 127.0.0.1:6390 — test-only overlay compose.workspace.test.yml
#: («make ws-up-test»); прецедент WS_TEST_REDIS_URL в корневом Makefile и
#: ai_workspace/tests/conftest.py. Недоступность → сценарии живой очереди
#: skip'ятся (fixture ws_queue), страница деградирует в fail-soft баннер.
E2E_WS_REDIS_URL = "redis://127.0.0.1:6390/0"


@dataclass(frozen=True)
class ConsoleRoleSession:
    """Аутентифицированная сессия per-user self-инстанса (роль + креды)."""

    url: str
    page: Page
    username: str
    password: str  # тестовый НЕсекретный пароль (leak-ассерт S3)
    role: str


@pytest.fixture(scope="session")
def e2e_users_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Self-инстанс kb-console в per-user режиме (Ф6-1, вариант A).

    Всегда собственный инстанс (KB_CONSOLE_URL игнорируется — внешнему
    контуру тестовых юзеров не создаём): users-стор сеем сами через механику
    core/users.py (UserStore.create_user, как прод-бутстрап bootstrap_from_env,
    но + contributor). CALIB_API_URL=:8700 (живой admin-API калибровки),
    CALIB_API_KEY — из окружения раннера (пуст → 401 → fail-soft, не падение).
    """
    from kb_console.core.users import UserStore

    tmp = tmp_path_factory.mktemp("e2e-console-users")
    users_file = tmp / "users.jsonl"
    store = UserStore(users_file=str(users_file))
    for role, (username, password) in E2E_USERS.items():
        store.create_user(username, password, role, note="e2e Ф6-1 S3")
    # fail-closed sanity: пустой стор молча уводит контур в legacy-режим
    assert store.has_users(), "users-стор пуст: per-user контур не поднялся"

    port = _free_port()
    log_path = Path(".trash") / (
        "e2e-console-users-" + time.strftime("%Y%m%dT%H%M%S") + f"-{port}.log"
    )
    log_path.parent.mkdir(exist_ok=True)
    log = log_path.open("w")
    # PYTEST_*/NICEGUI_* из pytest-окружения ломают ui.run в подпроцессе —
    # вычищаем (наследуем механику e2e_server).
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("NICEGUI", "PYTEST"))
    }
    env.update(
        {
            # users_present → auth "on" без CONSOLE_PASSWORD (матрица auth.py)
            "CONSOLE_AUTH": "required",
            "CONSOLE_HOST": "127.0.0.1",
            "CONSOLE_PORT": str(port),
            "CONSOLE_USERS_FILE": str(users_file),
            "CONSOLE_ACCESS_REQUESTS_DB": str(tmp / "access_requests.db"),
            # S3: данные карточек — с живого admin-API калибровки (fail-soft)
            "CALIB_API_URL": E2E_CALIB_API_URL,
            "CALIB_API_KEY": os.environ.get("CALIB_API_KEY", ""),
            # S2: живой ws-redis ws-контура — бэкенд /queue (pages/queue.py читает
            # redis напрямую, НЕ через MCP); недоступен → fail-soft баннер,
            # S3-сценарии калибровки не затрагиваются
            "WS_REDIS_URL": E2E_WS_REDIS_URL,
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_console.app"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    try:
        import httpx

        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                resp = httpx.get(f"{url}/login", timeout=1.0, trust_env=False)
                # per-user контракт: поле #f-user есть ⇔ стор непуст (login_page)
                if resp.status_code == 200 and "f-user" in resp.text:
                    ready = True
                    break
            except Exception:  # noqa: BLE001, S110 — ждём старт сервера
                pass
            time.sleep(0.25)
        if not ready or proc.poll() is not None:
            tail = log_path.read_text()[-2000:]
            pytest.fail(
                f"per-user self-инстанс kb-console не поднялся за 30s "
                f"(rc={proc.returncode}); хвост лога {log_path}:\n{tail}",
                pytrace=False,
            )
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _role_session(page: Page, url: str, role: str) -> ConsoleRoleSession:
    """Войти в per-user контур ролью (переиспользование S0-механики логина)."""
    username, password = E2E_USERS[role]
    _form_login(page, url, username, password)
    return ConsoleRoleSession(url, page, username, password, role)


@pytest.fixture
def console_as_admin(page: Page, e2e_users_server: str) -> ConsoleRoleSession:
    """S3: сессия admin в per-user self-инстансе (карточки/история калибровки)."""
    return _role_session(page, e2e_users_server, "admin")


@pytest.fixture
def console_as_contributor(page: Page, e2e_users_server: str) -> ConsoleRoleSession:
    """S3-негатив «роль»: сессия contributor (уровень ниже admin)."""
    return _role_session(page, e2e_users_server, "contributor")


# ── S2 (Ф6-2): очередь ws-контура — живой backend + тест-задача ──────────

E2E_WS_TEST_PREFIX = "e2e-s2-"
"""Префикс тест-сущностей S2 (call/job/prio): уборка — ТОЛЬКО свои ключи
(прецедент job_store, ai_workspace/tests/conftest.py)."""


@dataclass(frozen=True)
class WsQueueTestTask:
    """Посеянная тест-задача: shelf + уникальные call/job (помечены e2e-s2-)."""

    shelf: str
    call: str
    job: str


class WsQueueBackend:
    """Клиент ws-redis для S2: посев тест-задачи, фиксация бэкенд-ответа,
    уборка только своих ключей.

    Контракт ключей — pages/queue.py (SSOT-дубль спека Scheduler §2):
    ``ws:q:{shelf}`` ZSET call→vft; ``ws:call:{shelf}:{call}`` HASH;
    ``ws:pos:{call}`` int; ``ws:prio:{job}`` STRING — override приоритета
    (UI-мутация «Применить» → SET EX 24 ч). Общие ключи не трогаем: из
    ZSET удаляется только свой member (ZREM); стрим ws:quota:events получает
    одно append-only событие job_priority_set от UI-мутации (maxlen 10k,
    помечено e2e-*) — это наблюдаемый контракт, не разрушение.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self._tasks: list[WsQueueTestTask] = []

    def _sweep_leftovers(self) -> None:
        """Убрать остатки упавших прогонов: e2e-s2-* ключи + члены ZSET."""
        for shelf in ("local", "ext", "gpu"):
            members = [
                m
                for m in self.client.zrange(f"ws:q:{shelf}", 0, -1)
                if m.startswith(E2E_WS_TEST_PREFIX)
            ]
            if members:
                self.client.zrem(f"ws:q:{shelf}", *members)
        stale = list(self.client.scan_iter(match=f"ws:pos:{E2E_WS_TEST_PREFIX}*"))
        stale += list(self.client.scan_iter(match=f"ws:prio:{E2E_WS_TEST_PREFIX}*"))
        for shelf in ("local", "ext", "gpu"):
            stale += list(
                self.client.scan_iter(match=f"ws:call:{shelf}:{E2E_WS_TEST_PREFIX}*")
            )
        if stale:
            self.client.delete(*stale)

    def seed_test_task(self, *, shelf: str = "local") -> WsQueueTestTask:
        """Посеять ОДНУ безопасную тест-задачу (мутация очереди, задание S2).

        Уникальные call/job c префиксом e2e-s2- (+unix-ts) не collide'ят с
        реальными; потребитель ws-контура в dev не запущен (стек
        compose.workspace — только ws-redis), ключи убираются в teardown.
        Возврат позиции 1 → строка видна в таблице полки.
        """
        ts = str(time.time()).replace(".", "-")
        call = f"{E2E_WS_TEST_PREFIX}call-{ts}"
        job = f"{E2E_WS_TEST_PREFIX}job-{ts}"
        vft = time.time() + 300.0
        self.client.zadd(f"ws:q:{shelf}", {call: vft})
        self.client.hset(
            f"ws:call:{shelf}:{call}",
            mapping={
                "job": job,
                "prio": "med",
                "class": "chat",
                "epoch": str(int(time.time())),
                "attempt": "0",
                "vft": str(vft),
            },
        )
        self.client.set(f"ws:pos:{call}", 1)
        task = WsQueueTestTask(shelf=shelf, call=call, job=job)
        self._tasks.append(task)
        return task

    def get_prio_override(self, task: WsQueueTestTask) -> str | None:
        """Бэкенд-ответ UI-мутации: GET ws:prio:{job} (None | high|med|low)."""
        return self.client.get(f"ws:prio:{task.job}")

    def prio_override_ttl(self, task: WsQueueTestTask) -> int:
        """TTL override (SET EX 24 ч — SSOT prio.PRIO_OVERRIDE_TTL_S)."""
        return int(self.client.ttl(f"ws:prio:{task.job}"))

    def _cleanup_all(self) -> None:
        for task in self._tasks:
            self.client.zrem(f"ws:q:{task.shelf}", task.call)
            self.client.delete(
                f"ws:call:{task.shelf}:{task.call}",
                f"ws:pos:{task.call}",
                f"ws:prio:{task.job}",
            )
        self._sweep_leftovers()


@pytest.fixture
def ws_queue() -> Iterator[WsQueueBackend]:
    """S2: клиент живого ws-redis + гарантированная уборка своих ключей.

    Недоступность ws-redis → явный skip с причиной (прецедент requires_redis,
    ai_workspace/tests/conftest.py): страница при этом живёт в fail-soft —
    отдельный сценарий на legacy-инстансе без WS_REDIS_URL.
    """
    import redis as redis_lib

    try:
        # decode_responses=True — контракт make_ws_redis (строки, не bytes)
        client = redis_lib.Redis.from_url(
            E2E_WS_REDIS_URL,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 — любая ошибка = skip с причиной
        pytest.skip(
            f"ws-redis недоступен по {E2E_WS_REDIS_URL} "
            f"({type(exc).__name__}) — make ws-up-test"
        )
    backend = WsQueueBackend(client)
    backend._sweep_leftovers()
    try:
        yield backend
    finally:
        try:
            backend._cleanup_all()
        except Exception:  # noqa: BLE001, S110 — teardown не должен ронять прогон
            pass


# ── S1 (Ф6-5, arch-2026-10-10-ai-ws-acceptance): чат — живые MCP+LLM ──────────

E2E_MCP_URL = "http://127.0.0.1:8000"
"""Живой MCP-сервер dev-контура (host-порт 8000): /health=200; /metrics
несёт счётчик mcp_tool_requests_total{tool=…} — СЕРВЕРНОЕ доказательство
того, что tool-loop чата реально ходил в search_knowledge (не по тексту
ответа: 7B-модель могла бы «цитировать» и без вызова)."""

E2E_LITELLM_CONTAINER = "mcp-knowledge-litellm"
"""Контейнер LiteLLM-шлюза (compose.gateway.yml): host-порта НЕТ —
доступ только по контейнерному IP (резолвим docker inspect'ом)."""

E2E_METRIC_SEARCH_LINE = 'mcp_tool_requests_total{'
"""Префикс строк счётчика вызовов tools в /metrics (metrics.py Фаза 12)."""


def _litellm_base_url() -> str:
    """Базовый URL LiteLLM для e2e: ``http://<container-IP>:4000/v1``.

    IP нестабилен между рестартами стека → резолв на старте сессии.
    Docker недоступен / контейнер не найден → "" (вызывающий скипает).
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                E2E_LITELLM_CONTAINER,
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return f"http://{ip}:4000/v1" if ip else ""


@dataclass(frozen=True)
class ChatBackends:
    """Живые бэкенды чата S1: MCP (поиск) + LiteLLM (local=qwen2.5:7b).

    Ключи хранятся, но НИКОГДА не печатаются (маскируются в repr через
    dataclass-поле с __repr__-обрезкой не делаем — просто не логируем).
    """

    mcp_url: str
    mcp_key: str
    llm_url: str
    llm_key: str

    def search_knowledge_calls(self) -> int:
        """Сумма mcp_tool_requests_total{tool="search_knowledge"} (все статусы).

        Серия отсутствует (нет вызовов с рестарта сервера) → 0. Ошибка
        /metrics — pytest.fail: это доказательный канал S1, молчать нельзя.
        """
        import httpx

        try:
            resp = httpx.get(f"{self.mcp_url}/metrics", timeout=5.0, trust_env=False)
        except httpx.HTTPError as exc:
            pytest.fail(f"/metrics недоступен ({type(exc).__name__})", pytrace=False)
        assert resp.status_code == 200, f"/metrics -> {resp.status_code}"
        total = 0
        for line in resp.text.splitlines():
            if not line.startswith(E2E_METRIC_SEARCH_LINE):
                continue
            if 'tool="search_knowledge"' not in line:
                continue
            try:
                total += int(float(line.rsplit(" ", 1)[1]))
            except (IndexError, ValueError):
                continue
        return total


@pytest.fixture(scope="session")
def e2e_chat_backends() -> ChatBackends:
    """S1: живые бэкенды чата (MCP :8000 + LiteLLM-контейнер) или явный skip.

    Ключи — ТОЛЬКО из окружения раннера (``make console-e2e`` извлекает
    MCP_API_KEY/LITELLM_MASTER_KEY из ./.env точечно, без печати). Пробы
    read-only: /health MCP, /models LiteLLM. Недоступность живых сервисов —
    skip с причиной (прецедент ws_queue), не падание прогона.
    """
    import httpx

    mcp_key = os.environ.get("MCP_API_KEY", "")
    llm_key = os.environ.get("LITELLM_MASTER_KEY", "")
    missing = [k for k, v in (("MCP_API_KEY", mcp_key), ("LITELLM_MASTER_KEY", llm_key)) if not v]
    if missing:
        pytest.skip(
            f"нет ключей бэкендов чата: {', '.join(missing)} "
            "(make console-e2e извлекает из ./.env)",
        )
    mcp_err = ""
    try:
        mcp_ok = (
            httpx.get(f"{E2E_MCP_URL}/health", timeout=5.0, trust_env=False).status_code
            == 200
        )
    except httpx.HTTPError as exc:
        mcp_ok = False
        mcp_err = type(exc).__name__
    if not mcp_ok:
        detail = f" ({mcp_err})" if mcp_err else ""
        pytest.skip(f"MCP-сервер недоступен {E2E_MCP_URL}{detail} — стек не поднят")
    llm_url = _litellm_base_url()
    if not llm_url:
        pytest.skip(
            f"LiteLLM-контейнер {E2E_LITELLM_CONTAINER} не найден "
            "(docker inspect) — make gateway-up"
        )
    try:
        llm_ok = (
            httpx.get(
                f"{llm_url}/models",
                headers={"Authorization": f"Bearer {llm_key}"},
                timeout=5.0,
                trust_env=False,
            ).status_code
            == 200
        )
    except httpx.HTTPError:
        llm_ok = False
    if not llm_ok:
        pytest.skip(f"LiteLLM недоступен {llm_url} — make gateway-up")
    return ChatBackends(mcp_url=E2E_MCP_URL, mcp_key=mcp_key, llm_url=llm_url, llm_key=llm_key)


@pytest.fixture(scope="session")
def e2e_chat_server(
    tmp_path_factory: pytest.TempPathFactory, e2e_chat_backends: ChatBackends
) -> Iterator[str]:
    """Self-инстанс kb-console (per-user) с ЖИВЫМИ бэкендами чата (S1).

    Отличия от e2e_users_server: WS_MCP_URL/WS_MCP_KEY + WS_LLM_URL/
    LITELLM_MASTER_KEY (tool-loop ходит в реальный MCP и реальный LiteLLM
    local=qwen2.5:7b ⇒ 0₽); WS_REDIS_URL="" — персист диалогов ОТКЛЮЧЕН
    (store=None → fail-soft по контракту): тестовые сессии не должны
    попадать в живой ws-redis dev-контура. Ключи передаются только через
    env процесса, не печатаются (см. e2e_chat_backends).
    """
    from kb_console.core.users import UserStore

    tmp = tmp_path_factory.mktemp("e2e-console-chat")
    users_file = tmp / "users.jsonl"
    store = UserStore(users_file=str(users_file))
    for role, (username, password) in E2E_USERS.items():
        store.create_user(username, password, role, note="e2e Ф6-5 S1")
    assert store.has_users(), "users-стор пуст: per-user контур чата не поднялся"

    port = _free_port()
    log_path = Path(".trash") / (
        "e2e-console-chat-" + time.strftime("%Y%m%dT%H%M%S") + f"-{port}.log"
    )
    log_path.parent.mkdir(exist_ok=True)
    log = log_path.open("w")
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("NICEGUI", "PYTEST"))
    }
    llm_host = e2e_chat_backends.llm_url.removeprefix("http://").split(":")[0]
    env.update(
        {
            "CONSOLE_AUTH": "required",
            "CONSOLE_HOST": "127.0.0.1",
            "CONSOLE_PORT": str(port),
            "CONSOLE_USERS_FILE": str(users_file),
            "CONSOLE_ACCESS_REQUESTS_DB": str(tmp / "access_requests.db"),
            # S1: живые бэкенды tool-loop (ключи из env раннера, без печати)
            "WS_MCP_URL": e2e_chat_backends.mcp_url,
            "WS_MCP_KEY": e2e_chat_backends.mcp_key,
            "WS_LLM_URL": e2e_chat_backends.llm_url,
            "LITELLM_MASTER_KEY": e2e_chat_backends.llm_key,
            # Диалоги e2e не пишем в живой ws-redis (fail-soft store=None)
            "WS_REDIS_URL": "",
            # Контейнерный IP LiteLLM — мимо любых прокси раннера
            "NO_PROXY": ",".join(
                dict.fromkeys(
                    [
                        *os.environ.get("NO_PROXY", "").split(","),
                        "127.0.0.1",
                        "localhost",
                        llm_host,
                    ]
                )
            ),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_console.app"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    try:
        import httpx

        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                resp = httpx.get(f"{url}/login", timeout=1.0, trust_env=False)
                if resp.status_code == 200 and "f-user" in resp.text:
                    ready = True
                    break
            except Exception:  # noqa: BLE001, S110 — ждём старт сервера
                pass
            time.sleep(0.25)
        if not ready or proc.poll() is not None:
            tail = log_path.read_text()[-2000:]
            pytest.fail(
                f"чат-self-инстанс kb-console не поднялся за 30s "
                f"(rc={proc.returncode}); хвост лога {log_path}:\n{tail}",
                pytrace=False,
            )
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture
def chat_as_admin(page: Page, e2e_chat_server: str) -> ConsoleRoleSession:
    """S1: сессия admin в чат-контуре (живые MCP+LLM, per-user инстанс)."""
    return _role_session(page, e2e_chat_server, "admin")
