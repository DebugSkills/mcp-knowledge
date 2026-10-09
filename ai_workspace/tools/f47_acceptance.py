"""Приёмочный контурный драйвер Ф4.7 (S1 + S2 + S3).

Запуск: ``make f47-run`` (или ``WS_REDIS_URL=... python -m
ai_workspace.tools.f47_acceptance [--json]``). Выход: ``0`` — ассерты
пройдены; ``1`` — нарушение ассертов (отчёт пишется ВСЕГДА); ``2`` —
контур не поднят (нет WS_REDIS_URL / ping-wait истёк) — fail-closed.

ГРАНИЦА ЧЕСТНОСТИ: прогон КОНТУРНЫЙ, НЕ через живых провайдеров.
LLM-путь заводится в движок стабами полок (``StubShelfLLM``/``StubMCP``
из ``tools/golden_run.py``) — реальные ollama/DeepSeek это шаг прода Ф6
(см. ``tools/golden_run.py:19``). Исполнителя авто-вытеснения (arq-воркер)
тоже нет — движок вызывают потоки драйвера. Поэтому S1 доказывает
МЕХАНИКИ КОНТУРА на реальных компонентах ws-redis:

- admission/квоты D3/D6 (``QuotaWiring.submit`` → Lua ADMIT; deny-проба
  по D3 с предзаписанным дневным расходом); бюджетный D5-парк — сценарий
  S3 ниже (ext-полка с pricing);
- приоритеты D2/D8: ``prio``/``prio_source`` из Decision, разрешённого
  ``QuotaWiring._resolve_priority`` (наблюдение через обёртку, не мок);
- списание по факту usage + возврат conc-резерва терминалом движка
  (``RedisQuotaPort``: heartbeat/charge/release — реальный порт).

Сценарий S2 «Очередь/K/вытеснение в бою»: реальные Lua ``queue.lua``/
``slots.lua`` (полка local, K=1) — WFQ-порядок снятия == ``policy.pick_best``,
K-инвариант (отказ, не очередь), preempt-кредит ``vft − cost_done/w`` со
starve-стабильностью (I2), сервис requeued-вызова. ГРАНИЦА: исполнителя
авто-вытеснения (arq-воркера) нет — роль планировщика-арбитра играет сам
драйвер (явные ``Slots.release`` + ``Queue.preempt``); механики реальные.

Сценарий S3 «park/resume бюджета (D4/D5)»: полка ext (``PricingRegistry``),
реальные ``QuotaWiring.submit``/``ParkControl``: исчерпание бюджета → job
создан и СРАЗУ parked (слот/conc не течёт); resume при всё ещё исчерпанном
→ False; после возврата бюджета → True, вызов возвращается в очередь с
ИСХОДНЫМИ vft/starve. Счётчик месяца выставляется драйвером напрямую
(имитация исчерпания/nightly reconcile), реальных ₽-списаний нет. Что НЕ
доказывается: латентность живых моделей, ретраи шлюзов, REVISE-петля
реального критика, многопроцессный воркер с preempt, реальные ₽-списания.

Сценарий S1 «5 параллельных постановок + квоты» (D1): admin-1 (admin),
member-1/member-2 (member), guest-1/guest-2 (guest); разные job_class и
зоны (private — как в golden_run: режим statya-private). Постановка
конкурентна (потоки + старт-событие), исполнение — реальный ModeEngine
с реальным портом квот; пик RUNNING мерится сэмплером по job-store.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_workspace.artifacts import ArtifactStore, RedisBackend
from ai_workspace.conformance import DECODING_PIN
from ai_workspace.orchestrator.board import BoardStore
from ai_workspace.orchestrator.engine import LLMResult, ModeEngine, load_mode
from ai_workspace.orchestrator.job import JobState, JobStore
from ai_workspace.orchestrator.ledger import RedisLedger
from ai_workspace.redis_client import make_ws_redis
from ai_workspace.registry import Registry
from ai_workspace.registry.pricing import PricingRegistry
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler import Queue, policy
from ai_workspace.scheduler.admission import (
    AdmissionDenied,
    QuotaRedisUnavailable,
    budget_global_key,
    budget_user_key,
    tok_key,
)
from ai_workspace.scheduler.park import ParkControl, pos_key
from ai_workspace.scheduler.slots import Slots
from ai_workspace.scheduler.wiring import QuotaWiring
from ai_workspace.tools.golden_run import StubMCP, StubShelfLLM

AI_WORKSPACE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AI_WORKSPACE_DIR.parent
DEFAULT_MODE = AI_WORKSPACE_DIR / "modes" / "statya.yaml"
DEFAULT_PRIVATE_MODE = AI_WORKSPACE_DIR / "modes" / "statya-private.yaml"
DEFAULT_REPORT = (
    REPO_ROOT / "plans" / "_provenance" / "arch-2026-10-05-ai-workspace"
    / "Ф4.7-acceptance-report.md"
)

S1_PARTICIPANTS: tuple[dict[str, str], ...] = (
    {"user": "admin-1", "account_level": "admin", "job_class": "interactive", "zone": "public"},
    {"user": "member-1", "account_level": "member", "job_class": "batch", "zone": "public"},
    {"user": "member-2", "account_level": "member", "job_class": "interactive", "zone": "private"},
    {"user": "guest-1", "account_level": "guest", "job_class": "background", "zone": "public"},
    {"user": "guest-2", "account_level": "guest", "job_class": "interactive", "zone": "public"},
)
"""5 участников (D1): admin/member×2/guest×2, разные job_class и зоны."""

DENY_USER = "guest-deny"
"""Отдельный участник deny-проб D3 (не смешивается с основными пятью)."""

S1_BRIEF = "Статья про MCP-сервер: архитектура, инструменты, примеры использования."
S1_ANSWER = (
    "Статья про MCP: структура и разделы, архитектура клиент-сервер, "
    "инструменты и ресурсы, примеры интеграции и выводы."
)

LLM_DELAY_S = 0.25
"""Задержка стаб-вызова: гарантировать перекрытие RUNNING (измерение
пиковой параллельности, НЕ латентности; механику контура не меняет)."""

SAMPLE_INTERVAL_S = 0.02
PING_TIMEOUT_S = 30.0
MAX_GATE_ROUNDS = 8


def _local_day() -> str:
    """Локальная дата дневного ключа квот (та же семантика, что admission)."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


@dataclass
class WorkspaceContext:
    """Собранный контур S1: клиент, реестры, wiring, сторы, графы режимов."""

    client: Any
    registry: Registry
    quota_registry: QuotaRegistry
    wiring: QuotaWiring
    jobs: JobStore
    ledger: RedisLedger
    graphs: dict[str, Any] = field(default_factory=dict)
    prio_log: dict[str, dict[str, str | None]] = field(default_factory=dict)
    """job_id → {prio, prio_source} из РЕАЛЬНОГО Decision (обёртка-наблюдатель)."""


