"""Parity замороженных артефактов (code-2026-09-23-010, канон §13.4/§11.4-6).

Две копии одних и тех же замороженных конвенций цикла Error→Rule:
collector = scripts/errors_collect.py (сбор) ↔ tool = errors_query (агентский
API). Дубликат ОБЯЗАТЕЛЕН (scripts/ не копируется в Docker-образ), дрейф ловит
ЭТОТ тест — расширение прецедента test_masking_parity.py (маскирование).

Блоки (имена функций/семантика, без file:line-якорей — строки дрейфуют):
  A — словарь P0–P3: P[0-3]-литералы приоритетной лестницы update_aggregates
      (словарь НЕявный → AST, без выделения константы в коллекторе) == PRIO_RANK;
  B — формат сигнатуры: round-trip make_signature → разбор НАСТОЯЩИМ
      _filter_aggregates; маркер ERRORS_QUERY: classify_routine-ветка
      коллектора (audit→routine) ↔ аудит-фильтр тула;
  C — trend: матрица _trend ↔ рост-условие лестницы update_aggregates
      (живой прогон update_aggregates с fake-now);
  D — 7d-окно: включительная граница _suppressed_7d ↔ week_ago-фильтр
      update_aggregates (живой прогон с fake-now);
  E — endpoints cap: ENDPOINTS_KEEP ↔ литерал среза в _render_aggregate;
  F — RED-механика: симуляция дрейфа обязана краснеть (self-test).

Fake-now: патч ec.datetime ТОЛЬКО в тесте, aware-UTC (ловушка naive/aware:
update_aggregates вычисляет (now - last_seen).days — naive упал бы на
вычитании aware-last_seen). Живые прогоны на tmp-sink, продуктовый код не
меняется: parity фиксирует ТЕКУЩЕЕ равенство (сверка трассы: 6/6).
"""

import ast as ast_mod
import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "errors_collect", ROOT / "scripts" / "errors_collect.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

# имя модуля tools.errors_query затенено одноимённой функцией в пакете → sys.modules
import mcp_server.tools  # noqa: F401  (регистрирует пакет в sys.modules)

eq_tool = sys.modules["mcp_server.tools.errors_query"]

COLLECTOR_SRC = (ROOT / "scripts" / "errors_collect.py").read_text(encoding="utf-8")
TOOL_SRC = (ROOT / "mcp_server" / "src" / "mcp_server" / "tools"
            / "errors_query.py").read_text(encoding="utf-8")

FAKE_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


# ── Хелперы (read-only приёмы: AST/двойной импорт — константы не выделяем) ──

def collector_prio_literals() -> set:
    """Неявный словарь приоритетов коллектора: точные P[0-3]-литералы AST-ом.

    Точное совпадение константы → docstring/комментарии не матчатся.
    """
    tree = ast_mod.parse(COLLECTOR_SRC)
    return {n.value for n in ast_mod.walk(tree)
            if isinstance(n, ast_mod.Constant) and isinstance(n.value, str)
            and re.fullmatch(r"P[0-3]", n.value)}


def tool_render_aggregate_body() -> str:
    """Тело _render_aggregate по AST-границам функции (не строковый поиск)."""
    tree = ast_mod.parse(TOOL_SRC)
    lines = TOOL_SRC.splitlines()
    for node in ast_mod.walk(tree):
        if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef)) \
                and node.name == "_render_aggregate":
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("_render_aggregate не найден в tools/errors_query")


def make_flt(**over) -> dict:
    """Фильтр _filter_aggregates (все проверки выключены, кроме переопределённых)."""
    flt = {"include_audit": True, "priority": [], "source": None,
           "signature": None, "status": None, "query": None, "cutoff": None}
    flt.update(over)
    return flt


def mini_agg(**over) -> dict:
    agg = {"priority": "P2", "class": "T", "status": "active",
           "sources": ["docker_logs"], "count_7d": 1, "count_prev_7d": 0,
           "count_total": 1, "actors": [],
           "first_seen": "2026-09-24T00:00:00Z",
           "last_seen": "2026-09-24T00:00:00Z"}
    agg.update(over)
    return agg


