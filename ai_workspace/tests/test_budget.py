"""Тесты бюджет-контура ext-полки (P0-1 ревизии Ф4): деньги end-to-end.

Доказательства (контракт P0-1 «₽-hard-stop существует»):
- валидатор прайса fail-closed (коды P1-P10, ref-целостность на полки
  model_classes + покрытие budgets + валюта RUB);
- ДЕНЬГИ — только целые микро-₽: cost_micro чистая int-арифметика,
  счётчики Redis — INCRBY int; 1000 списаний не дают дрейфа;
- единицы согласованы: Budget.limit_micro (₽ x 10^6) == порог admission,
  счётчик в той же единице — на лимите парк, за лимитом тоже;
- e2e: submit на ext-полку -> РЕАЛЬНОЕ списание (charge_budget) исчерпывает
  бюджет -> job parked (НЕ failed); reconcile чинит расхождение -> resume
  возвращает job в очередь;
- списание пишет ЖУРНАЛ (ws:budget:journal) — SSOT факта для reconcile;
- ext-wiring без прайса — fail-fast на конструкции (деньги без потолка
  запрещены); деградация redis — QuotaRedisUnavailable (fail-closed).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from ai_workspace.registry import Registry, RegistryError
from ai_workspace.registry.pricing import (
    PricingRegistry,
    ShelfPrice,
    validate_pricing,
)
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

MODEL_CLASSES = {"heavy": {"shelf": "ext"}, "fast": {"shelf": "local"}}
BUDGETS_EXT = {"ext": {"currency": "RUB", "limit": 3000}}


def _valid_doc() -> dict:
    return {
        "version": 1,
        "currency": "USD",
        "rate_usd_rub": 80.0,
        "shelves": {
            "ext": {
                "model": "deepseek/deepseek-flash",
                "input_per_1m": 0.30,
                "output_per_1m": 1.20,
                "source": "https://api-docs.deepseek.com/pricing",
            }
        },
    }


# ── offline: валидатор прайса (fail-closed, коды) ──────────────────────


def _codes(doc, *, model_classes=MODEL_CLASSES, budgets=BUDGETS_EXT):
    return {f.code for f in validate_pricing(doc, model_classes, budgets)}


def test_validator_accepts_valid_doc() -> None:
    assert _codes(_valid_doc()) == set()


def test_validator_not_mapping_and_missing_fields() -> None:
    assert _codes(["nope"]) == {"P1"}
    doc = _valid_doc()
    del doc["rate_usd_rub"]
    del doc["shelves"]
    assert _codes(doc) == {"P1", "P7"}  # поля P1 + бюджет без прайса P7


def test_validator_bad_version_currency_rate() -> None:
    doc = _valid_doc() | {"version": 0, "currency": "RUB", "rate_usd_rub": 0}
    assert _codes(doc) == {"P2", "P3", "P4"}


def test_validator_ref_integrity_unknown_shelf() -> None:
    doc = _valid_doc()
    doc["shelves"]["mars"] = dict(doc["shelves"]["ext"])
    assert _codes(doc) == {"P6"}


def test_validator_budget_shelf_without_price() -> None:
    # budgets.ext есть, прайса ext нет -> списание невозможно (fail-closed)
    doc = _valid_doc()
    doc["shelves"] = {"local": {  # local не бюджетный, но валидная полка
        "model": "ollama/qwen2.5:7b", "input_per_1m": 0.0,
        "output_per_1m": 0.0, "source": "internal",
    }}
    assert _codes(doc) == {"P7"}


def test_validator_budget_currency_must_be_rub() -> None:
    doc = _valid_doc()
    budgets = {"ext": {"currency": "USD", "limit": 30}}  # Q12 допускает USD...
    assert _codes(doc, budgets=budgets) == {"P10"}  # ...но прайс-контур — микро-₽


def test_validator_shelf_fields_and_prices() -> None:
    doc = _valid_doc()
    doc["shelves"]["ext"] = {"model": "", "output_per_1m": -1}
    assert _codes(doc) == {"P1", "P8"}  # P8: model="" и price=-1


def test_pricing_registry_fail_closed_on_broken_hot_reload(tmp_path) -> None:
    """Битый hot-reload прайса -> RegistryError (без тихих старых цен)."""
    work = tmp_path / "registry"
    shutil.copytree(REGISTRY_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    reg = Registry(work)
    pricing = PricingRegistry(reg)
    assert pricing.price_for("ext").model  # валидная загрузка
    (work / "pricing.yaml").write_text("version: 1\ncurrency: EUR\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="P3"):
        pricing.price_for("ext")


# ── offline: целочисленные деньги ──────────────────────────────────────


def test_shelf_price_cost_micro_is_pure_integer_math() -> None:
    price = ShelfPrice(
        shelf="ext", model="m", input_per_1m_usd=0.3, output_per_1m_usd=1.2,
        input_per_1m_micro=24_000_000, output_per_1m_micro=96_000_000,
        source="test",
    )
    # 1M входных = ровно 24 ₽ (24e6 микро); 1M выходных = 96 ₽
    assert price.cost_micro(1_000_000, 0) == 24_000_000
    assert price.cost_micro(0, 1_000_000) == 96_000_000
    # дробные токены НЕ дают float: округление к ближайшему целому микро
    assert isinstance(price.cost_micro(1, 1), int)
    assert price.cost_micro(1, 0) == 24  # 1e6/1e6=1... 24e6*1/1e6=24
    assert price.cost_micro(0, 1) == 96
    # большие объёмы — точная int-арифметика (никаких e-нотаций/потерь)
    assert price.cost_micro(123_456_789, 987_654_321) == (
        (123_456_789 * 24_000_000 + 500_000) // 1_000_000
        + (987_654_321 * 96_000_000 + 500_000) // 1_000_000
    )
    with pytest.raises(ValueError):
        price.cost_micro(-1, 0)


def test_thousand_equal_charges_sum_exactly() -> None:
    """1000 одинаковых списаний == 1000 x одно списание (int, дрейфа нет)."""
    price = ShelfPrice(
        shelf="ext", model="m", input_per_1m_usd=0.3, output_per_1m_usd=1.2,
        input_per_1m_micro=24_000_000, output_per_1m_micro=96_000_000,
        source="test",
    )
    once = price.cost_micro(3_333, 1_049)
    assert once * 1000 == sum(price.cost_micro(3_333, 1_049) for _ in range(1000))


def test_budget_limit_micro_contract() -> None:
    """Единицы согласованы: ₽-лимит конвертируется в микро-₽ при загрузке."""
    budget = QuotaRegistry(Registry(REGISTRY_DIR)).budgets["ext"]
    assert budget.limit_micro == budget.limit * 1_000_000
    assert isinstance(budget.limit_micro, int)


# ── offline: fail-fast / fail-closed ──────────────────────────────────


def test_ext_wiring_without_pricing_fails_fast() -> None:
    """ext-полка без прайса: конструкция запрещена (P0-1) — до любого redis."""
    from ai_workspace.scheduler.wiring import QuotaWiring, RedisQuotaPort

    with pytest.raises(ValueError, match="pricing"):
        QuotaWiring(None, registry=None, shelf="ext", pricing=None)
    with pytest.raises(ValueError, match="pricing"):
        RedisQuotaPort(registry=None, redis=None, shelf="ext", pricing=None)


def test_charge_budget_degrades_fail_closed() -> None:
    """redis недоступен при списании -> QuotaRedisUnavailable (деньги не
    пропускаются молча)."""
    import redis

    from ai_workspace.scheduler.admission import QuotaRedisUnavailable
    from ai_workspace.scheduler.budget import charge_budget

    class _DeadCall:
        def __init__(self, exc):
            self._exc = exc

        def __call__(self, *a, **k):
            raise self._exc

    class _DeadRedis:
        def register_script(self, _s):
            return _DeadCall(redis.exceptions.ConnectionError("refused"))

        def xadd(self, *a, **k):
            raise redis.exceptions.ConnectionError("refused")

    with pytest.raises(QuotaRedisUnavailable):
        charge_budget(
            "u", tokens_in=1, tokens_out=2, redis=_DeadRedis(),
            pricing=PricingRegistry(Registry(REGISTRY_DIR)),
        )


# ── integration: живой ws-redis ────────────────────────────────────────


@pytest.mark.integration
@requires_redis
class TestBudgetIntegration:
    """Живой контур: списание/журнал/дрейф/e2e-парк/resume (P0-1)."""

    @pytest.fixture()
    def ws(self):
        from ai_workspace.redis_client import make_ws_redis
        from ai_workspace.scheduler.admission import budget_global_key, budget_user_key

        client = make_ws_redis()
        user = f"{WS_TEST_ID_PREFIX}p01-{uuid4().hex[:8]}"
        yield client, user
        keys = [budget_global_key(), budget_user_key(user)]
        keys += list(client.scan_iter(match=f"ws:quota:*{user}*"))
        keys += list(client.scan_iter(match=f"ws:job:{user}-*"))
        client.delete(*keys)

    @pytest.fixture()
    def isolated(self, ws):
        """Изоляция сверки: журнал и счётчик месяца чисты до/после теста.

        Журнал — общий стрим: записи других тестов того же месяца иначе
        попали бы в journal-сумму reconcile (serial-прогон — безопасно)."""
        from ai_workspace.scheduler.budget import BUDGET_JOURNAL_KEY

        client, user = ws
        client.delete(BUDGET_JOURNAL_KEY, *list(client.scan_iter(match="ws:budget:user:*")))
        yield client, user
        client.delete(BUDGET_JOURNAL_KEY, *list(client.scan_iter(match="ws:budget:user:*")))

    def test_charge_writes_counters_and_journal_integers(self, isolated):
        from ai_workspace.scheduler.admission import budget_global_key, budget_user_key
        from ai_workspace.scheduler.budget import (
            BUDGET_JOURNAL_KEY,
            charge_budget,
            local_month,
        )

        client, user = isolated
        pricing = PricingRegistry(Registry(REGISTRY_DIR))
        price = pricing.price_for("ext")
        expect = price.cost_micro(100, 900)

        got = charge_budget(
            user, tokens_in=100, tokens_out=900, redis=client, pricing=pricing, job="j-1"
        )

        month = local_month()
        raw_global, raw_user = client.get(budget_global_key(month)), client.get(
            budget_user_key(user, month)
        )
        assert got == expect
        assert raw_global.isdigit() and int(raw_global) == expect  # int, не float
        assert raw_user.isdigit() and int(raw_user) == expect
        entries = [json.loads(f["event"]) for _i, f in client.xrange(BUDGET_JOURNAL_KEY)]
        assert entries[-1] == {
            "user": user, "month": month, "rub_micro": expect,
            "tokens_in": 100, "tokens_out": 900, "job": "j-1",
        }

    def test_thousand_incrby_charges_no_drift(self, isolated):
        from ai_workspace.scheduler.admission import budget_global_key
        from ai_workspace.scheduler.budget import charge_budget

        client, user = isolated
        pricing = PricingRegistry(Registry(REGISTRY_DIR))
        once = charge_budget(user, tokens_in=0, tokens_out=7_777, redis=client, pricing=pricing)
        for _ in range(999):
            charge_budget(user, tokens_in=0, tokens_out=7_777, redis=client, pricing=pricing)
        assert int(client.get(budget_global_key())) == once * 1000  # ровно, без дрейфа

    def test_e2e_real_charges_exhaust_budget_then_submit_parks(self, isolated):
        """P0-1 главная нить: списания РЕАЛЬНЫМ писателем исчерпывают бюджет
        -> submit на ext-полку -> job PARKED (не failed), резерв не взят."""
        from ai_workspace.orchestrator.job import JobState
        from ai_workspace.scheduler.admission import (
            budget_global_key,
            conc_key,
            conchold_key,
        )
        from ai_workspace.scheduler.budget import charge_budget
        from ai_workspace.scheduler.wiring import QuotaWiring

        client, user = isolated
        book = QuotaRegistry(Registry(REGISTRY_DIR))
        pricing = PricingRegistry(Registry(REGISTRY_DIR))
        limit_micro = book.budgets["ext"].limit_micro
        price = pricing.price_for("ext")
        # токенов ровно до исчерпания: cost >= limit_micro за ОДИН вызов
        tokens = limit_micro * 1_000_000 // price.output_per_1m_micro
        assert price.cost_micro(0, tokens) >= limit_micro
        charge_budget(user, tokens_in=0, tokens_out=tokens, redis=client, pricing=pricing)
        assert int(client.get(budget_global_key())) >= limit_micro

        wiring = QuotaWiring(client, registry=book, shelf="ext", pricing=pricing)
        rec = wiring.submit(
            user=user, account_level="member", job_class="interactive",
            mode="statya", zone="private", job_id=f"{user}-j1",
        )

        assert rec.state is JobState.PARKED  # НЕ failed: бюджет — парк (D5)
        assert client.exists(f"ws:job:{user}-j1")
        assert client.get(conc_key(user)) is None  # парк не резервирует
        assert client.smembers(conchold_key(user)) == set()

    def test_reconcile_fixes_drift_and_resume_returns_job(self, isolated):
        """Сверка чинит расхождение (в обе стороны) и «возвращает бюджет»:
        исчерпание -> парк; reconcile привёл счётчик к журналу (ниже лимита)
        -> resume -> job снова queued (едет)."""
        from ai_workspace.orchestrator.job import JobState, JobStore
        from ai_workspace.scheduler.admission import (
            QUOTA_EVENTS_KEY,
            budget_global_key,
        )
        from ai_workspace.scheduler.budget import charge_budget, reconcile_budget
        from ai_workspace.scheduler.park import ParkControl

        client, user = isolated
        book = QuotaRegistry(Registry(REGISTRY_DIR))
        pricing = PricingRegistry(Registry(REGISTRY_DIR))

        # факт: два списания (журнал = истина), счётчик «задран» выше факта
        charge_budget(user, tokens_in=0, tokens_out=1_000, redis=client, pricing=pricing)
        truth = charge_budget(user, tokens_in=0, tokens_out=1_000, redis=client, pricing=pricing)
        client.set(budget_global_key(), book.budgets["ext"].limit_micro + 5)  # дрейф вверх

        report = reconcile_budget(redis=client)
        assert report.journal_total_micro == truth
        assert report.global_after_micro == truth  # дрейт починен К журналу
        assert int(client.get(budget_global_key())) == truth
        assert report.per_user == {user: truth}
        # хвост общего стрима (аудит Ф4.4a): xrange с головы при накоплении >200
        # терял свежее событие; в payload budget_reconciled нет user — фильтр
        # по type + сумме (продуктовый payload не меняем)
        events = [
            json.loads(e[1]["event"])
            for e in client.xrevrange(QUOTA_EVENTS_KEY, count=200)
        ]
        assert any(
            e["type"] == "budget_reconciled" and e["journal_total_micro"] == truth
            for e in events
        )

        # дрейф ВНИЗ тоже чинится (счётчик занижен -> поднят к журналу)
        client.set(budget_global_key(), 0)
        reconcile_budget(redis=client)
        assert int(client.get(budget_global_key())) == truth

        # парк при исчерпании -> reconcile вернул бюджет -> resume едет
        # (ws:events:ext — ОБЩИЙ стрим: resumed-событие чистим в finally,
        # паттерн _cleanup_shelf в test_park_resume)
        client.set(budget_global_key(), book.budgets["ext"].limit_micro)
        store = JobStore(client)
        job = store.create(
            user=user, account_level="member", job_class="interactive",
            mode="review", zone="private", job_id=f"{user}-j2",
        )
        pc = ParkControl(client, shelf="ext", store=store, clock=lambda: 0.0)
        try:
            assert pc.park(job.id, reason="budget_ext_exhausted") is True
            assert store.get(job.id).state is JobState.PARKED

            client.set(budget_global_key(), truth)  # reconcile-уровень (ниже лимита)
            assert pc.resume(job.id, registry=book) is True
            assert store.get(job.id).state is JobState.QUEUED  # job едет
        finally:
            client.delete("ws:events:ext")


# ── integration: P2-3 iter2 — гейт корректировки ВНИЗ ──────────────────


@pytest.mark.integration
@requires_redis
class TestBudgetReconcileGate:
    """Сверка — counter↔journal (наши списания), НЕ audit провайдера:
    подозрительно большая корректировка ВНИЗ (счётчик ≫ журнала — признак
    потери/усечения журнала) без явного порога НЕ применяется (fail-closed:
    бюджет не «возвращается» молча)."""

    @pytest.fixture()
    def isolated(self):
        from ai_workspace.redis_client import make_ws_redis
        from ai_workspace.scheduler.admission import budget_user_key
        from ai_workspace.scheduler.budget import BUDGET_JOURNAL_KEY

        client = make_ws_redis()
        user = f"{WS_TEST_ID_PREFIX}p23-{uuid4().hex[:8]}"
        client.delete(BUDGET_JOURNAL_KEY, *list(client.scan_iter(match="ws:budget:user:*")))
        yield client, user
        from ai_workspace.scheduler.admission import budget_global_key

        keys = [budget_global_key(), budget_user_key(user)]
        keys += list(client.scan_iter(match=f"ws:quota:*{user}*"))
        keys += list(client.scan_iter(match=f"ws:job:{user}-*"))
        keys += [BUDGET_JOURNAL_KEY]
        keys += list(client.scan_iter(match="ws:budget:user:*"))
        client.delete(*keys)

    def test_reconcile_downward_gate_blocks_suspicious_correction(self, isolated):
        from ai_workspace.scheduler.admission import budget_global_key
        from ai_workspace.scheduler.budget import charge_budget, reconcile_budget

        client, user = isolated
        pricing = PricingRegistry(Registry(REGISTRY_DIR))
        truth = charge_budget(
            user, tokens_in=0, tokens_out=1_000, redis=client, pricing=pricing
        )
        client.set(budget_global_key(), truth + 10**9)  # «дрейф» на 1000 ₽ вверх

        with pytest.raises(ValueError, match="вниз"):
            reconcile_budget(redis=client, max_downward_micro=1_000_000)
        assert int(client.get(budget_global_key())) == truth + 10**9  # НЕ тронут

        report = reconcile_budget(redis=client, max_downward_micro=2 * 10**9)
        assert report.global_after_micro == truth  # в пределах порога — применено
        assert int(client.get(budget_global_key())) == truth
