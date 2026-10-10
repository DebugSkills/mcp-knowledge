"""Ф4.6 — сигнатура WFQ: наблюдаемый контракт очереди (живой ws-redis).

Дополняет (НЕ дублирует) уже покрытое:
- ``test_queue_policy.py`` — offline weight/pick_best/aging;
- ``test_position.py`` — offline parity rank_positions<->pick_best, update_pos;
- ``test_dequeue_acquire.py`` — атомарность слотов, parity dequeue<->acquire.

НОВОЕ (собственно сигнатура Ф4.6):
1. живой 3x3-порядок девяти вызовов с observed vft + доказательство «не FIFO»
   (вставка в ОБРАТНОМ порядке -> выдача всё равно по argmin vft);
2. ``rank_positions`` согласован с ЖИВЫМ ``dequeue`` на том же наборе;
3. starvation-ГРАНИЦА на живой очереди (до дедлайна -> по vft, в дедлайн -> по
   дедлайну; низкий приоритет обгоняет высокий после просрочки — I2);
4. тай-брейк равных vft по лекс. имени, независимо от порядка вставки;
5. конфиг-инварианты шлюза: queue/scheduler fairness OFF, ``num_retries: 0``,
   по одному deployment на model_name (``simple-shuffle`` — не fairness), K local.

Числа сценариев печатаются (``-s``) как наблюдение по носителю, не по сводке.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from ai_workspace.scheduler import policy
from ai_workspace.scheduler.position import PositionStore, rank_positions
from ai_workspace.scheduler.queue import Queue
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

NOW = 1_000.0
ROOT = Path(__file__).resolve().parents[2]

# 3x3: (имя, prio, class); cost_est одинаков -> vft = cost/w(p,c) (спека §3).
NINE = [
    ("ih", "high", "interactive"),
    ("im", "med", "interactive"),
    ("il", "low", "interactive"),
    ("bh", "high", "batch"),
    ("bm", "med", "batch"),
    ("bl", "low", "batch"),
    ("bgh", "high", "background"),
    ("bgm", "med", "background"),
    ("bgl", "low", "background"),
]
COST = 3600.0
FAR = NOW + 1_000_000.0  # дедлайн далеко: aging не вмешивается в WFQ


@pytest.fixture()
def env():
    """Изолированная полка test-f46-*; уборка своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = f"{WS_TEST_ID_PREFIX}f46-{uuid4().hex[:8]}"
    yield client, shelf, Queue(client, shelf=shelf, clock=lambda: NOW)
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


@requires_redis
@pytest.mark.integration
def test_signature_3x3_vft_matrix_and_wfq_order_not_fifo(env):
    _, shelf, q = env
    seen: dict[str, float] = {}
    # вставка в ОБРАТНОМ порядке WFQ -> FIFO-очередь выдала бы "bgl" первым
    for name, prio, cls in reversed(NINE):
        seen[name] = q.enqueue(
            f"{shelf}:{name}:0:0", prio=prio, call_class=cls,
            cost_est=COST, now=NOW, starve_deadline=FAR,
        )
    expected = {n: COST / policy.weight(p, c) for n, p, c in NINE}
    print("\n[Ф4.6] vft 3x3:", {n: seen[n] for n, _, _ in NINE})
    for n, _, _ in NINE:
        assert seen[n] == pytest.approx(expected[n]), n

    name_of = {f"{shelf}:{n}:0:0": n for n, _, _ in NINE}
    order = [name_of[c] for c in q.dequeue(now=NOW, limit=9)]
    print("[Ф4.6] dequeue order:", order)
    assert order == [n for n, _, _ in NINE]
    assert order[:3] == ["ih", "im", "il"]  # WFQ группирует по весу
    assert order[0] == "ih"  # НЕ FIFO: "ih" вставлен ПОСЛЕДНИМ
    assert order != [n for n, _, _ in reversed(NINE)]


@requires_redis
@pytest.mark.integration
def test_signature_rank_positions_consistent_with_live_dequeue(env):
    client, shelf, q = env
    positions = PositionStore(client, clock=lambda: NOW)
    calls = {n: f"{shelf}:{n}:0:0" for n, _, _ in NINE}
    name_of = {v: k for k, v in calls.items()}
    for name, prio, cls in NINE:
        q.enqueue(calls[name], prio=prio, call_class=cls, cost_est=COST,
                  now=NOW, starve_deadline=FAR)

    depth = positions.update_pos(shelf, now=NOW)
    assert depth == 9
    ranks = {n: positions.position_of(calls[n]) for n, _, _ in NINE}
    cands = [
        {"call": calls[n], "vft": q.call_record(calls[n])["vft"],
         "starve_deadline": q.call_record(calls[n])["starve_deadline"]}
        for n, _, _ in NINE
    ]
    ordered = sorted(rank_positions(cands, NOW).items(), key=lambda kv: kv[1])
    rule_order = [name_of[c] for c, _ in ordered]
    panel_order = sorted((n for n, _, _ in NINE), key=lambda n: ranks[n])
    live = [name_of[c] for c in q.dequeue(now=NOW, limit=9)]

    print("[Ф4.6] panel ranks:", ranks)
    assert rule_order == [n for n, _, _ in NINE]
    assert panel_order == rule_order == live