def set_fake_now(monkeypatch, moment: datetime) -> None:
    """Патч ec.datetime aware-UTC: update_aggregates зовёт datetime.now(tz)."""
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(ec, "datetime", FakeDateTime)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Блок A — словарь P0–P3 (структурный parity: лестница ↔ PRIO_RANK) ──

class TestPrioDictParity:
    def test_a1_collector_literals_equal_tool_prio_rank(self):
        # лестница/default/гварды коллектора дают ровно те же ключи, что PRIO_RANK
        assert collector_prio_literals() == set(eq_tool.PRIO_RANK)

    def test_a2_ranks_monotonic_by_severity(self):
        # сортировка тула (PRIO_RANK) согласована с тяжестью P0<P1<P2<P3
        r = eq_tool.PRIO_RANK
        assert r["P0"] < r["P1"] < r["P2"] < r["P3"]

    def test_a3_each_side_self_consistent(self):
        # лестница производит только валидные ключи PRIO_RANK; ранги биективны 0..3
        assert collector_prio_literals() <= set(eq_tool.PRIO_RANK)
        assert sorted(eq_tool.PRIO_RANK.values()) == [0, 1, 2, 3]


# ── Блок B — формат сигнатуры (round-trip: make_signature → настоящий разбор) ──

class TestSignatureRoundTrip:
    def test_b1_marker_case_survives_tool_parse(self):
        # marker-кейс: тул разбирает ключ коллектора, маркер ≠ аудит → выживает
        sig = ec.make_signature("docker_logs", "MCP", None, "tool=search ok 12 ms")
        out = eq_tool._filter_aggregates({sig: mini_agg()},
                                         make_flt(include_audit=False))
        assert [s for s, _ in out] == [sig]

    def test_b2_error_code_case_key_fallback(self):
        # marker=None → ключ = error_code (не dash, не мусор)
        sig = ec.make_signature("docker_logs", None, "500", "upstream failed")
        out = eq_tool._filter_aggregates({sig: mini_agg()},
                                         make_flt(include_audit=False))
        assert [s for s, _ in out] == [sig]

    def test_b3_dash_case_both_none(self):
        # оба None → ключ "-"; тул не путает dash с аудит-маркером
        sig = ec.make_signature("docker_logs", None, None, "plain fault")
        assert "|-|" in sig  # формула ключа: marker or error_code or "-"
        out = eq_tool._filter_aggregates({sig: mini_agg()},
                                         make_flt(include_audit=False))
        assert [s for s, _ in out] == [sig]

    def test_b4_pipe_inside_message_maxsplit2(self):
        # '|' в message: разбор maxsplit=2 сохраняет message целиком (query-хвост
        # в сигнатуре находится) и маркер остаётся ВТОРОЙ компонентой
        msg = "route a|b|c failed hard"
        sig = ec.make_signature("docker_logs", "MCP", None, msg)
        out = eq_tool._filter_aggregates(
            {sig: mini_agg()}, make_flt(query="b|c failed hard"))
        assert [s for s, _ in out] == [sig]
        # даже при пайпах в message аудит-маркер опознаётся именно как маркер
        sig_audit = ec.make_signature("docker_logs", "ERRORS_QUERY", None, msg)
        out2 = eq_tool._filter_aggregates({sig_audit: mini_agg()},
                                          make_flt(include_audit=False))
        assert out2 == []

    def test_b5_errors_query_marker_both_sides(self, tmp_path, monkeypatch):
        # (а) коллектор: audit-строка тула → routine (classify_routine-ветка
        # маркера ERRORS_QUERY): expected=True, без актора; агрегат → P3/T
        line = (iso(FAKE_NOW) + " [ERRORS_QUERY] view=list prio=- src=-"
                " period=7d q_len=0 q_hash=- sig=False results=3 dur=0.5ms"
                " key=none")
        evs = ec.parse_docker_log_events("mcp-knowledge-server", [line],
                                         last_ts=None)
        assert len(evs) == 1
        assert evs[0]["marker"] == "ERRORS_QUERY"
        assert evs[0]["expected"] is True
        set_fake_now(monkeypatch, FAKE_NOW)
        ec.update_aggregates(tmp_path / "sink", evs, cfg={})
        (agg,) = ec.load_json(tmp_path / "sink" / "aggregates"
                              / "signatures.json", {}).values()
        assert (agg["priority"], agg["class"]) == ("P3", "T")
        # (б) тул: аудит-фильтр согласован с (а) — ERRORS_QUERY исключается
        # при include_audit=False, чужие маркеры переживают
        s_audit = ec.make_signature("docker_logs", "ERRORS_QUERY", None,
                                    "view=list prio=-")
        s_norm = ec.make_signature("docker_logs", "CRON", None,
                                   "job=backup exit=0")
        aggs = {s_audit: mini_agg(), s_norm: mini_agg()}
        out_no = eq_tool._filter_aggregates(aggs, make_flt(include_audit=False))
        assert [s for s, _ in out_no] == [s_norm]
        out_yes = eq_tool._filter_aggregates(aggs, make_flt(include_audit=True))
        assert len(out_yes) == 2