def build_context(
    redis_url: str,
    *,
    shelf: str = "local",
    mode: Path = DEFAULT_MODE,
    private_mode: Path = DEFAULT_PRIVATE_MODE,
) -> WorkspaceContext:
    """Собрать контур на реальных компонентах (паттерн golden_run + ws_quota_sweep).

    - ``Registry`` — hot-reload по mtime; ``QuotaRegistry`` оборачивает его
      (точная строка — ``scripts/ws_quota_sweep.py``: QuotaWiring(client,
      registry=QuotaRegistry(Registry(...))));
    - идемпотентность повторных прогонов: снос ключей job'ов/квот участников;
    - ``prio_log`` — обёртка НАД ``wiring._resolve_priority`` (вызов реальный,
      перехватывается только результат Decision для отчёта).
    """
    client = make_ws_redis(redis_url)
    registry = Registry(AI_WORKSPACE_DIR / "registry")
    quota_registry = QuotaRegistry(registry)
    wiring = QuotaWiring(client, registry=quota_registry, shelf=shelf)
    ctx = WorkspaceContext(
        client=client,
        registry=registry,
        quota_registry=quota_registry,
        wiring=wiring,
        jobs=JobStore(client),
        ledger=RedisLedger(client),
        graphs={"public": load_mode(mode), "private": load_mode(private_mode)},
    )

    original = wiring._resolve_priority

    def _capture(decision, user, account_level, job_id, job_prio):  # type: ignore[no-untyped-def]
        resolved = original(decision, user, account_level, job_id, job_prio)
        ctx.prio_log[job_id] = {"prio": resolved.prio, "prio_source": resolved.prio_source}
        return resolved

    wiring._resolve_priority = _capture  # type: ignore[method-assign]

    job_ids = [f"f47-s1-{p['user']}" for p in S1_PARTICIPANTS]
    job_ids.append("f47-s1-deny-probe")
    users = [p["user"] for p in S1_PARTICIPANTS] + [DENY_USER]
    _cleanup_keys(client, users, job_ids)
    return ctx


def _cleanup_keys(client: Any, users: list[str], job_ids: list[str]) -> None:
    """Снести хвосты прошлого прогона (job/board/fx/resume + квоты участников)."""
    for job_id in job_ids:
        stale = [
            f"ws:job:{job_id}",
            f"ws:board:{job_id}",
            f"ws:board:{job_id}:owner",
            f"ws:fx:{job_id}",
            f"ws:resume:{job_id}",
        ]
        stale += list(client.scan_iter(match=f"ws:board:{job_id}:v:*"))
        if stale:
            client.delete(*stale)
    for user in users:
        keys = [f"ws:quota:conc:{user}", f"ws:quota:conchold:{user}"]
        keys += list(client.scan_iter(match=f"ws:quota:tok:{user}:*"))
        keys += list(client.scan_iter(match=f"ws:quota:conclease:{user}:*"))
        if keys:
            client.delete(*keys)


def _ping_wait(client: Any, timeout_s: float = PING_TIMEOUT_S) -> bool:
    """Ждать готовности ws-redis (без этого suite молча скипает интеграцию)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.ping():
                return True
        except Exception:  # noqa: BLE001 — retry до таймаута
            time.sleep(0.5)
    return False


class _SlowStubShelfLLM(StubShelfLLM):
    """``StubShelfLLM`` с задержкой: гарантировать перекрытие RUNNING-состояний.

    Без задержки стаб отрабатывает быстрее, чем поток успевает зайти в
    ``engine.run`` — пик параллельности не наблюдаем. Задержка живёт в
    стабе провайдера (его «латентность»), контур остаётся реальным.
    """

    delay_s: float = LLM_DELAY_S

    def complete(
        self,
        *,
        role: str,
        model_class: str,
        prompt: str,
        inputs: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        job_id: str | None = None,
    ) -> LLMResult:
        time.sleep(self.delay_s)
        return super().complete(
            role=role,
            model_class=model_class,
            prompt=prompt,
            inputs=inputs,
            params=params,
            job_id=job_id,
        )


class _InFlightSampler(threading.Thread):
    """Сэмплер пика RUNNING по job-store (наблюдение стора, не наших потоков)."""

    def __init__(self, jobs: JobStore, job_users: dict[str, str]) -> None:
        super().__init__(daemon=True)
        self._jobs = jobs
        self._job_users = job_users
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.peak = 0
        self.peak_users: frozenset[str] = frozenset()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            running: list[str] = []
            for job_id, user in self._job_users.items():
                with contextlib.suppress(Exception):  # job ещё/уже нет в сторе
                    if self._jobs.get(job_id).state is JobState.RUNNING:
                        running.append(user)
            with self._lock:
                if len(running) > self.peak:
                    self.peak = len(running)
                    self.peak_users = frozenset(running)
            self._stop_evt.wait(SAMPLE_INTERVAL_S)

    def stop_and_join(self) -> None:
        self._stop_evt.set()
        self.join(timeout=10.0)


def _submit_participant(ctx: WorkspaceContext, p: Mapping[str, str], barrier_start: threading.Event,
                        results: dict[str, dict[str, Any]], results_lock: threading.Lock) -> None:
    """Поток постановки: РЕАЛЬНЫЙ ``QuotaWiring.submit`` (не мок)."""
    barrier_start.wait()
    job_id = f"f47-s1-{p['user']}"
    zone = p["zone"]
    entry: dict[str, Any] = {
        "user": p["user"],
        "account_level": p["account_level"],
        "job_class": p["job_class"],
        "zone": zone,
        "mode": ctx.graphs[zone].doc["id"],
        "job_id": job_id,
        "submit": "error",
        "deny_code": None,
        "error": None,
    }
    try:
        rec = ctx.wiring.submit(
            user=p["user"],
            account_level=p["account_level"],
            job_class=p["job_class"],
            mode=entry["mode"],
            zone=zone,
            job_id=job_id,
        )
        entry["submit"] = "parked" if rec.state is JobState.PARKED else "admitted"
    except AdmissionDenied as exc:
        entry["submit"] = "denied"
        entry["deny_code"] = exc.code
        entry["error"] = exc.message
    except Exception as exc:  # noqa: BLE001 — любой сбой постановки фиксируем
        entry["submit"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"
    with results_lock:
        results[p["user"]] = entry


def _run_one_job(ctx: WorkspaceContext, port: Any, shelf: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Прогнать admitted job реальным ModeEngine до терминала (HITL авто-approve)."""
    job_id = entry["job_id"]
    llm = _SlowStubShelfLLM(shelf, S1_ANSWER)
    # В1a.1/В1a.2 Ф7 (arch-2026-10-08-f7-calibration): активный профиль
    # калибровки для конструктора движка. Селектор — реестр model_classes
    # (active_profile) того же реестра, что у движка, факт полки — провайдер
    # (ollama /api/tags, Э2-2); нет профиля → (None, None) → паритет F1.
    # Import ЛОКАЛЬНЫЙ — единообразие с golden_run/vp_ab_pilot (модель
    # импорт-циклов описана в ai_workspace/calibration/runtime.py).
    from ai_workspace.calibration.model_facts import urllib_http_get
    from ai_workspace.calibration.runtime import DEFAULT_PROFILES_DIR, active_calibration

    cal_profile, cal_facts = active_calibration(
        ctx.registry.dir, DEFAULT_PROFILES_DIR, http_get=urllib_http_get,
    )
    engine = ModeEngine(
        jobs=ctx.jobs,
        boards=BoardStore(ctx.client, job_id),
        graph=ctx.graphs[entry["zone"]],
        llm=llm,
        mcp=StubMCP(),
        ledger=ctx.ledger,
        artifacts=ArtifactStore(RedisBackend(ctx.client)),
        registry=ctx.registry,
        decoding=DECODING_PIN,
        quota=port,
        # В1a.2 Ф7: профиль/факты калибровки (см. выше); (None, None)
        # без active_profile — паритет F1.
        calibration_profile=cal_profile,
        calibration_model_facts=cal_facts,
    )
    engine.seed(job_id, {"brief": S1_BRIEF}, epoch=1)
    step = engine.run(job_id, epoch=1)
    rounds = 0
    while step.status == "paused" and step.resume_token:
        rounds += 1
        if rounds > MAX_GATE_ROUNDS:
            raise RuntimeError(f"{job_id}: human-gate не завершаются ({rounds} раундов)")
        step = engine.resume(job_id, epoch=1, token=step.resume_token, decision="approve")
    return {
        "final_status": step.status,
        "final_state": ctx.jobs.get(job_id).state.value,
        "detail": step.detail,
        "llm_calls": len(llm.calls),
        "gate_rounds": rounds,
    }


