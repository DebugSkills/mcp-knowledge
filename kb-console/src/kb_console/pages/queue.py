"""Страница «Очередь» — панель очередей вызовов ws-контура (Ф4.4b).

Read-only витрина планировщика AI-верстака: консоль НЕ импортирует
``ai_workspace`` — читает ws-redis напрямую (прецедент Ф2:
``core/redis_client.make_ws_redis``, как ``pages/chat.py:_build_store``).

Контракт ключей (Ф4.4a, только чтение):
- ``ws:q:{shelf}`` — ZSET call→raw VFT (ожидающие);
- ``ws:call:{shelf}:{call}`` — HASH (prio, class, job, epoch, attempt,
  vft, starve_deadline);
- ``ws:pos:{call}`` — позиция вызова (1-based, меньше = раньше);
  ``ws:posq:{shelf}`` — глубина полки (int);
- ``ws:starve:{shelf}`` — ZSET call→starve_deadline (просроченные =
  aging-пол, приоритетнее всех — инвариант I2);
- ``ws:eta:{shelf}`` — JSON {"ema_s","p95_s","n","updated_at"};
- исполняющиеся: ``ws:lease:{shelf}:{call}``, ``ws:job:{id}`` — в этой
  итерации НЕ читаются (панель показывает ожидающих; остаток — см. отчёт).

Полки: ``local``, ``ext`` (контурные — показываются всегда, даже пустые);
``gpu`` — только при наличии ключей (не выдумываем).

Fail-soft: у операторской консоли ``WS_REDIS_URL`` НЕ задан (env — только
сервису workspace, compose.workspace.yml) → ``RuntimeError``/сетевые сбои
гасятся в баннер «ws-redis недоступен» с КЛАССОМ ошибки (str redis-ошибок
несёт host:port — гигиена core/redis_client.py); страница не падает,
таймер продолжает попытки.

R6: ``components/queue_console.py`` — витрина ИМПОРТ-очереди KB; здесь
общего только слово «очередь» — НЕ переиспользуется и не смешивается.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nicegui import ui

from ..core.redis_client import make_ws_redis

REFRESH_SECONDS = 2.0
"""Авто-обновление панели (паттерн status.py / import_page.py:227-237)."""

BASE_SHELVES: tuple[str, ...] = ("local", "ext")
GPU_SHELF = "gpu"


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
                 "calls": int, "eta_range": (lower, upper)|None}]}]}
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
        shelves_out.append(
            {
                "shelf": shelf,
                "depth": int(raw_depth) if raw_depth is not None else len(waiting),
                "depth_source": "posq" if raw_depth is not None else "waiting",
                "waiting_calls": len(waiting),
                "eta": eta,
                "jobs": _aggregate_jobs(calls, eta),
            }
        )
    return {"ok": True, "now": float(now), "shelves": shelves_out}


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


def _render(state: dict[str, Any]) -> None:
    """Отрисовать состояние панели: баннер ошибки ЛИБО полки с очередями."""
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
                rows.append(
                    {
                        "job": j["job"]
                        + (f" ({j['calls']} вызова)" if j["calls"] > 1 else ""),
                        "prio": j["prio"] or "—",
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


def build_queue() -> None:
    """Построить страницу «Очередь» (панель ws-контура, read-only)."""
    state: dict[str, Any] = {"client": None, "snapshot": None, "error": None}
    _refresh_timer: ui.timer | None = None

    @ui.refreshable
    def render() -> None:
        _render(state)

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

    def refresh() -> None:
        _refresh_data()
        try:
            render.refresh()
        except RuntimeError:
            pass  # вкладка скрыта — parent slot deleted; таймер сам остановится

    ui.label("Очередь верстака (ws-контур)").classes("text-h4 q-mb-xs")
    ui.label(
        "Read-only панель планировщика: ожидающие job-ы по полкам, позиции "
        f"и ETA-оценка. Обновление каждые {REFRESH_SECONDS:.0f} с."
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
