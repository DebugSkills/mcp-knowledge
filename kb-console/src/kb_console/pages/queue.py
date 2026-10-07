"""Страница «Очередь» — панель очередей вызовов ws-контура (Ф4.4b).

Read-only витрина планировщика AI-верстака: консоль НЕ импортирует
``ai_workspace`` — читает ws-redis напрямую (прецедент Ф2:
``core/redis_client.make_ws_redis``, как ``pages/chat.py:_build_store``).

Ф4.5b: страница остаётся contributor-видимой на чтение, но admin получает
UI-контроль per-job приоритета — селектор + «Применить»/«Сбросить» для
``ws:prio:{job}`` (гейт роли — на КОНТРОЛЕ, не на странице; запись —
намеренное дублирование ~5 строк контракта SSOT, см.
``set_job_priority_ui``).

Контракт ключей (Ф4.4a, чтение; ``ws:prio`` — чтение+запись):
- ``ws:q:{shelf}`` — ZSET call→raw VFT (ожидающие);
- ``ws:call:{shelf}:{call}`` — HASH (prio, class, job, epoch, attempt,
  vft, starve_deadline);
- ``ws:pos:{call}`` — позиция вызова (1-based, меньше = раньше);
  ``ws:posq:{shelf}`` — глубина полки (int);
- ``ws:starve:{shelf}`` — ZSET call→starve_deadline (просроченные =
  aging-пол, приоритетнее всех — инвариант I2);
- ``ws:eta:{shelf}`` — JSON {"ema_s","p95_s","n","updated_at"};
- ``ws:prio:{job}`` — STRING high|med|low (override приоритета job'а,
  TTL 24 ч; SSOT — ai_workspace/scheduler/prio.py);
- исполняющиеся: ``ws:lease:{shelf}:{call}``, ``ws:job:{id}`` — в этой
  итерации НЕ читаются (панель показывает ожидающих; остаток — см. отчёт).

Полки: ``local``, ``ext`` (контурные — показываются всегда, даже пустые);
``gpu`` — только при наличии ключей (не выдумываем).

Fail-soft: у операторской консоли ``WS_REDIS_URL`` НЕ задан (env — только
сервису workspace, compose.workspace.yml) → ``RuntimeError``/сетевые сбои
гасятся в баннер «ws-redis недоступен» с КЛАССОМ ошибки (str redis-ошибок
несёт host:port — гигиена core/redis_client.py); страница не падает,
таймер продолжает попытки. Сбой записи приоритета → ``ui.notify`` (класс
ошибки, без host:port), страница жива, таймер работает.

R6: ``components/queue_console.py`` — витрина ИМПОРТ-очереди KB; здесь
общего только слово «очередь» — НЕ переиспользуется и не смешивается.

Ф6 TODO 4б (К4): внизу страницы — блок «Метрики узлов» (счётчики
``ws:metrics:*``: calls/cached/tokens по лейблам {kind, model_class,
shelf, role} + ``usage_fallback_total``). Состояние НЕ дублируется в
памяти: носитель — Redis, перечитывается каждым обновлением таймера;
сбой метрик — отдельный fail-soft (НЕ роняет панель очередей).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from nicegui import ui

from ..core.identity import current_actor, current_role
from ..core.redis_client import make_ws_redis
from ..ws_metrics import fetch_ws_metrics_snapshot

REFRESH_SECONDS = 2.0
"""Авто-обновление панели (паттерн status.py / import_page.py:227-237)."""

BASE_SHELVES: tuple[str, ...] = ("local", "ext")
GPU_SHELF = "gpu"

VALID_PRIORITIES: tuple[str, ...] = ("high", "med", "low")
"""Допустимые значения override — SSOT ``ai_workspace/scheduler/policy.MULT``
(ключи weight-матрицы WFQ); менять синхронно с бэкендом."""

PRIO_OVERRIDE_TTL_S = 86_400
"""TTL override 24 ч — SSOT ``prio.DEFAULT_TTL_S`` (SET EX продлевает TTL)."""

QUOTA_EVENTS_KEY = "ws:quota:events"
"""Стрим событий ws-контура — SSOT ``admission.QUOTA_EVENTS_KEY``."""

QUOTA_EVENTS_MAXLEN = 10_000
"""MAXLEN ~ стрима событий — SSOT ``admission.DEFAULT_QUOTA_STREAM_MAXLEN``."""

# ── Ключи ws-контура (имена — SSOT спека Scheduler §2 / scheduler/*.py) ──


def q_key(shelf: str) -> str:
    return f"ws:q:{shelf}"


def starve_key(shelf: str) -> str:
    return f"ws:starve:{shelf}"


def call_key(shelf: str, call: str) -> str:
    return f"ws:call:{shelf}:{call}"


def pos_key(call: str) -> str:
    return f"ws:pos:{call}"


def posq_key(shelf: str) -> str:
    return f"ws:posq:{shelf}"


def eta_key(shelf: str) -> str:
    return f"ws:eta:{shelf}"


def prio_key(job: str) -> str:
    """Ключ override приоритета job'а — SSOT ``scheduler/prio.py::prio_key``."""
    return f"ws:prio:{job}"