def _deny_probe(ctx: WorkspaceContext) -> dict[str, Any]:
    """Deny-проба D3: guest с предзаписанным расходом = лимиту дня.

    Ключ ``ws:quota:tok:{user}:{day}`` пишется клиентом ДО постановки
    (SMALL-трик; quotas.yaml НЕ правится). Ожидание — ``AdmissionDenied``
    с кодом ``quota_tokens_exhausted`` (Lua ADMIT: spent >= limit → deny).
    """
    day = _local_day()
    limit = ctx.quota_registry.quota_for("guest").tokens_per_day
    ctx.client.set(tok_key(DENY_USER, day), str(limit))
    try:
        ctx.wiring.submit(
            user=DENY_USER,
            account_level="guest",
            job_class="interactive",
            mode=ctx.graphs["public"].doc["id"],
            zone="public",
            job_id="f47-s1-deny-probe",
        )
    except AdmissionDenied as exc:
        return {
            "user": DENY_USER,
            "preset_spent": limit,
            "expected_code": "quota_tokens_exhausted",
            "actual_code": exc.code,
            "ok": exc.code == "quota_tokens_exhausted",
            "message": exc.message,
        }
    return {
        "user": DENY_USER,
        "preset_spent": limit,
        "expected_code": "quota_tokens_exhausted",
        "actual_code": None,
        "ok": False,
        "message": "постановка ПРОШЛА — deny по D3 не сработал (дефект контура?)",
    }


