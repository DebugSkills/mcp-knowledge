"""Ф4.4b/Ф4.5b: тесты страницы «Очередь» — панель очередей ws-контура (kb-console).

Покрывает:
  (а) ROUTES: /queue ровно один раз, label «Очередь», min_role contributor;
  (б) fetch_queue_snapshot на фейковом клиенте (dict-обёртка): пусто /
      агрегация job (min-позиция, «впереди N», prio/class, просрочен) /
      ETA-диапазон / отсутствие ws:eta и ws:posq / битый JSON ws:eta;
  (в) полка gpu — показывается только при наличии ключей;
  (г) build_queue fail-soft: make_ws_redis → RuntimeError → баннер, не падает;
  (д) Ф4.5b per-job приоритет: запись/сброс ws:prio:{job} (ключ/значение/TTL,
      событие в ws:quota:events — читать хвостом xrevrange), валидация до
      записи, гейт роли (только admin), поля prio_override/prio_source в
      snapshot (в т.ч. MGET fail-soft), UI-обработчики (notify/пере-рендер).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kb_console.pages import ROUTES, queue

# ── Фейковый ws-redis (duck-typed: zrange/zscore/hgetall/get) ──────────


class FakeRedis:
    """Dict-обёртка над подмножеством API redis-py, нужным snapshot'у."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int | None] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._seq = 0

    # string
    def get(self, key: str):
        return self.kv.get(key)

    # zset: zrange по (score, member) — как сортирует Redis
    def zrange(self, key: str, start: int = 0, end: int = -1, withscores: bool = False):
        members = self.zsets.get(key, {})
        ordered = sorted(members.items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            ordered = ordered[start:]
        else:
            ordered = ordered[start : end + 1]
        if withscores:
            return list(ordered)
        return [m for m, _ in ordered]

    def zscore(self, key: str, member: str):
        return self.zsets.get(key, {}).get(member)

    # hash
    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    # неиспользуемые в snapshot, но часть duck-контракта страницы
    def smembers(self, key: str) -> set[str]:
        return set()

    def scan_iter(self, match: str | None = None):
        return iter([])

    # string: пакетное чтение (Ф4.5b — MGET ws:prio:{job})
    def mget(self, keys: list[str]) -> list:
        return [self.kv.get(k) for k in keys]

    # string: запись с TTL (Ф4.5b — SET ws:prio:{job} EX)
    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value
        self.ttls[key] = ex

    # delete (Ф4.5b — DEL ws:prio:{job}); возвращает число снятых ключей
    def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                removed += 1
        return removed

    # stream: XADD (Ф4.5b — события ws:quota:events)
    def xadd(
        self,
        key: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    # stream: читать ХВОСТом (стрим общий, накапливается — SSOT prio.py)
    def xrevrange(
        self, key: str, max: str = "+", min: str = "-", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = list(reversed(self.streams.get(key, [])))
        return entries[:count] if count is not None else entries


def _seed_waiting(c: FakeRedis, shelf: str, calls: list[dict]) -> None:
    """Поставить вызовы: dict(call, vft, pos?, prio, class, job, starve_dl)."""
    for item in calls:
        call = item["call"]
        c.zsets.setdefault(f"ws:q:{shelf}", {})[call] = item["vft"]
        c.zsets.setdefault(f"ws:starve:{shelf}", {})[call] = item["starve_dl"]
        c.hashes[f"ws:call:{shelf}:{call}"] = {
            "prio": item.get("prio", ""),
            "class": item.get("class", ""),
            "job": item.get("job", ""),
        }
        if item.get("pos") is not None:
            c.kv[f"ws:pos:{call}"] = str(item["pos"])


# ── (а) ROUTES ─────────────────────────────────────────────────────────


def test_queue_route_registered():
    """/queue в ROUTES ровно один раз: label «Очередь», min_role contributor."""
    matches = [r for r in ROUTES if r[0] == "/queue"]
    assert len(matches) == 1
    assert matches[0][1] == "Очередь"
    assert matches[0][3] == "contributor"
    assert callable(matches[0][2])


# ── (б) fetch_queue_snapshot ───────────────────────────────────────────


def test_snapshot_empty():
    """Пустой ws-redis: две базовые полки, глубина 0, jobs [], gpu НЕ выдуман."""
    snap = queue.fetch_queue_snapshot(FakeRedis(), now=100.0)
    assert snap["ok"] is True
    assert snap["now"] == 100.0
    assert [s["shelf"] for s in snap["shelves"]] == ["local", "ext"]
    for shelf in snap["shelves"]:
        assert shelf["depth"] == 0
        assert shelf["jobs"] == []
        assert shelf["eta"] is None


def test_snapshot_two_jobs_min_position_and_ahead():
    """2 job-а, у одного 2 вызова: позиция job = min позиций, «впереди N»,
    prio/class-бейджи, просроченный (starve_deadline <= now) помечен."""
    c = FakeRedis()
    _seed_waiting(
        c,
        "local",
        [
            {
                "call": "jobA:0:0",
                "vft": 10.0,
                "pos": 1,
                "prio": "high",
                "class": "interactive",
                "job": "jobA",
                "starve_dl": 200.0,
            },
            {
                "call": "jobA:1:0",
                "vft": 20.0,
                "pos": 2,
                "prio": "high",
                "class": "interactive",
                "job": "jobA",
                "starve_dl": 200.0,
            },
            {
                "call": "jobB:0:0",
                "vft": 15.0,
                "pos": 3,
                "prio": "low",
                "class": "batch",
                "job": "jobB",
                "starve_dl": 50.0,
            },  # now=100 → просрочен
        ],
    )
    c.kv["ws:posq:local"] = "3"
    c.kv["ws:eta:local"] = json.dumps(
        {"ema_s": 2.0, "p95_s": 5.0, "n": 4, "updated_at": 1.0}
    )

    snap = queue.fetch_queue_snapshot(c, now=100.0)
    local = next(s for s in snap["shelves"] if s["shelf"] == "local")
    assert local["depth"] == 3
    assert local["waiting_calls"] == 3
    assert local["eta"]["n"] == 4

    jobs = {j["job"]: j for j in local["jobs"]}
    assert set(jobs) == {"jobA", "jobB"}
    # jobA: min(1, 2) = 1; впереди 0; два вызова; бейджи из per-call HASH
    assert jobs["jobA"]["position"] == 1
    assert jobs["jobA"]["ahead"] == 0
    assert jobs["jobA"]["calls"] == 2
    assert jobs["jobA"]["prio"] == "high"
    assert jobs["jobA"]["call_class"] == "interactive"
    assert jobs["jobA"]["starved"] is False
    # jobB: позиция 3, впереди 2, просрочен (aging-пол)
    assert jobs["jobB"]["position"] == 3
    assert jobs["jobB"]["ahead"] == 2
    assert jobs["jobB"]["starved"] is True
    # сортировка: просроченный (aging-пол приоритетнее всех) — первым
    assert local["jobs"][0]["job"] == "jobB"


def test_snapshot_eta_range_positions():
    """ETA-диапазон: pos=1 → (0, p95); pos=3 → (2*ema, 3*p95)."""
    c = FakeRedis()
    _seed_waiting(
        c,
        "local",
        [
            {
                "call": "j1:0:0",
                "vft": 1.0,
                "pos": 1,
                "prio": "high",
                "class": "interactive",
                "job": "j1",
                "starve_dl": 200.0,
            },
            {
                "call": "j3:0:0",
                "vft": 3.0,
                "pos": 3,
                "prio": "low",
                "class": "batch",
                "job": "j3",
                "starve_dl": 200.0,
            },
        ],
    )
    c.kv["ws:eta:local"] = json.dumps(
        {"ema_s": 2.0, "p95_s": 5.0, "n": 9, "updated_at": 1.0}
    )

    snap = queue.fetch_queue_snapshot(c, now=100.0)
    jobs = {j["job"]: j for j in snap["shelves"][0]["jobs"]}
    assert jobs["j1"]["eta_range"] == (0.0, 5.0)  # (pos-1)*ema=0; pos*p95
    assert jobs["j3"]["eta_range"] == (4.0, 15.0)  # 2*ema; 3*p95


def test_snapshot_missing_eta_and_posq_ok():
    """Нет ws:eta / ws:posq → не ломается: глубина из очереди, eta None."""
    c = FakeRedis()
    _seed_waiting(
        c,
        "ext",
        [
            {
                "call": "jX:0:0",
                "vft": 7.0,
                "pos": 1,
                "prio": "med",
                "class": "background",
                "job": "jX",
                "starve_dl": 900.0,
            },
        ],
    )
    snap = queue.fetch_queue_snapshot(c, now=100.0)
    ext = next(s for s in snap["shelves"] if s["shelf"] == "ext")
    assert ext["eta"] is None
    assert ext["depth"] == 1  # fallback: len(waiting)
    assert ext["depth_source"] == "waiting"
    job = ext["jobs"][0]
    assert job["eta_range"] is None  # без снимка ETA нет диапазона


def test_snapshot_broken_eta_json_degrades_to_none():
    """Битый JSON в ws:eta → eta=None (очередь цела).

    Обоснование: SSOT ai_workspace/scheduler/eta.py ETAStore.snapshot сам
    возвращает None на битый JSON (display-only, read-хелпер UI не падает
    на чужих данных) — панель повторяет семантику SSOT, а не изобретает
    собственный сбой.
    """
    c = FakeRedis()
    _seed_waiting(
        c,
        "local",
        [
            {
                "call": "jY:0:0",
                "vft": 1.0,
                "pos": 1,
                "prio": "med",
                "class": "batch",
                "job": "jY",
                "starve_dl": 900.0,
            },
        ],
    )
    c.kv["ws:eta:local"] = "{not-json"
    snap = queue.fetch_queue_snapshot(c, now=100.0)
    local = snap["shelves"][0]
    assert local["eta"] is None
    assert local["jobs"][0]["job"] == "jY"  # очередь отрисована
    assert local["jobs"][0]["eta_range"] is None


def test_snapshot_call_without_position():
    """Вызов без ws:pos (глубина > max_detail): job без позиции, не падает."""
    c = FakeRedis()
    _seed_waiting(
        c,
        "local",
        [
            {
                "call": "deep:0:0",
                "vft": 1.0,
                "pos": None,
                "prio": "low",
                "class": "background",
                "job": "deep",
                "starve_dl": 900.0,
            },
        ],
    )
    snap = queue.fetch_queue_snapshot(c, now=100.0)
    job = snap["shelves"][0]["jobs"][0]
    assert job["position"] is None
    assert job["ahead"] is None
    assert job["eta_range"] is None


# ── (в) полка gpu ──────────────────────────────────────────────────────


def test_gpu_shelf_shown_only_when_keys_exist():
    """gpu показывается при наличии ws:q:gpu; без ключей — НЕ выдумывается."""
    empty = queue.fetch_queue_snapshot(FakeRedis(), now=1.0)
    assert [s["shelf"] for s in empty["shelves"]] == ["local", "ext"]

    c = FakeRedis()
    c.zsets["ws:q:gpu"] = {"g:0:0": 1.0}
    snap = queue.fetch_queue_snapshot(c, now=1.0)
    assert [s["shelf"] for s in snap["shelves"]] == ["local", "ext", "gpu"]


# ── (г) build_queue fail-soft ──────────────────────────────────────────


def test_build_queue_fail_soft_without_ws_redis():
    """Нет ws-redis (make_ws_redis → RuntimeError): баннер, страница НЕ падает.

    В тексте — только КЛАСС исключения (str redis-ошибок несёт host:port —
    гигиена core/redis_client.py), без стек-трейса.
    """
    with (
        patch.object(
            queue, "make_ws_redis", side_effect=RuntimeError("WS_REDIS_URL не задан")
        ),
        patch.object(queue, "ui") as mock_ui,
    ):
        # @ui.refreshable → passthrough: render() зовёт _render(state)
        # напрямую (в реальном NiceGUI — refreshable-контейнер).
        mock_ui.refreshable.side_effect = lambda fn: fn
        queue.build_queue()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("ws-redis недоступен" in t for t in labels)
    assert any("RuntimeError" in t for t in labels)


def test_build_queue_fail_soft_on_connection_error():
    """ConnectionError при первом ЧТЕНИИ (redis поднят по env, но недоступен) —
    тот же баннер с классом ошибки: транзиентный сбой не роняет страницу."""
    with (
        patch.object(queue, "make_ws_redis", return_value=object()),
        patch.object(queue, "fetch_queue_snapshot", side_effect=ConnectionError),
        patch.object(queue, "ui") as mock_ui,
    ):
        mock_ui.refreshable.side_effect = lambda fn: fn
        queue.build_queue()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("ws-redis недоступен" in t for t in labels)
    assert any("ConnectionError" in t for t in labels)


# ── (д) Ф4.5b: per-job приоритет — запись/гейт/snapshot/fail-soft ──────


def _seed_two_jobs(c: FakeRedis) -> None:
    """2 job-а на local (jobA: med/interactive, jobB: low/batch)."""
    _seed_waiting(
        c,
        "local",
        [
            {
                "call": "jobA:0:0",
                "vft": 10.0,
                "pos": 1,
                "prio": "med",
                "class": "interactive",
                "job": "jobA",
                "starve_dl": 900.0,
            },
            {
                "call": "jobB:0:0",
                "vft": 15.0,
                "pos": 2,
                "prio": "low",
                "class": "batch",
                "job": "jobB",
                "starve_dl": 900.0,
            },
        ],
    )


def test_set_job_priority_ui_key_value_ttl_and_event():
    """Контракт записи (дубль SSOT prio.py): ключ ws:prio:{job}, значение,
    TTL 24 ч (>0), событие job_priority_set в ws:quota:events — читать
    ХВОСТом xrevrange (стрим общий, накапливается)."""
    c = FakeRedis()
    # стрим уже накапливался: чужие события раньше наших — хвост всё равно наш
    c.xadd(
        "ws:quota:events",
        {"event": json.dumps({"type": "quota_reclaimed", "ts": 1.0})},
    )

    queue.set_job_priority_ui(c, "jobA", "high", actor="op")

    assert c.kv["ws:prio:jobA"] == "high"
    assert c.ttls["ws:prio:jobA"] == 86_400  # SSOT prio.DEFAULT_TTL_S (>0)
    tail = c.xrevrange("ws:quota:events", count=1)
    event = json.loads(tail[0][1]["event"])
    assert event["type"] == "job_priority_set"
    assert event["job"] == "jobA"
    assert event["prio"] == "high"
    assert event["actor"] == "op"


def test_clear_job_priority_ui_removes_key_and_emits():
    """Сброс: DEL ключа + событие job_priority_cleared; идемпотентен."""
    c = FakeRedis()
    queue.set_job_priority_ui(c, "jobA", "med", actor="op")

    removed = queue.clear_job_priority_ui(c, "jobA", actor="op")

    assert removed is True
    assert "ws:prio:jobA" not in c.kv
    event = json.loads(c.xrevrange("ws:quota:events", count=1)[0][1]["event"])
    assert event["type"] == "job_priority_cleared"
    assert event["job"] == "jobA"
    assert event["actor"] == "op"
    # повторный сброс: ключа нет → False, событие всё равно фиксирует команду
    assert queue.clear_job_priority_ui(c, "jobA", actor="op") is False


def test_set_job_priority_ui_invalid_prio_refused_before_redis():
    """Валидация ДО записи (SSOT fail-closed): невалидный prio → ValueError,
    ключ НЕ записан, события нет — мусор через UI в ключ не попадает."""
    c = FakeRedis()
    with pytest.raises(ValueError):
        queue.set_job_priority_ui(c, "jobA", "urgent", actor="op")
    assert "ws:prio:jobA" not in c.kv
    assert c.streams.get("ws:quota:events") in (None, [])


def test_set_job_priority_ui_event_best_effort():
    """XADD-событие — best-effort (паттерн SSOT prio.emit_event): сбой
    стрима НЕ валит операцию — ключ записан, исключение не выходит."""
    class XaddBoomRedis(FakeRedis):
        def xadd(self, *args: object, **kwargs: object) -> str:
            raise ConnectionError("stream boom")

    c = XaddBoomRedis()
    queue.set_job_priority_ui(c, "jobA", "low", actor="op")
    assert c.kv["ws:prio:jobA"] == "low"


def test_can_manage_priority_role_gate():
    """Гейт роли — ЧИСТАЯ функция (тестируется без UI): контроль только
    admin; страница остаётся contributor-видимой (гейт на КОНТРОЛЕ)."""
    assert queue.can_manage_priority("admin") is True
    assert queue.can_manage_priority("editor") is False
    assert queue.can_manage_priority("contributor") is False


def test_snapshot_prio_override_and_source_fields():
    """snapshot: prio_override = ws:prio:{job} или None; prio_source =
    "job" при валидном override, иначе "account"."""
    c = FakeRedis()
    _seed_two_jobs(c)
    c.kv["ws:prio:jobA"] = "high"

    snap = queue.fetch_queue_snapshot(c, now=100.0)
    jobs = {j["job"]: j for j in snap["shelves"][0]["jobs"]}

    assert jobs["jobA"]["prio_override"] == "high"
    assert jobs["jobA"]["prio_source"] == "job"
    assert jobs["jobB"]["prio_override"] is None
    assert jobs["jobB"]["prio_source"] == "account"


def test_snapshot_prio_garbage_value_source_account():
    """Мусор в ws:prio (ручная правка мимо API) → source "account"
    (семантика SSOT effective_priority: availability > strictness);
    сырое значение остаётся в prio_override для витрины."""
    c = FakeRedis()
    _seed_two_jobs(c)
    c.kv["ws:prio:jobA"] = "urgent"

    snap = queue.fetch_queue_snapshot(c, now=100.0)
    job = next(j for j in snap["shelves"][0]["jobs"] if j["job"] == "jobA")

    assert job["prio_override"] == "urgent"
    assert job["prio_source"] == "account"


def test_snapshot_mget_failure_fail_soft():
    """Сбой MGET → поля None/"account", snapshot жив (fail-soft: приоритет —
    доп-поля витрины, не причина ронять очередь)."""
    class MgetBoomRedis(FakeRedis):
        def mget(self, keys: list[str]) -> list:
            raise ConnectionError("ws-redis down")

    c = MgetBoomRedis()
    _seed_two_jobs(c)
    c.kv["ws:prio:jobA"] = "high"

    snap = queue.fetch_queue_snapshot(c, now=100.0)

    assert snap["ok"] is True
    jobs = {j["job"]: j for j in snap["shelves"][0]["jobs"]}
    assert jobs["jobA"]["prio_override"] is None
    assert jobs["jobA"]["prio_source"] == "account"
    assert jobs["jobB"]["prio_source"] == "account"


def test_apply_priority_from_ui_success_notifies_and_rerenders():
    """«Применить»: запись + positive-notify + НЕМЕДЛЕННЫЙ пере-рендер
    (не ждём таймера)."""
    c = FakeRedis()
    refreshed: list[int] = []
    with patch.object(queue, "ui") as mock_ui:
        queue._apply_priority_from_ui(
            c,
            "jobA",
            "high",
            actor="op",
            on_refresh=lambda: refreshed.append(1),
        )

    assert c.kv["ws:prio:jobA"] == "high"
    mock_ui.notify.assert_called_once()
    assert mock_ui.notify.call_args.kwargs.get("type") == "positive"
    assert refreshed == [1]


def test_apply_priority_from_ui_invalid_prio_negative_notify():
    """Невалидный prio (селектор пуст) → отказ notify-negative, ключ НЕ
    записан, валидация до любого обращения к redis."""
    c = FakeRedis()
    with patch.object(queue, "ui") as mock_ui:
        queue._apply_priority_from_ui(c, "jobA", None, actor="op")

    assert "ws:prio:jobA" not in c.kv
    mock_ui.notify.assert_called_once()
    assert mock_ui.notify.call_args.kwargs.get("type") == "negative"


def test_apply_priority_from_ui_redis_down_notifies_class_only():
    """SET/XADD бросают → не падает, notify с КЛАССОМ ошибки (без host:port —
    гигиена core/redis_client.py), пере-рендера нет."""
    class SetBoomRedis(FakeRedis):
        def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise ConnectionError("redis://secret-host:6379 down")

        def xadd(self, *args: object, **kwargs: object) -> str:
            raise ConnectionError("xadd boom")

    c = SetBoomRedis()
    refreshed: list[int] = []
    with patch.object(queue, "ui") as mock_ui:
        queue._apply_priority_from_ui(  # fail-soft: исключение НЕ выходит
            c,
            "jobA",
            "high",
            actor="op",
            on_refresh=lambda: refreshed.append(1),
        )

    mock_ui.notify.assert_called_once()
    text = " ".join(str(a) for a in mock_ui.notify.call_args.args)
    assert "ConnectionError" in text  # только класс ошибки
    assert "secret-host" not in text
    assert "6379" not in text
    assert refreshed == []  # пере-рендера не было
    assert "ws:prio:jobA" not in c.kv


def test_clear_priority_from_ui_redis_down_notifies():
    """«Сбросить» при недоступном redis → notify-negative, не падает."""

    class DeleteBoomRedis(FakeRedis):
        def delete(self, *keys: str) -> int:
            raise TimeoutError("del boom")

    c = DeleteBoomRedis()
    with patch.object(queue, "ui") as mock_ui:
        queue._clear_priority_from_ui(c, "jobA", actor="op")

    mock_ui.notify.assert_called_once()
    assert mock_ui.notify.call_args.kwargs.get("type") == "negative"
