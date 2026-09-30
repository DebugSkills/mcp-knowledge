"""Юнит-тесты источника pulled_error_log (Wave 2, трек G): Svyazi error_log.

Спека: pull-таймер на lup складывает JSONL-документы
`ops_events_query --source error_log` (одна строка = документ
{"kind": "top|impact|signature", "rows": […]}); коллектор читает локальный
файл (без network/subprocess). Идентичность события — сигнатура Svyazi
(error_code + детерминированный message БЕЗ cnt/persons — иначе сигнатура
sink «плывёт» каждый цикл). Sink-пути в тестах — только tmp_path.

Запуск: `.venv/bin/python -m pytest tests/test_errors_svyazi_pull.py -v`
(входит в `make test-errors`).
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("errors_collect", ROOT / "scripts" / "errors_collect.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

TS_A_FIRST, TS_A_LAST = "2026-09-28T09:00:00Z", "2026-09-28T10:00:00Z"
TS_B_FIRST, TS_B_LAST = "2026-09-28T09:30:00Z", "2026-09-28T09:40:00Z"

AGG_ROW_A = {"signature": "ValidationError|/api/v1/entries|422|svyazi", "cnt": 12,
             "persons": 3, "first_seen": TS_A_FIRST, "last_seen": TS_A_LAST}
AGG_ROW_B = {"signature": "PermissionError|/admin|403|svyazi", "cnt": 2,
             "persons": 1, "first_seen": TS_B_FIRST, "last_seen": TS_B_LAST}


def _write_jsonl(tmp_path, docs, name="svyazi.jsonl"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(d, ensure_ascii=False) + "\n" for d in docs)
    return p


def _cfg(p, source="svyazi_error_log"):
    return {"pulled_error_log": [{"path": str(p), "source": source, "origin": "svyazi"}]}


# ── агрегаты kind=top/impact: строки → события ──


class TestPulledErrorLogAggregates:
    def test_two_aggregate_rows_two_events(self, tmp_path):
        p = _write_jsonl(tmp_path, [{"kind": "top",
                                     "meta": {"source": "error_log", "since": "30m"},
                                     "rows": [AGG_ROW_A, AGG_ROW_B]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 2
        by_code = {e["error_code"]: e for e in evs}
        a = by_code[AGG_ROW_A["signature"]]
        assert a["source"] == "svyazi_error_log"
        assert a["level"] == "ERROR"
        assert a["ts"] == TS_A_LAST  # ts = last_seen
        assert a["priority_hint"] == "P1"  # persons=3
        assert a["marker"] == "error_log"
        assert a["container"] is None and a["stream"] is None and a["actor_id"] is None
        assert a["message"] == f"[Svyazi error_log] {AGG_ROW_A['signature']}"
        b = by_code[AGG_ROW_B["signature"]]
        assert b["ts"] == TS_B_LAST
        assert b["priority_hint"] == "P2"  # persons=1

    def test_first_seen_fallback(self, tmp_path):
        row = dict(AGG_ROW_B, last_seen=None)
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [row]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1 and evs[0]["ts"] == TS_B_FIRST

    def test_row_without_any_ts_skipped(self, tmp_path):
        row = {"signature": "X|/x|500|svyazi", "cnt": 1, "persons": 0,
               "first_seen": None, "last_seen": None}
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [row]}])
        assert ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p)) == []

    def test_row_with_bad_ts_skipped(self, tmp_path):
        # битый ts не роняет цикл: update_aggregates считает parse_ts(last_seen)
        row = {"signature": "X|/x|500|svyazi", "cnt": 1, "persons": 3,
               "last_seen": "not-a-ts"}
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [row]}])
        assert ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p)) == []

    def test_sink_signature_stable_across_cnt_persons(self, tmp_path):
        # сигнатура sink НЕ «плывёт» от cnt/persons (меняются каждый цикл):
        # message детерминирован — одна sink-сигнатура на сигнатуру Svyazi
        r1 = dict(AGG_ROW_A, cnt=5, persons=1)
        r2 = dict(AGG_ROW_A, cnt=99, persons=7, last_seen="2026-09-28T11:00:00Z")
        e1 = ec.collect_pulled_error_log(
            tmp_path / "s1", {}, _cfg(_write_jsonl(tmp_path, [{"kind": "top", "rows": [r1]}], "a.jsonl")))
        e2 = ec.collect_pulled_error_log(
            tmp_path / "s2", {}, _cfg(_write_jsonl(tmp_path, [{"kind": "top", "rows": [r2]}], "b.jsonl")))
        assert e1[0]["signature"] == e2[0]["signature"]
        assert e1[0]["message"] == e2[0]["message"]


# ── kind=signature: отдельные события (ts из строки) ──


class TestPulledErrorLogSignatureKind:
    def test_signature_row_uses_row_ts(self, tmp_path):
        row = {"signature": "Http404|/api/v1/x|404|svyazi", "ts": "2026-09-28T12:34:56Z",
               "message": "not found"}
        p = _write_jsonl(tmp_path, [{"kind": "signature", "rows": [row]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1
        assert evs[0]["ts"] == "2026-09-28T12:34:56Z"
        assert evs[0]["error_code"] == row["signature"]
        assert evs[0]["message"] == f"[Svyazi error_log] {row['signature']}"

    def test_row_without_signature_uses_dash(self, tmp_path):
        # «не выдумывать»: нет signature → заглушка "-", а не конструктор
        row = {"ts": "2026-09-28T12:34:56Z"}
        p = _write_jsonl(tmp_path, [{"kind": "signature", "rows": [row]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1 and evs[0]["error_code"] == "-"


# ── регресс: PostgreSQL-формат ts с пробельным разделителем ──
# ops_events_query (Svyazi) отдаёт время как "2026-09-22 17:32:02.965000+03:00"
# (пробел вместо RFC3339 "T"); parse_ts принимает только "T". Источник
# нормализует пробел → "T" и кладёт в событие УЖЕ нормализованный ts:
# downstream снова зовёт parse_ts(e["ts"]) при раскладке по дням. Живой дефект
# прода: все строки kind=top молча пропускались → 0 событий с источника.


PG_TS = "2026-09-22 17:32:02.965000+03:00"
PG_TS_NORM = "2026-09-22T17:32:02.965000+03:00"


class TestPulledErrorLogPostgresTs:
    def test_top_row_postgres_space_ts_normalized(self, tmp_path):
        row = {"signature": "http_401|/api/v1/auth/me|401|server", "cnt": 21,
               "persons": 0, "first_seen": "2026-09-21 15:53:04.221000+03:00",
               "last_seen": PG_TS}
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [row]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1
        assert evs[0]["ts"] == PG_TS_NORM  # нормализованный ts с "T"
        assert "T" in evs[0]["ts"] and " " not in evs[0]["ts"]
        # downstream: parse_ts по ts события не падает, смещение учтено
        dt = ec.parse_ts(evs[0]["ts"])
        assert dt == datetime(2026, 9, 22, 14, 32, 2, 965000, tzinfo=timezone.utc)

    def test_signature_row_postgres_space_ts_normalized(self, tmp_path):
        row = {"signature": "http_401|/api/v1/x|401|svyazi", "ts": PG_TS}
        p = _write_jsonl(tmp_path, [{"kind": "signature", "rows": [row]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1
        assert evs[0]["ts"] == PG_TS_NORM
        ec.parse_ts(evs[0]["ts"])  # не падает


# ── offset-state: повторный вызов не дублирует; ротация → с нуля ──


class TestPulledErrorLogOffset:
    def test_second_call_no_duplicates_then_append(self, tmp_path):
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [AGG_ROW_A, AGG_ROW_B]}])
        state = {}
        cfg = _cfg(p)
        assert len(ec.collect_pulled_error_log(tmp_path / "sink", state, cfg)) == 2
        assert ec.collect_pulled_error_log(tmp_path / "sink", state, cfg) == []
        assert state["pulled_error_log"][str(p)]["offset"] == p.stat().st_size
        # докатился новый документ → только его строки
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "top", "rows": [
                dict(AGG_ROW_A, last_seen="2026-09-28T11:00:00Z")]}) + "\n")
        evs = ec.collect_pulled_error_log(tmp_path / "sink", state, cfg)
        assert len(evs) == 1 and evs[0]["ts"] == "2026-09-28T11:00:00Z"

    def test_truncation_rereads_from_zero(self, tmp_path):
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [AGG_ROW_A]}])
        state = {}
        cfg = _cfg(p)
        ec.collect_pulled_error_log(tmp_path / "sink", state, cfg)
        # «ротация»: файл перезаписан меньшим размером (offset > size) → с нуля
        _write_jsonl(tmp_path, [{"kind": "top", "rows": [AGG_ROW_B]}])
        evs = ec.collect_pulled_error_log(tmp_path / "sink", state, cfg)
        assert len(evs) == 1
        assert evs[0]["error_code"] == AGG_ROW_B["signature"]


# ── robustness: битый JSON / отсутствующий файл / обратная совместимость ──


class TestPulledErrorLogRobustness:
    def test_broken_line_skipped_others_processed(self, tmp_path, capsys):
        p = tmp_path / "svyazi.jsonl"
        good = json.dumps({"kind": "top", "rows": [AGG_ROW_A]})
        p.write_text("{not json!!!\n" + good + "\n", encoding="utf-8")
        evs = ec.collect_pulled_error_log(tmp_path / "sink", {}, _cfg(p))
        assert len(evs) == 1
        assert evs[0]["error_code"] == AGG_ROW_A["signature"]
        assert "bad json line" in capsys.readouterr().err

    def test_missing_file_zero_events_no_crash(self, tmp_path):
        cfg = _cfg(tmp_path / "nope.jsonl")
        assert ec.collect_pulled_error_log(tmp_path / "sink", {}, cfg) == []

    def test_default_config_empty_source_disabled(self, tmp_path):
        # обратная совместимость: DEFAULT_CONFIG["pulled_error_log"] == [] → 0 событий
        assert ec.DEFAULT_CONFIG["pulled_error_log"] == []
        assert ec.collect_pulled_error_log(tmp_path / "sink", {}, ec.DEFAULT_CONFIG) == []

    def test_source_key_from_config(self, tmp_path):
        # source приходит из конфига (элемент), не зашит жёстко
        p = _write_jsonl(tmp_path, [{"kind": "top", "rows": [AGG_ROW_A]}])
        evs = ec.collect_pulled_error_log(
            tmp_path / "sink", {}, _cfg(p, source="svyazi_error_log_staging"))
        assert len(evs) == 1 and evs[0]["source"] == "svyazi_error_log_staging"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