# ── Блок C — trend (двусторонний: матрица _trend ↔ рост-условие лестницы) ──

TREND_CASES = [  # (count_7d, count_prev_7d, ожидание _trend)
    (0, 0, None),
    (5, 0, None),
    (5, 3, "up"),
    (3, 5, "down"),
    (5, 5, "flat"),
]


class TestTrendParity:
    @pytest.mark.parametrize("c7,prev,expected", TREND_CASES)
    def test_c1_c5_tool_trend_matrix(self, c7, prev, expected):
        assert eq_tool._trend({"count_7d": c7, "count_prev_7d": prev}) == expected

    def test_trend_invariant_equals_growth_condition(self):
        # up ⟺ prev>0 ∧ c7>prev — ровно рост-условие лестницы update_aggregates
        for c7 in range(7):
            for prev in range(7):
                got = eq_tool._trend({"count_7d": c7, "count_prev_7d": prev})
                if prev == 0:
                    want = None
                elif c7 > prev:
                    want = "up"
                elif c7 < prev:
                    want = "down"
                else:
                    want = "flat"
                assert got == want, (c7, prev)

    def test_c6_collector_growth_live(self, tmp_path, monkeypatch):
        """Живое рост-условие лестницы: 2 события 8d назад + 3 сегодня (одна
        сигнатура) → count_7d=3 > count_prev_7d=2 → P1/T. Второй цикл несёт
        события (ранний выход update_aggregates при пустых events/delta).
        """
        sink = tmp_path / "sink"
        msg = "upstream timeout after 30s"
        t8 = FAKE_NOW - timedelta(days=8)
        old = [ec.make_event(iso(t8), "docker_logs", msg, container="c",
                             level="ERROR") for _ in range(2)]
        set_fake_now(monkeypatch, t8)
        ec.update_aggregates(sink, old, cfg={})
        fresh = [ec.make_event(iso(FAKE_NOW), "docker_logs", msg, container="c",
                               level="ERROR") for _ in range(3)]
        set_fake_now(monkeypatch, FAKE_NOW)
        ec.update_aggregates(sink, fresh, cfg={})
        (agg,) = ec.load_json(sink / "aggregates" / "signatures.json",
                              {}).values()
        assert agg["count_7d"] == 3
        assert agg["count_prev_7d"] == 2
        assert agg["priority"] == "P1"
        assert agg["class"] == "T"


# ── Блок D — 7d-окно (двусторонне: _suppressed_7d ↔ week_ago-фильтр) ──