def s1_parallel_jobs(ctx: WorkspaceContext, *, workers: int, shelf: str) -> dict[str, Any]:
    """S1 «5 параллельных постановок + квоты»: submit → engine → метрики."""
    workers = max(1, workers)
    n = len(S1_PARTICIPANTS)

    # ── фаза A: конкурентная постановка (старт-событие, по 1 job на участника)
    start = threading.Event()
    results: dict[str, dict[str, Any]] = {}
    results_lock = threading.Lock()
    threads = [
        threading.Thread(target=_submit_participant, args=(ctx, p, start, results, results_lock))
        for p in S1_PARTICIPANTS
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)  # все потоки дошли до старт-события
    t0 = time.monotonic()
    start.set()
    for t in threads:
        t.join(timeout=60.0)
    submit_seconds = round(time.monotonic() - t0, 3)

    rows = [results[p["user"]] for p in S1_PARTICIPANTS]
    admitted = [r for r in rows if r["submit"] in ("admitted", "parked")]
    totals = {
        "submitted": n,
        "admitted": sum(1 for r in rows if r["submit"] == "admitted"),
        "parked": sum(1 for r in rows if r["submit"] == "parked"),
        "denied": sum(1 for r in rows if r["submit"] == "denied"),
        "errors": sum(1 for r in rows if r["submit"] == "error"),
    }

    # ── фаза B: исполнение admitted job'ов (потоки, реальный порт квот)
    run_outcomes: dict[str, dict[str, Any]] = {}
    run_lock = threading.Lock()
    sampler = _InFlightSampler(ctx.jobs, {r["job_id"]: r["user"] for r in admitted})
    if admitted:
        port = ctx.wiring.make_port()  # stateless — один на все потоки
        sem = threading.Semaphore(workers)
        engine_start = threading.Event()

        def _worker(entry: dict[str, Any]) -> None:
            with sem:
                engine_start.wait()
                try:
                    outcome = _run_one_job(ctx, port, shelf, entry)
                except Exception as exc:  # noqa: BLE001 — фиксируем сбой прогона
                    outcome = {
                        "final_status": "error",
                        "final_state": "error",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "llm_calls": 0,
                        "gate_rounds": 0,
                    }
                with run_lock:
                    run_outcomes[entry["user"]] = outcome

        workers_threads = [threading.Thread(target=_worker, args=(e,)) for e in admitted]
        sampler.start()
        for t in workers_threads:
            t.start()
        time.sleep(0.05)
        t1 = time.monotonic()
        engine_start.set()
        for t in workers_threads:
            t.join(timeout=120.0)
        run_seconds = round(time.monotonic() - t1, 3)
        sampler.stop_and_join()
    else:
        port = None
        run_seconds = 0.0

    # ── метрики: квоты D3, приоритеты D2/D8, остатки, резерв после терминала
    day = _local_day()
    tokens_sum = 0
    for row in rows:
        outcome = run_outcomes.get(row["user"])
        if outcome:
            row.update(outcome)
        quota = ctx.quota_registry.quota_for(row["account_level"])
        row["expected_prio"] = quota.priority
        row["conc_limit"] = quota.conc
        row["tokens_per_day"] = quota.tokens_per_day
        prio = ctx.prio_log.get(row["job_id"])
        row["prio"] = prio["prio"] if prio else None
        row["prio_source"] = prio["prio_source"] if prio else None
        if row["submit"] in ("admitted", "parked"):
            usage = ctx.ledger.get(row["job_id"], "usage") or {}
            row["tokens"] = int(usage.get("tokens", 0))
            row["charged"] = int(usage.get("charged", 0))
            tokens_sum += row["tokens"]
            spent = ctx.client.get(tok_key(row["user"], day))
            row["tokens_spent_day"] = int(spent) if spent else 0
            row["tokens_remaining"] = (
                None if quota.tokens_per_day is None
                else quota.tokens_per_day - row["tokens_spent_day"]
            )
            conc_after = ctx.client.get(f"ws:quota:conc:{row['user']}")
            row["conc_inflight_after"] = int(conc_after) if conc_after else 0
        else:
            row["tokens"] = row["charged"] = 0
            row["tokens_spent_day"] = row["tokens_remaining"] = None
            row["conc_inflight_after"] = None

    deny = _deny_probe(ctx)

    # ── ассерты (fail → ok=False → exit 1; отчёт пишется всегда)
    expected_peak = min(workers, totals["admitted"])
    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    _check(
        "all_admitted",
        totals["admitted"] == n and totals["denied"] == 0 and totals["parked"] == 0
        and totals["errors"] == 0,
        f"admitted={totals['admitted']}/{n}, denied={totals['denied']}, "
        f"parked={totals['parked']}, errors={totals['errors']}",
    )
    _check(
        "max_in_flight",
        sampler.peak == expected_peak,
        f"пик RUNNING={sampler.peak}, ожидание={expected_peak} "
        f"(workers={workers}, admitted={totals['admitted']}); "
        f"пик у: {sorted(sampler.peak_users) or '—'}",
    )
    prio_bad = [
        f"{r['user']}: {r['prio']}/{r['prio_source']} (ожидалось {r['expected_prio']}/account)"
        for r in rows
        if r["submit"] == "admitted"
        and (r["prio"] != r["expected_prio"] or r["prio_source"] != "account")
    ]
    _check(
        "priorities_D2",
        not prio_bad,
        "; ".join(prio_bad) or "у всех admitted prio=priority аккаунта (D2), source=account (D8)",
    )
    _check(
        "deny_probe_D3",
        deny["ok"],
        f"код={deny['actual_code']!r}, ожидание={deny['expected_code']!r}",
    )
    _check("tokens_charged", tokens_sum > 0, f"сумма tokens по admitted: {tokens_sum}")
    finals_bad = [
        f"{r['user']}: {r.get('final_status')}/{r.get('final_state')}"
        for r in rows
        if r["submit"] == "admitted" and r.get("final_status") != "done"
    ]
    _check("all_done", not finals_bad, "; ".join(finals_bad) or "все admitted завершились done")
    conc_bad = [
        f"{r['user']}: conc={r['conc_inflight_after']}"
        for r in rows
        if r["submit"] == "admitted" and (r["conc_inflight_after"] or 0) != 0
    ]
    _check(
        "conc_released",
        not conc_bad,
        "; ".join(conc_bad) or "после терминалов резервы D6 возвращены (conc=0 у всех)",
    )

    return {
        "name": "S1 · 5 параллельных постановок + квоты",
        "shelf": shelf,
        "workers": workers,
        "submit_seconds": submit_seconds,
        "run_seconds": run_seconds,
        "participants": rows,
        "totals": {**totals, "tokens_sum": tokens_sum},
        "max_in_flight": sampler.peak,
        "max_in_flight_expected": expected_peak,
        "max_in_flight_users": sorted(sampler.peak_users),
        "deny_probe": deny,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


# ── S2: очередь/K/вытеснение в бою (реальные Lua, полка local, K=1) ─────


S2_SHELF = "local"
"""Полка S2. Общий namespace (как ext): уборка — ТОЧНЫМ списком ключей,
паттерн ``_cleanup_shelf`` из ``tests/test_park_resume.py`` (без scan-паттерна
по подстроке — переложил бы чужие ключи)."""

S2_SPECS: tuple[tuple[str, str, str, float], ...] = (
    ("ih1", "high", "interactive", 3.0),
    ("ih2", "high", "interactive", 2.0),
    ("bm1", "med", "batch", 3.0),
    ("bm2", "med", "batch", 2.0),
    ("bl1", "low", "background", 4.0),
)
"""5 вызовов (2×interactive/high, 2×batch/med, 1×background/low): ожидаемый
порядок обслуживания — интерактив раньше batch раньше background (WFQ по
vft: base 100/10/1 × mult 4/2/1)."""

S2_NOW0 = 1000.0
"""Фиктивные часы S2 (``now`` инъектируется явно — паттерн тестов Ф3.4):
starve-дедлайны (60с/30м/2ч) заведомо НЕ просрочены — порядок чисто WFQ."""


def _cleanup_s2(client: Any, calls: list[str]) -> None:
    """Снести хвосты прошлого прогона S2 (очередь/слоты/per-call записи).

    Полка ``local`` — общий namespace: точный список ключей (q/starve/vt/
    slots/events/posidx/vftlast использованных (p,c)-пар + ключи вызовов).
    """
    keys = [
        f"ws:q:{S2_SHELF}",
        f"ws:starve:{S2_SHELF}",
        f"ws:vt:{S2_SHELF}",
        f"ws:slots:{S2_SHELF}",
        f"ws:events:{S2_SHELF}",
        f"ws:posidx:{S2_SHELF}",
        f"ws:vftlast:{S2_SHELF}:high:interactive",
        f"ws:vftlast:{S2_SHELF}:med:batch",
        f"ws:vftlast:{S2_SHELF}:low:background",
    ]
    keys += [f"ws:call:{S2_SHELF}:{c}" for c in calls]
    keys += [f"ws:lease:{S2_SHELF}:{c}" for c in calls]
    keys += [f"ws:pos:{c}" for c in calls]
    client.delete(*keys)


def s2_queue_preempt(ctx: WorkspaceContext) -> dict[str, Any]:
    """S2 «Очередь/K/вытеснение в бою»: реальные Lua queue/slots, K=1.

    ГРАНИЦА ЧЕСТНОСТИ: исполнителя авто-вытеснения (arq-воркера) нет —
    роль планировщика-арбитра играет сам драйвер: он явно вызывает
    ``Slots.release`` + ``Queue.preempt`` и отдаёт слот interactive-вызову.
    Механики при этом РЕАЛЬНЫЕ: Lua queue.lua/slots.lua на живом ws-redis.
    """
    client = ctx.client
    q = Queue(client, shelf=S2_SHELF)
    slots = Slots(client, shelf=S2_SHELF, k=1)
    jobs = [f"f47-s2-{spec[0]}" for spec in S2_SPECS]
    battle_jobs = ["f47-s2-batch-x", "f47-s2-hi-x", "f47-s2-overk"]
    all_calls = [Queue.make_call(j, 0) for j in jobs + battle_jobs]
    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        _cleanup_s2(client, all_calls)

        # ── фаза A: 5 вызовов разного (prio, class); now инъектируется —
        # vft/starve детерминированы (паттерн тестов Ф3.4)
        records: dict[str, dict[str, Any]] = {}
        now = S2_NOW0
        for tag, prio, call_class, cost_est in S2_SPECS:
            job = f"f47-s2-{tag}"
            call = Queue.make_call(job, 0)
            vft = q.enqueue(
                call, prio=prio, call_class=call_class, cost_est=cost_est,
                now=now, job=job, epoch=1,
            )
            records[call] = {
                "call": call,
                "job": job,
                "prio": prio,
                "class": call_class,
                "cost_est": cost_est,
                "vft": vft,
                "starve_deadline": now + policy.T_STARVE[call_class],
            }
            now += 0.1

        # ожидаемый порядок — итеративный pick_best по тем же данным, что
        # видит Lua (vft/starve из per-call записей; complete() vt не меняет
        # vft уже стоящих — порядок стабилен между снятиями)
        remaining = dict(records)
        expected: list[str] = []
        while remaining:
            best = policy.pick_best(list(remaining.values()), now=now)
            expected.append(best)
            del remaining[best]

        taken: list[str] = []
        peak_in_flight = 0
        refusal_over_k: bool | None = None
        refusal_dequeue_full: bool | None = None
        queue_intact_on_refusal: bool | None = None
        for i, want in enumerate(expected):
            got = q.dequeue_and_acquire(k_max=1, now=now)
            taken.append(got[0] if got else None)
            peak_in_flight = max(peak_in_flight, slots.used())
            if i == 0:
                # слот ЗАНЯТ (K=1): прямой acquire сверх K и повторное
                # снятие — отказ ДО любых записей (I1), очередь нетронута
                refusal_over_k = (
                    slots.acquire(Queue.make_call("f47-s2-overk", 0)) is False
                )
                size_before = q.size()
                refusal_dequeue_full = q.dequeue_and_acquire(k_max=1, now=now) == []
                queue_intact_on_refusal = q.size() == size_before
            holder = got[0] if got else None
            if holder is not None:
                rec = records[holder]
                slots.release(holder)  # терминал воркера: слот назад + complete
                q.complete(
                    prio=rec["prio"], call_class=rec["class"],
                    cost_actual=rec["cost_est"], call=holder,
                )

        # ── фаза B: вытеснение в бою — слот держит batch, «приходит»
        # interactive; драйвер играет роль арбитра (arq-воркера нет)
        b_job, i_job = "f47-s2-batch-x", "f47-s2-hi-x"
        b_call = Queue.make_call(b_job, 0)
        i_call = Queue.make_call(i_job, 0)
        b_cost, b_done = 3.0, 1.5  # 0 < cost_done < cost_est
        vft_before = q.enqueue(
            b_call, prio="med", call_class="batch", cost_est=b_cost,
            now=now, job=b_job, epoch=1,
        )
        starve_before = client.zscore(q.starve_key, b_call)
        slot_held_by_batch = q.dequeue_and_acquire(k_max=1, now=now) == [b_call]
        peak_in_flight = max(peak_in_flight, slots.used())
        i_vft = q.enqueue(
            i_call, prio="high", call_class="interactive", cost_est=0.05,
            now=now, job=i_job, epoch=1,
        )
        # арбитр: воркер освобождает слот, очередь re-enqueue'ит batch с
        # кредитом за сделанное (preempt), затем снятие в пользу interactive
        released = slots.release(b_call)
        preempt_ok = q.preempt(b_call, cost_done=b_done, cost_est=b_cost)
        vft_after = client.zscore(q.q_key, b_call)
        starve_after = client.zscore(q.starve_key, b_call)
        in_both = vft_after is not None and starve_after is not None
        credit_expected = vft_before - b_done / policy.weight("med", "batch")
        got_i = q.dequeue_and_acquire(k_max=1, now=now)
        interactive_got_slot = got_i == [i_call]
        peak_in_flight = max(peak_in_flight, slots.used())
        # воркер interactive завершил → следующим сервируется вытесненный batch
        slots.release(i_call)
        q.complete(
            prio="high", call_class="interactive", cost_actual=0.05, call=i_call
        )
        got_b = q.dequeue_and_acquire(k_max=1, now=now)
        requeued_served_next = got_b == [b_call]
        peak_in_flight = max(peak_in_flight, slots.used())
        slots.release(b_call)
        q.complete(
            prio="med", call_class="batch", cost_actual=b_cost - b_done,
            call=b_call,
        )
        holders_after = slots.used()

        order_ok = taken == expected
        class_seq = [records[c]["class"] for c in expected]
        _check(
            "dequeue_order_pick_best",
            order_ok,
            f"снятие {taken} == pick_best {expected}",
        )
        _check(
            "class_order",
            class_seq == ["interactive", "interactive", "batch", "batch",
                          "background"],
            f"классы в порядке снятия: {class_seq}",
        )
        _check(
            "k_invariant_refusal",
            bool(refusal_over_k) and bool(refusal_dequeue_full)
            and bool(queue_intact_on_refusal),
            f"acquire сверх K={refusal_over_k}, dequeue при занятом слоте "
            f"пуст: {refusal_dequeue_full}, очередь нетронута: "
            f"{queue_intact_on_refusal}",
        )
        _check(
            "peak_in_flight_le_k",
            peak_in_flight == 1,
            f"пик in-flight слотов {peak_in_flight} (K=1, никогда > K)",
        )
        _check(
            "preempt_credit",
            preempt_ok and vft_after is not None and vft_after < vft_before
            and abs(vft_after - credit_expected) < 1e-9,
            f"preempt={preempt_ok}, vft {vft_before!r} → {vft_after!r} "
            f"(кредит vft−cost_done/w = {credit_expected!r})",
        )
        _check(
            "preempt_starve_stable_both_indices",
            in_both and starve_after is not None
            and abs(starve_after - starve_before) < 1e-9,
            f"в обоих индексах (ws:q+ws:starve)={in_both}, starve "
            f"{starve_before!r} → {starve_after!r} (I2, без сдвига)",
        )
        _check(
            "interactive_got_slot",
            slot_held_by_batch and interactive_got_slot,
            f"слот держал batch={slot_held_by_batch}, после preempt "
            f"interactive взял слот={interactive_got_slot}",
        )
        _check(
            "requeued_served_next",
            requeued_served_next,
            "вытесненный batch-вызов сервируется следующим dequeue",
        )
        return {
            "name": "S2 · очередь/K/вытеснение в бою",
            "shelf": S2_SHELF,
            "k": 1,
            "calls": len(S2_SPECS),
            "expected_order": expected,
            "taken_order": taken,
            "class_order": class_seq,
            "refusal_over_k": refusal_over_k,
            "refusal_dequeue_full": refusal_dequeue_full,
            "queue_intact_on_refusal": queue_intact_on_refusal,
            "peak_in_flight": peak_in_flight,
            "battle": {
                "batch_call": b_call,
                "interactive_call": i_call,
                "slot_held_by_batch": slot_held_by_batch,
                "cost_est": b_cost,
                "cost_done": b_done,
                "vft_before": vft_before,
                "vft_after": vft_after,
                "credit_expected": credit_expected,
                "starve_before": starve_before,
                "starve_after": starve_after,
                "in_both_indices": in_both,
                "released": released,
                "preempt_ok": preempt_ok,
                "interactive_vft": i_vft,
                "interactive_got_slot": interactive_got_slot,
                "requeued_served_next": requeued_served_next,
            },
            "holders_after": holders_after,
            "checks": checks,
            "ok": all(c["ok"] for c in checks),
        }
    finally:
        _cleanup_s2(client, all_calls)


# ── S3: park/resume бюджета D4/D5 (полка ext, PricingRegistry) ──────────


S3_SHELF = "ext"
S3_USER = "f47-s3-member"
S3_JOB = "f47-s3-member-j1"


def _cleanup_s3(client: Any, call: str) -> None:
    """Снести хвосты прошлого прогона S3 (ext — общий namespace: точный
    список ключей, паттерн ``_cleanup_shelf`` из ``tests/test_park_resume.py``)."""
    keys = [
        f"ws:q:{S3_SHELF}",
        f"ws:starve:{S3_SHELF}",
        f"ws:vt:{S3_SHELF}",
        f"ws:slots:{S3_SHELF}",
        f"ws:events:{S3_SHELF}",
        f"ws:posidx:{S3_SHELF}",
        f"ws:vftlast:{S3_SHELF}:med:interactive",
        f"ws:call:{S3_SHELF}:{call}",
        f"ws:lease:{S3_SHELF}:{call}",
        f"ws:pos:{call}",
        pos_key(S3_JOB),
        f"ws:job:{S3_JOB}",
        f"ws:board:{S3_JOB}",
        f"ws:board:{S3_JOB}:owner",
        f"ws:resume:{S3_JOB}",
        f"ws:fx:{S3_JOB}",
        f"ws:prio:{S3_JOB}",
        budget_user_key(S3_USER),
        f"ws:quota:conc:{S3_USER}",
        f"ws:quota:conchold:{S3_USER}",
    ]
    keys += list(client.scan_iter(match=f"ws:board:{S3_JOB}:v:*"))
    keys += list(client.scan_iter(match=f"ws:quota:tok:{S3_USER}:*"))
    keys += list(client.scan_iter(match=f"ws:quota:conclease:{S3_USER}:*"))
    client.delete(*keys)


def s3_budget_park_resume(ctx: WorkspaceContext) -> dict[str, Any]:
    """S3 «park/resume бюджета (D4/D5)»: полка ext, реальные submit/ParkControl.

    Исчерпание бюджета имитируется прямой записью счётчика месяца
    (``ws:budget:global:{Y-M}`` = limit_micro, микро-₽; паттерн
    ``test_budget.py``/``test_park_resume.py``); реальных списаний
    ``charge_budget`` в сценарии нет (движок не гоняется). Исходное значение
    счётчика восстанавливается в finally (shared-ключ тест-контура).
    """
    client = ctx.client
    call = Queue.make_call(S3_JOB, 0)
    month_key = budget_global_key()
    prev_budget = client.get(month_key)
    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        _cleanup_s3(client, call)
        pricing = PricingRegistry(ctx.registry)
        wiring_ext = QuotaWiring(
            client, registry=ctx.quota_registry, shelf=S3_SHELF, pricing=pricing
        )
        pc = ParkControl(client, shelf=S3_SHELF, store=ctx.jobs)
        q = Queue(client, shelf=S3_SHELF)
        limit_micro = ctx.quota_registry.budgets["ext"].limit_micro
        mode_id = ctx.graphs["public"].doc["id"]

        # 1) бюджет исчерпан → submit: job создан и СРАЗУ parked (D5),
        #    слот не течёт, conc-резерв не берётся
        client.set(month_key, limit_micro)
        rec = wiring_ext.submit(
            user=S3_USER, account_level="member", job_class="interactive",
            mode=mode_id, zone="public", job_id=S3_JOB,
        )
        park_ok = rec.state is JobState.PARKED
        slots_ext_after_park = int(client.scard(f"ws:slots:{S3_SHELF}"))
        conc_after_park = client.get(f"ws:quota:conc:{S3_USER}")

        # 2) resume при всё ещё исчерпанном бюджете → False, job остаётся
        #    parked, записей нет (admission всё ещё отдаёт park)
        resume_blocked = pc.resume(S3_JOB, registry=ctx.quota_registry) is False
        still_parked = ctx.jobs.get(S3_JOB).state is JobState.PARKED
        queue_empty = q.size() == 0

        # 3) бюджет вернулся (nightly reconcile D4) → resume → True: queued
        client.set(month_key, limit_micro - 1)
        resume_ok = pc.resume(S3_JOB, registry=ctx.quota_registry) is True
        state_after = ctx.jobs.get(S3_JOB).state.value
        job_epoch = ctx.jobs.get(S3_JOB).epoch
        vft_orig = q.enqueue(
            call, prio="med", call_class="interactive", cost_est=1.0,
            now=S2_NOW0, job=S3_JOB, epoch=job_epoch,
        )
        starve_orig = client.zscore(q.starve_key, call)

        # 4) бюджет снова исчерпан → park С вызовом: изъят из обоих индексов,
        #    per-call кредит (vft/starve) сохранён для resume
        client.set(month_key, limit_micro)
        park_with_call = pc.park(S3_JOB, call=call, reason="budget_ext_exhausted")
        call_gone = (
            client.zscore(q.q_key, call) is None
            and client.zscore(q.starve_key, call) is None
        )
        credit_kept = q.call_record(call)

        # 5) resume с вызовом: при исчерпанном — False (без записей); после
        #    возврата бюджета — True: вызов в очереди с ИСХОДНЫМИ vft/starve
        resume_blocked2 = (
            pc.resume(S3_JOB, registry=ctx.quota_registry, call=call) is False
        )
        queue_empty2 = q.size() == 0
        client.set(month_key, limit_micro - 1)
        resume_ok2 = pc.resume(S3_JOB, registry=ctx.quota_registry, call=call) is True
        state_after2 = ctx.jobs.get(S3_JOB).state.value
        vft_restored = client.zscore(q.q_key, call)
        starve_restored = client.zscore(q.starve_key, call)
        served = q.dequeue(now=S2_NOW0 + 10.0, limit=3) == [call]
    finally:
        slots_ext_after = int(client.scard(f"ws:slots:{S3_SHELF}"))
        _cleanup_s3(client, call)
        if prev_budget is None:
            client.delete(month_key)
        else:
            client.set(month_key, prev_budget)

    _check(
        "park_on_exhausted_submit",
        park_ok and slots_ext_after_park == 0 and conc_after_park is None,
        f"state={rec.state.value}, ws:slots:ext={slots_ext_after_park} (пуст), "
        f"conc-резерв не взят ({conc_after_park is None})",
    )
    _check(
        "resume_blocked_while_exhausted",
        resume_blocked and still_parked and queue_empty and resume_blocked2
        and queue_empty2,
        f"resume(call=None)={resume_blocked} (job остался parked="
        f"{still_parked}), resume(с вызовом)={resume_blocked2}; очередь "
        f"пуста в обоих случаях ({queue_empty}/{queue_empty2})",
    )
    _check(
        "resume_ok_after_release",
        resume_ok and resume_ok2
        and state_after == JobState.QUEUED.value
        and state_after2 == JobState.QUEUED.value,
        f"resume#1={resume_ok} (state={state_after}), "
        f"resume#2={resume_ok2} (state={state_after2})",
    )
    _check(
        "park_with_call_removes_indices",
        park_with_call is True and call_gone and bool(credit_kept),
        f"park={park_with_call}, вызов изъят из q+starve={call_gone}, "
        f"per-call кредит сохранён={bool(credit_kept)}",
    )
    _check(
        "vft_starve_preserved_on_resume",
        vft_restored is not None and abs(vft_restored - vft_orig) < 1e-9
        and starve_restored is not None
        and abs(starve_restored - starve_orig) < 1e-9,
        f"vft {vft_orig!r} → {vft_restored!r}, starve {starve_orig!r} → "
        f"{starve_restored!r} (I2: приоритет не теряется)",
    )
    _check("requeued_served", served, "resumed-вызов снят следующим dequeue")
    return {
        "name": "S3 · park/resume бюджета (D4/D5)",
        "shelf": S3_SHELF,
        "user": S3_USER,
        "job_id": S3_JOB,
        "limit_micro": limit_micro,
        "park_ok": park_ok,
        "slots_ext_after_park": slots_ext_after_park,
        "conc_after_park": conc_after_park,
        "resume_blocked_while_exhausted": resume_blocked and resume_blocked2,
        "resume_ok_after_release": resume_ok and resume_ok2,
        "state_after": state_after,
        "state_after_call_cycle": state_after2,
        "vft_orig": vft_orig,
        "vft_restored": vft_restored,
        "starve_orig": starve_orig,
        "starve_restored": starve_restored,
        "served": served,
        "slots_ext_after": slots_ext_after,
        "budget_counter_restored": True,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def _md_report(payload: Mapping[str, Any]) -> str:
    """MD-отчёт: секции запущенных сценариев + итог + граница честности."""
    lines = [
        "# Ф4.7 — приёмочный контурный прогон (S1 + S2 + S3)",
        "",
        f"- Сгенерирован: {payload['generated_at']}",
        (
            f"- Сценарии: {', '.join(payload['scenario_set'])}; "
            "редис: ws-контур (URL не логируется)"
        ),
        "",
    ]
    if "s1" in payload:
        s1 = payload["s1"]
        lines += [
            "## S1 — 5 параллельных постановок + квоты (D1)",
            "",
            (
                f"- Полка: `{s1['shelf']}`; воркеров: {s1['workers']}; "
                f"постановка: {s1['submit_seconds']} c; "
                f"исполнение: {s1['run_seconds']} c"
            ),
            "",
            "| участник | уровень | class | zone | mode | постановка | prio (source) | финал | tokens/charged | день spent/limit | остаток | conc после |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in s1["participants"]:
            submit = (
                r["submit"] if r["submit"] != "denied"
                else f"denied ({r['deny_code']})"
            )
            finals = (
                f"{r.get('final_status')}/{r.get('final_state')}"
                if r["submit"] in ("admitted", "parked")
                else "—"
            )
            tokens = (
                f"{r['tokens']}/{r['charged']}"
                if r["submit"] in ("admitted", "parked")
                else "—"
            )
            day_spent = (
                f"{r['tokens_spent_day']}/{r['tokens_per_day']}"
                if r["submit"] in ("admitted", "parked")
                else "—"
            )
            remaining = (
                "—" if r["tokens_remaining"] is None else str(r["tokens_remaining"])
            )
            conc = (
                "—" if r["conc_inflight_after"] is None
                else str(r["conc_inflight_after"])
            )
            lines.append(
                f"| {r['user']} | {r['account_level']} | {r['job_class']} "
                f"| {r['zone']} | {r['mode']} | {submit} | {r['prio']} "
                f"({r['prio_source']}) | {finals} | {tokens} | {day_spent} "
                f"| {remaining} | {conc} |"
            )
        t = s1["totals"]
        lines += [
            "",
            "### Сводка S1",
            "",
            (
                f"- submitted/admitted/denied/parked/errors: **{t['submitted']}"
                f"/{t['admitted']}/{t['denied']}/{t['parked']}/{t['errors']}**; "
                f"сумма tokens: **{t['tokens_sum']}**"
            ),
            (
                f"- max in-flight (сэмпл {int(SAMPLE_INTERVAL_S * 1000)} мс по "
                f"стору): **{s1['max_in_flight']}** "
                f"(ожидание {s1['max_in_flight_expected']}); пик у: "
                f"{', '.join(s1['max_in_flight_users']) or '—'}"
            ),
            (
                f"- deny-проба D3 ({s1['deny_probe']['user']}, предзаписанный "
                f"расход {s1['deny_probe']['preset_spent']}): код "
                f"**{s1['deny_probe']['actual_code']}** — "
                f"{'✅' if s1['deny_probe']['ok'] else '❌'}"
            ),
        ]
        lines += ["", "### Ассерты S1", "", "| проверка | итог | деталь |",
                  "|---|---|---|"]
        for c in s1["checks"]:
            lines.append(
                f"| {c['name']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |"
            )

    if "s2" in payload:
        s2 = payload["s2"]
        b = s2["battle"]
        lines += [
            "",
            "## S2 — очередь/K/вытеснение в бою (Lua queue/slots, K=1)",
            "",
            (
                f"- Полка `{s2['shelf']}`, K={s2['k']}; вызовов: "
                f"{s2['calls']} (2×interactive/high, 2×batch/med, "
                "1×background/low)"
            ),
            (
                f"- Порядок снятия `{s2['taken_order']}` == "
                f"`policy.pick_best` {s2['expected_order']}; классы: "
                f"{s2['class_order']}"
            ),
            (
                f"- Пик in-flight слотов: **{s2['peak_in_flight']}** (K никогда "
                f"не превышен); отказ сверх K (acquire→False): "
                f"{s2['refusal_over_k']}, dequeue при занятом слоте пуст="
                f"{s2['refusal_dequeue_full']}, "
                f"очередь нетронута={s2['queue_intact_on_refusal']}"
            ),
            (
                f"- Вытеснение в бою: слот держал `{b['batch_call']}` (batch), "
                f"пришёл `{b['interactive_call']}` → preempt="
                f"**{b['preempt_ok']}**, vft {b['vft_before']!r} → "
                f"{b['vft_after']!r} (кредит vft−cost_done/w = "
                f"{b['credit_expected']!r}, cost_done={b['cost_done']}), "
                f"starve {b['starve_before']!r} → {b['starve_after']!r} (I2), "
                f"вызов в обоих индексах={b['in_both_indices']}"
            ),
            (
                f"- interactive получил слот: **{b['interactive_got_slot']}**; "
                f"вытесненный batch сервируется следующим dequeue: "
                f"**{b['requeued_served_next']}**; holders после S2: "
                f"{s2['holders_after']}"
            ),
            "",
            "### Ассерты S2",
            "",
            "| проверка | итог | деталь |",
            "|---|---|---|",
        ]
        for c in s2["checks"]:
            lines.append(
                f"| {c['name']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |"
            )

    if "s3" in payload:
        s3 = payload["s3"]
        lines += [
            "",
            "## S3 — park/resume бюджета (D4/D5, полка ext, PricingRegistry)",
            "",
            (
                f"- Пользователь `{s3['user']}`, job `{s3['job_id']}`, "
                f"лимит ext: {s3['limit_micro']} микро-₽"
            ),
            (
                f"- Бюджет исчерпан (счётчик месяца = limit_micro) → submit: "
                f"job создан и СРАЗУ **parked** ({s3['park_ok']}), "
                f"ws:slots:ext={s3['slots_ext_after_park']} (пуст), conc не "
                f"взят ({s3['conc_after_park'] is None})"
            ),
            (
                f"- resume при исчерпанном → False "
                f"(**{s3['resume_blocked_while_exhausted']}**, job остаётся "
                f"parked, записей нет); бюджет возвращён → resume → True "
                f"(**{s3['resume_ok_after_release']}**, state="
                f"{s3['state_after']}, после цикла с вызовом "
                f"{s3['state_after_call_cycle']})"
            ),
            (
                f"- Вызов в очереди с исходными vft {s3['vft_orig']!r} → "
                f"{s3['vft_restored']!r}, starve {s3['starve_orig']!r} → "
                f"{s3['starve_restored']!r}; снят следующим dequeue: "
                f"**{s3['served']}**"
            ),
            (
                f"- ws:slots:ext после S3: {s3['slots_ext_after']}; счётчик "
                f"месяца восстановлен ({s3['budget_counter_restored']})"
            ),
            "",
            "### Ассерты S3",
            "",
            "| проверка | итог | деталь |",
            "|---|---|---|",
        ]
        for c in s3["checks"]:
            lines.append(
                f"| {c['name']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |"
            )

    verdict = "✅ все сценарии пройдены" if payload["ok"] else "❌ есть нарушения"
    lines += ["", "## Итог", "", "| сценарий | итог |", "|---|---|"]
    for key, label in (
        ("s1", "S1 постановки+квоты"),
        ("s2", "S2 очередь/K/preempt"),
        ("s3", "S3 park/resume бюджета"),
    ):
        if key in payload:
            lines.append(
                f"| {label} | "
                f"{'✅ пройден' if payload[key]['ok'] else '❌ нарушения'} |"
            )
    lines += [
        "",
        f"**Общий итог:** {verdict}",
        "",
        "## Граница честности",
        "",
        "- Прогон **контурный, НЕ через живых провайдеров**: провайдеры —",
        "  детерминированные стабы полок (`StubShelfLLM`/`StubMCP` из",
        "  `golden_run.py`); LLM-путь через `Queue`/`Slots` в движок НЕ заведён —",
        "  реальные ollama/DeepSeek это шаг прода Ф6.",
        "- **Исполнителя авто-вытеснения (arq-воркера) нет**: в S2 роль",
        "  планировщика-арбитра играет сам драйвер — он ЯВНО вызывает",
        "  `Slots.release` + `Queue.preempt` и отдаёт слот interactive-вызову.",
        "  Это НЕ ослабляет проверку механик (реальные Lua `queue.lua`/",
        "  `slots.lua` на живом ws-redis), но авто-цикл воркера не доказывается.",
        "- S3: счётчик месяца `ws:budget:global:{Y-M}` выставляется драйвером",
        "  напрямую (имитация исчерпания/nightly reconcile D4); реальные",
        "  ₽-списания `charge_budget` не выполняются (движок не гоняется),",
        "  исходное значение счётчика восстанавливается.",
        "- Доказывается: S1 — admission D3/D6 (Lua ADMIT), приоритеты D2/D8,",
        "  списание usage, возврат conc; S2 — WFQ-порядок == pick_best,",
        "  K-инвариант (отказ, не очередь), preempt-кредит vft−cost_done/w,",
        "  starve-стабильность (I2), сервис requeued; S3 — park-на-submit при",
        "  исчерпании (D5), блокировка resume, возврат с исходными vft/starve.",
        "- НЕ доказывается: латентность живых моделей, ретраи шлюзов,",
        "  REVISE-петля реального критика, многопроцессный воркер с preempt,",
        "  реальные ₽-списания и полный reconcile-цикл.",
        "",
    ]
    return "\n".join(lines)



def main(argv: list[str] | None = None) -> int:
    """CLI: прогон выбранных сценариев (S1/S2/S3; default — все) → JSON/MD."""
    parser = argparse.ArgumentParser(
        prog="f47-acceptance",
        description="Ф4.7: приёмочный контурный драйвер AI-верстака (S1+S2+S3)",
    )
    parser.add_argument(
        "--shelf", default="local", help="полка контура S1 (default: local)"
    )
    parser.add_argument(
        "--redis-url", default=None, help="ws-redis URL (default: env WS_REDIS_URL)"
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="параллельность исполнения S1 (default: 5)",
    )
    parser.add_argument(
        "--json", action="store_true", help="печать payload JSON в stdout"
    )
    parser.add_argument("--md-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--run-s1", action="store_true", help="запустить S1 (5 постановок + квоты)"
    )
    parser.add_argument(
        "--run-s2", action="store_true",
        help="запустить S2 (очередь/K/preempt, полка local, K=1)",
    )
    parser.add_argument(
        "--run-s3", action="store_true",
        help="запустить S3 (park/resume бюджета, полка ext)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="запустить все сценарии (поведение по умолчанию)",
    )
    args = parser.parse_args(argv)

    redis_url = args.redis_url or os.environ.get("WS_REDIS_URL")
    if not redis_url:
        print(
            "WS_REDIS_URL не задан и нет --redis-url — fail-closed (дефолта НЕТ)."
            " Тестовый контур: make ws-up-test → redis://127.0.0.1:6390/0",
            file=sys.stderr,
        )
        return 2
    try:
        ctx = build_context(redis_url, shelf=args.shelf)
    except ValueError as exc:  # ext без pricing — fail-fast конструирования контура
        print(f"контур не собрался: {exc}", file=sys.stderr)
        return 2
    if not _ping_wait(ctx.client):
        print(
            "ws-redis не отвечает ping за 30 c — контур не поднят (make ws-up-test)",
            file=sys.stderr,
        )
        return 2

    selected = [
        name
        for name, flag in (
            ("S1", args.run_s1),
            ("S2", args.run_s2),
            ("S3", args.run_s3),
        )
        if flag
    ]
    if args.all or not selected:
        selected = ["S1", "S2", "S3"]

    scenarios: dict[str, dict[str, Any]] = {}
    try:
        if "S1" in selected:
            scenarios["s1"] = s1_parallel_jobs(
                ctx, workers=args.workers, shelf=args.shelf
            )
        if "S2" in selected:
            scenarios["s2"] = s2_queue_preempt(ctx)
        if "S3" in selected:
            scenarios["s3"] = s3_budget_park_resume(ctx)
    except QuotaRedisUnavailable as exc:
        print(f"ws-redis деградировал (fail-closed): {exc}", file=sys.stderr)
        return 2

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario_set": selected,
        **scenarios,
        "ok": all(s["ok"] for s in scenarios.values()),
    }
    report = _md_report(payload)
    args.md_report.parent.mkdir(parents=True, exist_ok=True)
    args.md_report.write_text(report, encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"отчёт: {args.md_report}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