@requires_redis
@pytest.mark.integration
def test_signature_starvation_boundary(env):
    client, shelf, _ = env
    t0, dl = NOW, NOW + 30.0
    # offline: точная граница правила (X — high/interactive vft 0.25;
    # Y — low/batch vft 10.0, но близкий дедлайн).
    cands = [
        {"call": "X", "vft": 0.25, "starve_deadline": t0 + 100_000.0},
        {"call": "Y", "vft": 10.0, "starve_deadline": dl},
    ]
    assert policy.pick_best(cands, dl - 0.001) == "X"  # до дедлайна — по vft
    assert policy.pick_best(cands, dl) == "Y"          # в дедлайн — по дедлайну
    assert policy.pick_best(cands, dl + 0.001) == "Y"

    # live: два идентичных набора, разница только в «сейчас».
    qa = Queue(client, shelf=f"{shelf}a", clock=lambda: t0)
    qb = Queue(client, shelf=f"{shelf}b", clock=lambda: t0)
    for qq in (qa, qb):
        qq.enqueue("X:0:0", prio="high", call_class="interactive",
                   cost_est=100.0, now=t0, starve_deadline=t0 + 100_000.0)
        qq.enqueue("Y:0:0", prio="low", call_class="batch",
                   cost_est=100.0, now=t0, starve_deadline=dl)
    before = qa.dequeue(now=dl - 0.001, limit=1)
    after = qb.dequeue(now=dl, limit=1)
    print("[Ф4.6] starve boundary before/after:", before, after)
    assert before == ["X:0:0"]
    assert after == ["Y:0:0"]  # низкий batch обгоняет high interactive (I2)


@requires_redis
@pytest.mark.integration
def test_signature_tiebreak_lex_name_insertion_independent(env):
    client, shelf, _ = env
    # равный vft из РАЗНЫХ (p,c): 100/100 == 10/10 == 1.0
    a, b = "aaa:0:0", "zzz:0:0"
    q1 = Queue(client, shelf=f"{shelf}1", clock=lambda: NOW)
    for call, prio, cls, cost in (
        (b, "low", "interactive", 100.0), (a, "low", "batch", 10.0),  # zzz,aaa
    ):
        q1.enqueue(call, prio=prio, call_class=cls, cost_est=cost,
                   now=NOW, starve_deadline=FAR)
    q2 = Queue(client, shelf=f"{shelf}2", clock=lambda: NOW)
    for call, prio, cls, cost in (
        (a, "low", "batch", 10.0), (b, "low", "interactive", 100.0),  # aaa,zzz
    ):
        q2.enqueue(call, prio=prio, call_class=cls, cost_est=cost,
                   now=NOW, starve_deadline=FAR)

    va = q1.call_record(a)["vft"]
    vb = q1.call_record(b)["vft"]
    print("[Ф4.6] tiebreak vft:", {a: va, b: vb})
    assert va == pytest.approx(vb) == pytest.approx(1.0)
    assert q1.dequeue(now=NOW, limit=2) == [a, b]
    assert q2.dequeue(now=NOW, limit=2) == [a, b]  # порядок вставки не влияет


def _rendered_gateway_yaml(name: str = "litellm.config.yaml", k: int = 1) -> str:
    """Конфиг шлюза: дефолт-рендер шаблона .in с K=1 (I13) — Ф-B R3.

    SSOT = ``<name>.in``; отрендеренный ``<name>`` — gitignored-артефакт (make
    gateway-render / ansible update.yml), на диске может отсутствовать или нести
    K!=1 (канареечный рендер) — тест опирается на ДЕФОЛТНЫЙ рендер K=1, чтобы
    инвариант «K local == 1 в дефолте» не флапал от локального рендера.
    Явная проверка конкретного артефакта: env GATEWAY_TEST_RENDERED=<путь>.
    """
    env_path = os.environ.get("GATEWAY_TEST_RENDERED")
    if env_path:
        return Path(env_path).read_text("utf-8")
    tpl = (ROOT / f"{name}.in").read_text("utf-8")
    return tpl.replace("${LITELLM_MAX_PARALLEL}", str(k))


def test_signature_gateway_config_is_router_not_queue():
    """Шлюз — роутер, не очередь: сигнатуру порядка даёт наш policy.pick_best."""
    doc = yaml.safe_load(_rendered_gateway_yaml("litellm.config.yaml"))
    assert "queue" not in doc and "scheduler" not in doc
    rs = doc.get("router_settings", {})
    assert rs.get("num_retries") == 0  # 429 отдаём сразу (retry — наш слой)
    assert "fallbacks" not in doc and "fallbacks" not in rs  # I5
    # routing_strategy ПРИСУТСТВУЕТ, но не fairness: 1 deployment на model_name
    assert rs.get("routing_strategy") == "simple-shuffle"
    names = [m["model_name"] for m in doc["model_list"]]
    assert names.count("local") == 1 and names.count("ext") == 1
    local = next(m for m in doc["model_list"] if m["model_name"] == "local")
    assert local["litellm_params"]["max_parallel_requests"] == 1  # K local

    doc2 = yaml.safe_load(_rendered_gateway_yaml("litellm.local_only.config.yaml"))
    assert doc2["router_settings"].get("num_retries") == 0
    assert [m["model_name"] for m in doc2["model_list"]] == ["local"]

    compose = (ROOT / "compose.gateway.yml").read_text("utf-8")
    assert "--num_workers 1" in compose  # cap per-worker -> W==1
    assert "ports:" not in compose       # порт 4000 не публикуется (I1/I6)

    # Ф-B (R3) стражи шаблонов: K живёт ТОЛЬКО в плейсхолдере ${LITELLM_MAX_PARALLEL}
    # (int-литерал появляется только после рендера — дефолт-рендер K=1 выше)
    for name in ("litellm.config.yaml", "litellm.local_only.config.yaml"):
        tpl_text = (ROOT / f"{name}.in").read_text("utf-8")
        assert "max_parallel_requests: ${LITELLM_MAX_PARALLEL}" in tpl_text, (
            f"{name}.in: K обязан быть плейсхолдером ${{LITELLM_MAX_PARALLEL}} (SSOT-рендер)"
        )