# ── Чистое ядро: snapshot (без NiceGUI, duck-typed клиент) ─────────────


def _eta_range(
    position: int | None, eta: dict[str, Any] | None
) -> tuple[float, float] | None:
    """Диапазон ожидания (lower, upper) для ранга position.

    Дублирование SSOT — 1 строка формулы: ai_workspace/scheduler/eta.py
    ::eta_range (R5): lower = max(0, pos-1)*ema_s (ожидание >= lower),
    upper = max(1, pos)*p95_s (медленный хвост). Помечать в UI как
    ОЦЕНКУ, не факт. Битый снимок → None (семантика SSOT ETAStore.snapshot).
    """
    if eta is None or position is None or position < 1:
        return None
    try:
        return (
            max(0, position - 1) * float(eta["ema_s"]),
            max(1, position) * float(eta["p95_s"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_eta(client: Any, shelf: str) -> dict[str, Any] | None:
    """``ws:eta:{shelf}`` → dict; нет ключа/битый JSON → None (SSOT-семантика
    ``ETAStore.snapshot``: display-only, read-хелпер не падает на чужих данных)."""
    raw = client.get(eta_key(shelf))
    if raw is None:
        return None
    try:
        snap = json.loads(raw)
    except ValueError:
        return None
    return snap if isinstance(snap, dict) else None


def _shelves_present(client: Any) -> list[str]:
    """Полки к показу: local/ext — всегда (контурные), gpu — только если
    его ключи есть (``ws:q:gpu`` непуст или написан ``ws:posq:gpu``)."""
    shelves = list(BASE_SHELVES)
    has_gpu = bool(client.zrange(q_key(GPU_SHELF), 0, -1)) or (
        client.get(posq_key(GPU_SHELF)) is not None
    )
    if has_gpu:
        shelves.append(GPU_SHELF)
    return shelves


def _aggregate_jobs(
    calls: list[dict[str, Any]], eta: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Группировка вызовов по job (поле ``job`` per-call HASH; пустое →
    группа самого вызова). Позиция job = MIN позиций его вызовов;
    «впереди N» = pos-1. Сортировка: просроченные (aging-пол) первыми,
    затем по позиции, без позиции — в конец."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        groups.setdefault(c["job"] or c["call"], []).append(c)

    jobs: list[dict[str, Any]] = []
    for key, grp in groups.items():
        positions = [c["position"] for c in grp if c["position"] is not None]
        pos = min(positions) if positions else None
        lead = next((c for c in grp if c["position"] == pos), grp[0])
        jobs.append(
            {
                "job": key,
                "position": pos,
                "ahead": (pos - 1) if pos is not None else None,
                "starved": any(c["starved"] for c in grp),
                "prio": lead["prio"],
                "call_class": lead["call_class"],
                "calls": len(grp),
                "eta_range": _eta_range(pos, eta),
            }
        )
    jobs.sort(
        key=lambda j: (
            not j["starved"],
            j["position"] if j["position"] is not None else float("inf"),
            j["job"],
        )
    )
    return jobs



def _attach_prio_overrides(client: Any, jobs: list[dict[str, Any]]) -> None:
    """Дописать в job-записи ``prio_override``/``prio_source`` (Ф4.5b).

    Пакетное ``MGET ws:prio:{job}`` — один вызов на полку, не N GET.
    ``prio_source`` = "job" только при ВАЛИДНОМ значении: мусор в ключе
    (ручная правка мимо API) → "account" — семантика SSOT
    ``prio.effective_priority`` (availability > strictness), сырое значение
    остаётся в ``prio_override`` для витрины. Fail-soft: сбой MGET →
    ``None``/"account" — приоритет-поля не роняют snapshot очереди.
    """
    if not jobs:
        return
    try:
        values = client.mget([prio_key(j["job"]) for j in jobs])
    except Exception:
        values = None
    for idx, job in enumerate(jobs):
        raw = values[idx] if values is not None else None
        override = str(raw) if raw is not None else None
        job["prio_override"] = override
        job["prio_source"] = "job" if override in VALID_PRIORITIES else "account"


def fetch_queue_snapshot(client: Any, *, now: float) -> dict[str, Any]:
    """Собрать snapshot очередей ws-контура (чистое ядро, без NiceGUI).

    ``client`` — duck-typed ws-redis (zrange/zscore/hgetall/get/...,
    decode_responses=True). Тяжёлые/сетевые сбои — НАРУЖУ исключением
    (UI-слой ловит и показывает баннер); здесь — только данные.

    Возвращаемая структура (render + asserts):

    ::

        {"ok": True, "now": <float>, "shelves": [
            {"shelf": "local", "depth": <int>, "depth_source": "posq"|"waiting",
             "waiting_calls": <int>, "eta": <dict|None>, "jobs": [
                {"job": str, "position": int|None, "ahead": int|None,
                 "starved": bool, "prio": str, "call_class": str,
                 "calls": int, "eta_range": (lower, upper)|None,
                 "prio_override": str|None, "prio_source": "job"|"account"}]}]}
    """
    shelves_out: list[dict[str, Any]] = []
    for shelf in _shelves_present(client):
        waiting = [
            (str(member), float(score))
            for member, score in client.zrange(q_key(shelf), 0, -1, withscores=True)
        ]
        raw_depth = client.get(posq_key(shelf))
        calls: list[dict[str, Any]] = []
        for call, _vft in waiting:
            rec = client.hgetall(call_key(shelf, call)) or {}
            pos_raw = client.get(pos_key(call))
            dl_raw = client.zscore(starve_key(shelf), call)
            deadline = float(dl_raw) if dl_raw is not None else None
            calls.append(
                {
                    "call": call,
                    "job": str(rec.get("job") or ""),
                    "prio": str(rec.get("prio") or ""),
                    "call_class": str(rec.get("class") or ""),
                    "position": int(pos_raw) if pos_raw is not None else None,
                    "starve_deadline": deadline,
                    "starved": deadline is not None and deadline <= now,
                }
            )
        eta = _read_eta(client, shelf)
        jobs = _aggregate_jobs(calls, eta)
        _attach_prio_overrides(client, jobs)
        shelves_out.append(
            {
                "shelf": shelf,
                "depth": int(raw_depth) if raw_depth is not None else len(waiting),
                "depth_source": "posq" if raw_depth is not None else "waiting",
                "waiting_calls": len(waiting),
                "eta": eta,
                "jobs": jobs,
            }
        )
    return {"ok": True, "now": float(now), "shelves": shelves_out}


# ── Ф4.5b: per-job приоритет — запись ───────────────────────────────────
# ⚠️ SSOT — ai_workspace/scheduler/prio.py; менять синхронно. Консоль НЕ
# импортирует ai_workspace (прецедент Ф2/Ф4.4b) — намеренное дублирование
# ~5 строк контракта (ключ/SET EX/TTL/событие); покрыто тестами формы.


def can_manage_priority(role: str) -> bool:
    """Гейт роли на приоритет-КОНТРОЛЕ: только admin.

    Чистая функция (тестируется без UI): страница «Очередь» остаётся
    contributor-видимой на чтение — роль режет именно контроль записи,
    не витрину (ROUTES /queue: min_role contributor).
    """
    return role == "admin"


def _prio_event_payload(type_: str, **fields: Any) -> str:
    """JSON события — формат SSOT ``admission._quota_event`` (единый вид
    стрима): ``{"type", "ts", **fields}``, компактные разделители."""
    payload: dict[str, Any] = {"type": type_, "ts": round(time.time(), 6)}
    payload.update(fields)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _emit_prio_event(client: Any, type_: str, **fields: Any) -> None:
    """XADD события в ``ws:quota:events`` (MAXLEN ~) — best-effort.

    Паттерн SSOT ``prio.emit_event``: наблюдение не валит операцию; в
    отличие от бэкенда гасим любое исключение (консоль не знает классов
    ws-контура, а UI-мутация уже подтверждена SET/DEL).
    """
    try:
        client.xadd(
            QUOTA_EVENTS_KEY,
            {"event": _prio_event_payload(type_, **fields)},
            maxlen=QUOTA_EVENTS_MAXLEN,
            approximate=True,
        )
    except Exception:
        pass


def set_job_priority_ui(
    client: Any,
    job: str,
    prio: str,
    *,
    actor: str,
    ttl_s: int = PRIO_OVERRIDE_TTL_S,
    reason: str = "ui",
) -> None:
    """Установить override приоритета job'а из консоли (``SET EX``).

    Дублирование SSOT ``prio.set_job_priority`` — менять синхронно:
    ключ ``ws:prio:{job}``, значение ``high|med|low`` (SSOT — policy.MULT),
    ``SET EX`` — значение и TTL атомарны, повторная установка ПРОДЛЕВАЕТ
    TTL (24 ч по умолчанию); событие ``job_priority_set`` (job/prio/
    ttl_s/actor/reason) — best-effort в ``ws:quota:events``. Валидация —
    ДО любого обращения к redis (fail-closed: мусор через UI в ключ не
    попадает). Очередь НЕ трогается: override подхватывают только
    ПОСЛЕДУЮЩИЕ admit/enqueue вызовов job'а.
    """
    if not isinstance(job, str) or not job.strip():
        raise ValueError(f"job должен быть непустой строкой, получено: {job!r}")
    if prio not in VALID_PRIORITIES:
        raise ValueError(
            f"prio должен быть одним из {list(VALID_PRIORITIES)} "
            f"(SSOT — policy.MULT), получено: {prio!r}"
        )
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0:
        raise ValueError(f"ttl_s должен быть целым > 0, получено: {ttl_s!r}")
    client.set(prio_key(job), prio, ex=ttl_s)
    _emit_prio_event(
        client,
        "job_priority_set",
        job=job,
        prio=prio,
        ttl_s=ttl_s,
        actor=actor,
        reason=reason,
    )


def clear_job_priority_ui(client: Any, job: str, *, actor: str) -> bool:
    """Снять override (``DEL``); событие ``job_priority_cleared`` — всегда.

    Дублирование SSOT ``prio.clear_job_priority`` — менять синхронно.
    ``True`` — ключ был, ``False`` — нет (идемпотентно; событие фиксирует
    саму команду оператора).
    """
    removed = bool(client.delete(prio_key(job)))
    _emit_prio_event(
        client, "job_priority_cleared", job=job, actor=actor, removed=removed
    )
    return removed


def _apply_priority_from_ui(
    client: Any,
    job: str,
    prio: Any,
    *,
    actor: str,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Обработчик «Применить»: запись + notify + НЕМЕДЛЕННЫЙ пере-рендер.

    Fail-soft: любая ошибка (валидация/ws-redis) → ``ui.notify`` negative
    только с КЛАССОМ исключения (str redis-ошибок несёт host:port —
    гигиена core/redis_client.py); страница жива, таймер работает.
    """
    try:
        set_job_priority_ui(client, job, prio, actor=actor)
    except ValueError as exc:
        ui.notify(f"Приоритет {job} не записан: {exc}", type="negative")
        return
    except Exception as exc:
        ui.notify(
            f"Приоритет {job} не записан ({type(exc).__name__}) — "
            "ws-redis недоступен?",
            type="negative",
        )
        return
    ui.notify(
        f"Приоритет {job} → {prio} на 24 ч (подействует на последующие вызовы)",
        type="positive",
    )
    if on_refresh is not None:
        on_refresh()


def _clear_priority_from_ui(
    client: Any,
    job: str,
    *,
    actor: str,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Обработчик «Сбросить»: DEL + notify + пере-рендер (fail-soft, класс
    ошибки без host:port — см. ``_apply_priority_from_ui``)."""
    try:
        removed = clear_job_priority_ui(client, job, actor=actor)
    except Exception as exc:
        ui.notify(
            f"Сброс приоритета {job} не выполнен ({type(exc).__name__}) — "
            "ws-redis недоступен?",
            type="negative",
        )
        return
    note = "override снят" if removed else "override не был установлен"
    ui.notify(
        f"{job}: {note} (последующие вызовы — приоритет аккаунта)",
        type="positive",
    )
    if on_refresh is not None:
        on_refresh()


def _refresh_metrics(state: dict[str, Any]) -> None:
    """Ф6 4б: собрать метрики ws:metrics:* в state (отдельный fail-soft).

    Сбой метрик НЕ трогает snapshot очередей (``error`` остаётся про
    очередь): панель показывает предупреждение с КЛАССОМ ошибки (без
    host:port — гигиена core/redis_client.py) и живёт до следующего
    тика таймера.
    """
    try:
        client = state["client"] or make_ws_redis()
        state["metrics"] = fetch_ws_metrics_snapshot(client)
        state["metrics_error"] = None
    except Exception as exc:  # fail-soft витрина метрик — см. выше
        state["metrics"] = None
        state["metrics_error"] = type(exc).__name__


# ── UI: страница ───────────────────────────────────────────────────────


def _fmt_seconds(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} с"
    return f"{seconds / 60:.1f} мин"


def _fmt_eta(rng: tuple[float, float] | None) -> str:
    if rng is None:
        return "—"
    lower, upper = rng
    return f"≈ {_fmt_seconds(lower)}–{_fmt_seconds(upper)} (оценка, ≥{_fmt_seconds(lower)})"


def _render(
    state: dict[str, Any],
    *,
    can_manage: bool = False,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Отрисовать состояние панели: баннер ошибки ЛИБО полки с очередями.

    ``can_manage`` — гейт роли на приоритет-КОНТРОЛЕ (только admin;
    contributor/editor видят страницу на чтение); ``on_refresh`` —
    немедленный пере-рендер после успешной записи приоритета.
    """
    error = state.get("error")
    if error is not None:
        with ui.card().classes("w-full q-mb-md"):
            ui.label(f"⚠ ws-redis недоступен ({error}) — очередь неизвестна").classes(
                "text-subtitle1 text-orange"
            )
            ui.label(
                "Панель read-only: обновление продолжится автоматически "
                "(/operator-консоли WS_REDIS_URL может быть не задан — "
                "см. compose.workspace.yml)."
            ).classes("text-caption text-grey")
        return

    snap = state.get("snapshot")
    if snap is None:
        ui.label("Загрузка…").classes("text-grey")
        return

    for shelf in snap["shelves"]:
        with ui.card().classes("w-full q-mb-md"):
            depth_note = (
                f"глубина {shelf['depth']}"
                if shelf["depth_source"] == "posq"
                else f"глубина ~{shelf['depth']} (ws:posq ещё не написан)"
            )
            ui.label(f"Полка «{shelf['shelf']}» · {depth_note}").classes("text-h6")
            eta = shelf["eta"]
            eta_note = (
                f"EMA {float(eta['ema_s']):.1f} с · p95 {float(eta['p95_s']):.1f} с · "
                f"n={eta.get('n', '?')}"
                if eta
                else "ETA: наблюдений ещё не было"
            )
            ui.label(eta_note).classes("text-caption text-grey")

            jobs = shelf["jobs"]
            if not jobs:
                ui.label("Очередь пуста").classes("text-grey")
                continue

            columns = [
                {"name": "job", "label": "Job", "field": "job", "align": "left"},
                {"name": "prio", "label": "prio", "field": "prio", "align": "left"},
                {
                    "name": "class",
                    "label": "class",
                    "field": "call_class",
                    "align": "left",
                },
                {"name": "pos", "label": "Позиция", "field": "pos", "align": "left"},
                {
                    "name": "eta",
                    "label": "Ожидание (оценка)",
                    "field": "eta",
                    "align": "left",
                },
            ]
            rows = []
            for j in jobs:
                pos_text = (
                    f"{'⏰ ' if j['starved'] else ''}{j['position']} · впереди {j['ahead']}"
                    if j["position"] is not None
                    else "—"  # глубже max_detail: пер-вызовные ws:pos не пишутся
                )
                if j["position"] is None and j["starved"]:
                    pos_text = "⏰ просрочен · поз. —"
                if j["prio_source"] == "job":
                    prio_text = f"{j['prio_override']} ⚡ (job-override)"
                else:
                    prio_text = j["prio"] or "—"
                rows.append(
                    {
                        "job": j["job"]
                        + (f" ({j['calls']} вызова)" if j["calls"] > 1 else ""),
                        "prio": prio_text,
                        "call_class": j["call_class"] or "—",
                        "pos": pos_text,
                        "eta": _fmt_eta(j["eta_range"]),
                    }
                )
            ui.table(columns=columns, rows=rows, row_key="job").classes("w-full")
            ui.label(
                "⏰ — просрочен starve-дедлайн (aging-пол, обслуживается вне "
                "общей очереди). ETA — ДИАПАЗОН-оценка (≥ нижней границы), "
                "не факт."
            ).classes("text-caption text-grey")

            if can_manage and jobs:
                ui.separator()
                ui.label("Override приоритета job (только admin)").classes(
                    "text-subtitle2"
                )
                ui.label(
                    "Override живёт 24 ч и действует на ПОСЛЕДУЮЩИЕ вызовы "
                    "job'а — уже стоящие в очереди вызовы не реордерятся."
                ).classes("text-caption text-grey")
                for j in jobs:
                    with ui.row().classes("w-full items-center"):
                        ui.label(j["job"]).classes("col-3 ellipsis")
                        current = (
                            j["prio_override"] if j["prio_source"] == "job" else None
                        )
                        select = ui.select(
                            list(VALID_PRIORITIES),
                            value=current,
                            label="приоритет",
                            clearable=True,
                        )
                        ui.button(
                            "Применить",
                            on_click=lambda j=j, s=select: _apply_priority_from_ui(
                                state["client"],
                                j["job"],
                                s.value,
                                actor=current_actor(),
                                on_refresh=on_refresh,
                            ),
                        )
                        ui.button(
                            "Сбросить",
                            on_click=lambda j=j: _clear_priority_from_ui(
                                state["client"],
                                j["job"],
                                actor=current_actor(),
                                on_refresh=on_refresh,
                            ),
                        ).props("flat")


    _render_metrics(state)


def _render_metrics(state: dict[str, Any]) -> None:
    """Ф6 4б (К4): карточка метрик узлов — таблица calls/cached/tokens по
    лейблам + счётчик usage_fallback_total. Простой блок: одна таблица,
    без агрегаций/графиков (nosherie — носитель К4 это /metrics и Redis)."""
    metrics = state.get("metrics")
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Метрики узлов ws-контура (ws:metrics)").classes("text-h6")
        if metrics is None:
            ui.label(
                f"⚠ Метрики недоступны ({state.get('metrics_error')}) — "
                "обновление продолжится"
            ).classes("text-orange")
        elif not metrics["nodes"]:
            ui.label(
                "Метрик узлов ещё нет — движок не писал ws:metrics:* "
                "(счётчики появятся после golden-run/интеграций)"
            ).classes("text-grey")
        else:
            columns = [
                {"name": "kind", "label": "kind", "field": "kind", "align": "left"},
                {
                    "name": "model_class",
                    "label": "model_class",
                    "field": "model_class",
                    "align": "left",
                },
                {"name": "shelf", "label": "shelf", "field": "shelf", "align": "left"},
                {"name": "role", "label": "role", "field": "role", "align": "left"},
                {
                    "name": "calls",
                    "label": "calls",
                    "field": "calls",
                    "align": "right",
                },
                {
                    "name": "cached",
                    "label": "cached",
                    "field": "cached",
                    "align": "right",
                },
                {
                    "name": "tokens",
                    "label": "tokens",
                    "field": "tokens",
                    "align": "right",
                },
            ]
            rows = [
                {
                    k: node[k]
                    for k in (
                        "kind",
                        "model_class",
                        "shelf",
                        "role",
                        "calls",
                        "cached",
                        "tokens",
                    )
                }
                for node in metrics["nodes"]
            ]
            ui.table(columns=columns, rows=rows).classes("w-full")
        fallback = metrics["usage_fallback"] if metrics is not None else None
        ui.label(
            "Счётчик fallback-оценок токенов chars/4 (usage_fallback_total): "
            f"{fallback if fallback is not None else '—'}"
        ).classes("text-caption text-grey")


def build_queue() -> None:
    """Построить страницу «Очередь» (панель ws-контура).

    Чтение — для всех ролей страницы (min_role contributor); запись
    приоритета — только admin (гейт в render → _render can_manage).
    """
    state: dict[str, Any] = {
        "client": None,
        "snapshot": None,
        "error": None,
        "metrics": None,
        "metrics_error": None,
    }
    _refresh_timer: ui.timer | None = None

    @ui.refreshable
    def render() -> None:
        _render(
            state,
            can_manage=can_manage_priority(current_role()),
            on_refresh=refresh,
        )

    def _refresh_data() -> None:
        """Собрать данные (sync ws-redis) в state; сбои → error-класс."""
        try:
            if state["client"] is None:
                state["client"] = make_ws_redis()
            state["snapshot"] = fetch_queue_snapshot(state["client"], now=time.time())
            state["error"] = None
        except Exception as exc:  # fail-soft панель — см. докстринг модуля
            state["snapshot"] = None
            state["error"] = type(exc).__name__
        _refresh_metrics(state)

    def refresh() -> None:
        _refresh_data()
        try:
            render.refresh()
        except RuntimeError:
            pass  # вкладка скрыта — parent slot deleted; таймер сам остановится

    ui.label("Очередь верстака (ws-контур)").classes("text-h4 q-mb-xs")
    ui.label(
        "Панель планировщика: ожидающие job-ы по полкам, позиции и "
        "ETA-оценка (чтение); admin — управление приоритетами job-ов. "
        f"Обновление каждые {REFRESH_SECONDS:.0f} с."
    ).classes("text-caption text-grey q-mb-md")

    _refresh_data()  # первый сбор синхронно → мгновенный баннер/данные
    render()

    _refresh_timer = ui.timer(REFRESH_SECONDS, refresh)

    def _cleanup() -> None:
        if _refresh_timer is not None:
            _refresh_timer.cancel()
        client = state["client"]
        if client is not None:
            try:
                client.close()
            except Exception:  # очистка on_disconnect — лучшее усилие
                pass

    ui.context.client.on_disconnect(_cleanup)