class TestWindow7dParity:
    def test_d1_boundary_day_included_tool(self):
        # день РОВНО 7d назад включён (граница >=, не >)
        edge = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        assert eq_tool._suppressed_7d({"suppressed_daily": {edge: 4}}) == 4

    def test_d2_day_beyond_window_excluded_tool(self):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d")
        assert eq_tool._suppressed_7d({"suppressed_daily": {old: 4}}) == 0

    def test_d3_three_day_synthetic_sum_tool(self):
        now = datetime.now(timezone.utc)
        daily = {(now - timedelta(days=8)).strftime("%Y-%m-%d"): 5,
                 (now - timedelta(days=7)).strftime("%Y-%m-%d"): 2,
                 now.strftime("%Y-%m-%d"): 3}
        # в окне только 7d-граница (2) и сегодня (3); 8d (5) — вне
        assert eq_tool._suppressed_7d({"suppressed_daily": daily}) == 5

    def test_d4_collector_boundary_live(self, tmp_path, monkeypatch):
        """Живая включительная граница коллектора. Механика (P2-калибр
        критика iter2): daily-бакет атрибутируется дню ЦИКЛА (day=now), а не
        дню события; ранний выход при пустых events ⇒ пересчёт count_7d
        происходит только в цикле, несущем событие этой сигнатуры. Поэтому
        ДВА цикла ОДНОЙ сигнатуры: цикл-1 при fake-now=T−7d (бакет на границе
        окна), цикл-2 при fake-now=T (пересчёт: week_ago == T−7d ровно) →
        count_7d == 2 (граница + сегодня); при дрейфе `>` день границы
        выпадает → 1 → красный."""
        sink = tmp_path / "sink"
        sig_msg = "flaky border case"
        t7 = FAKE_NOW - timedelta(days=7)
        border = [ec.make_event(iso(t7), "docker_logs", sig_msg,
                                container="c", level="ERROR")]
        set_fake_now(monkeypatch, t7)
        ec.update_aggregates(sink, border, cfg={})
        today = [ec.make_event(iso(FAKE_NOW), "docker_logs", sig_msg,
                               container="c", level="ERROR")]
        set_fake_now(monkeypatch, FAKE_NOW)
        ec.update_aggregates(sink, today, cfg={})
        (agg,) = ec.load_json(sink / "aggregates" / "signatures.json",
                              {}).values()
        assert agg["count_7d"] == 2  # сегодня (1) + день ровно на границе (1)
        assert agg["count_prev_7d"] == 0


# ── Блок E — endpoints cap (ENDPOINTS_KEEP ↔ литерал среза рендера) ──

class TestEndpointsCapParity:
    def test_e1_render_caps_to_collector_keep(self):
        endpoints = {f"GET /ep{i}": 30 - i for i in range(30)}
        rendered = eq_tool._render_aggregate("s", mini_agg(endpoints=endpoints))
        assert len(rendered["endpoints"]) == ec.ENDPOINTS_KEEP

    def test_e2_cap_literal_scoped_to_render(self):
        # срез-N ищется ТОЛЬКО в теле _render_aggregate: по файлу наивный
        # регекс ловит чужие [:8] sha256-аудита
        caps = re.findall(r"\[:(\d+)\]", tool_render_aggregate_body())
        assert caps == [str(ec.ENDPOINTS_KEEP)]


# ── Блок F — RED-механика (self-test: паритет умеет краснеть) ──

class TestRedInjection:
    def test_reduced_prio_dict_breaks_parity(self):
        """Симуляция дрейфа: усечённый ИЛИ расширенный словарь обязан
        расходиться с каноном — иначе тест зелёный всегда и бесполезен.
        (Оба направления — как в AC2: «добавить P4 / убрать P3».)"""
        canon = set(eq_tool.PRIO_RANK)
        drifted_add = canon | {"P4"}              # «коллектор добавил P4»
        drifted_cut = canon - {max(canon)}         # «потерял старший ключ»
        assert drifted_add != canon               # A1 краснел бы
        assert drifted_cut != canon
        assert canon - drifted_cut                # зубы: потеря наблюдаема
        assert drifted_add - canon == {"P4"}
