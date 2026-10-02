"""T1-2 (Ф-A1): build_digest() — утренняя сводка по хостам.

AC (мастер §T1 T1-2 + REV.2/REV.3/REV.3.1):
  origin-группировка (source → host; поля host нет) · цветовая политика
  (P0/P1→🔴, P2→⚠️, только-P3→✅, пустой sink) · активное окно
  (count_7d>0 ∧ status!=resolved) · маркер свежести last_seen + сортировка
  приоритет→свежесть (фикстура 6д НЕ выглядит сегодняшней) · suppression-фильтр
  ДО цвета/счётчиков (единый SSOT-парсер suppression_filters/load_suppression) +
  ⏸-сводка · невакуумный детектор (снятие фильтра рушит тест) · data-driven
  свойство (новая запись suppression.json чистит дайджест без изменения кода).
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


er = _load("errors_report_digest", "scripts/errors_report.py")

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


def _ago(now, days=0, hours=0):
    return (now - timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agg(priority="P1", status="active", last_seen=None, count_7d=3, sources=None, **extra):
    a = {
        "priority": priority, "class": "T", "status": status,
        "first_seen": "2026-09-01T00:00:00Z",
        "last_seen": last_seen or _ago(NOW, days=1),
        "count_total": count_7d, "count_7d": count_7d, "count_prev_7d": 0,
        "daily": {}, "actors": [], "sources": sources or ["docker_logs"],
        "last_example": {"message": "example"},
    }
    a.update(extra)
    return a


# ── AC1: origin-группировка ──

def test_origin_split(tmp_path):
    """Разрез по source: svyazi_error_log → chpd (Svyazi), остальное → lup (knowledge)."""
    aggs = {
        "docker_logs|T|err-a": _agg(priority="P1", sources=["docker_logs"]),
        "svyazi_error_log|T|err-b": _agg(priority="P2", sources=["svyazi_error_log"]),
    }
    text = er.render_digest(aggs, {}, now=NOW)
    assert "lup (knowledge): 1 активных" in text
    assert "chpd (Svyazi): 1 активных" in text
    # source-префикс не придумывает host-поле: только sources-разрез
    assert "T|err-a" in text and "T|err-b" in text


# ── AC2: цветовая политика ──

def test_colors(tmp_path):
    r = er.render_digest({"s|A|red": _agg(priority="P0")}, {}, now=NOW)
    assert "🔴 lup (knowledge)" in r
    w = er.render_digest({"s|A|warn": _agg(priority="P2")}, {}, now=NOW)
    assert "⚠️ lup (knowledge)" in w
    g = er.render_digest({"s|A|noise": _agg(priority="P3")}, {}, now=NOW)
    assert "🔴" not in g and "⚠️" not in g
    assert "✅ Итог: 0 активных" in g
    assert "🔇 шум P3: 1 сигнатур (lup (knowledge))" in g


def test_empty_sink(tmp_path):
    """Пустой sink → зелёный вывод (эталон оператора — отчёт бэкапов)."""
    assert er.render_digest({}, {}, now=NOW) == "✅ Проблем нет"


def test_active_window_excludes_resolved_and_zero(tmp_path):
    """Активное окно: resolved и count_7d=0 не видны в дайджесте (§4.1)."""
    aggs = {
        "docker_logs|T|resolved": _agg(priority="P0", status="resolved", count_7d=5),
        "docker_logs|T|zero": _agg(priority="P0", count_7d=0),
        "docker_logs|T|active": _agg(priority="P0", count_7d=3),
    }
    text = er.render_digest(aggs, {}, now=NOW)
    assert "T|resolved" not in text and "T|zero" not in text
    assert "T|active" in text
    assert "lup (knowledge): 1 активных" in text


# ── AC3: маркер свежести + сортировка приоритет→свежесть ──

def test_freshness_marker_stale(tmp_path):
    """last_seen=6д → «6д назад», НЕ сегодняшняя (P1-2)."""
    stale = _ago(NOW, days=6)
    aggs = {"docker_logs|GIN|POST /api/embed": _agg(
        priority="P1", count_7d=16054, last_seen=stale)}
    text = er.render_digest(aggs, {}, now=NOW)
    assert "6д назад" in text
    assert "сейчас" not in text
    assert "0с назад" not in text and "0м назад" not in text and "0ч назад" not in text


def test_sort_priority_then_freshness(tmp_path):
    """Топ-3 сортируется (priority ASC, last_seen DESC) — R2.3."""
    aggs = {
        "docker_logs|T|p2-fresh": _agg(priority="P2", last_seen=_ago(NOW, hours=1)),
        "docker_logs|T|p1-old": _agg(priority="P1", last_seen=_ago(NOW, days=3)),
        "docker_logs|T|p1-fresh": _agg(priority="P1", last_seen=_ago(NOW, hours=1)),
    }
    text = er.render_digest(aggs, {}, now=NOW)
    i_fresh = text.index("p1-fresh")
    i_old = text.index("p1-old")
    i_p2 = text.index("p2-fresh")
    assert i_fresh < i_old < i_p2


# ── AC4: suppression-фильтр (единый SSOT-парсер) + ⏸-сводка ──

GIN_SIG = "docker_logs|GIN|POST /api/embed"
GIN_SHORT = "GIN|POST /api/embed"


def test_suppressed_excluded_from_top(tmp_path):
    """Заглушённая сигнатура исключается из цветных топов, но правда в ⏸-строке."""
    aggs = {GIN_SIG: _agg(priority="P1", count_7d=16054, last_seen=_ago(NOW, hours=1),
                          suppressed_daily={"2026-09-30": 100})}
    supp = {GIN_SIG: {"reason": "рутинный трафик", "until": None}}
    text = er.render_digest(aggs, supp, now=NOW)
    assert GIN_SHORT not in text
    assert "⏸ suppression: 1 сигнатур / 100 событий за 24ч" in text
    assert "рутинный трафик" in text


def test_suppression_summary_line(tmp_path):
    """⏸-строка: N записей с M>0 в окне / M событий / топ-причины."""
    aggs = {
        GIN_SIG: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1),
                      suppressed_daily={"2026-09-30": 3}),
        "docker_logs|T|zero-ev": _agg(priority="P1", count_7d=5,
                                      last_seen=_ago(NOW, hours=2),
                                      suppressed_daily={"2026-09-01": 7}),  # вне окна
    }
    supp = {GIN_SIG: {"reason": "r1", "until": None},
            "docker_logs|T|zero-ev": {"reason": "r2", "until": None}}
    text = er.render_digest(aggs, supp, now=NOW)
    # только GIN имеет M>0 в окне → N=1 (запись без событий N не раздувает)
    assert "⏸ suppression: 1 сигнатур / 3 событий за 24ч" in text


def test_suppression_filter_detector(tmp_path):
    """Невакуумный детектор: с/без suppression на одной фикстуре → разный вывод."""
    aggs = {GIN_SIG: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1),
                          suppressed_daily={"2026-09-30": 3})}
    supp = {GIN_SIG: {"reason": "r", "until": None}}
    with_supp = er.render_digest(aggs, supp, now=NOW)
    without_supp = er.render_digest(aggs, {}, now=NOW)
    assert GIN_SHORT not in with_supp
    assert GIN_SHORT in without_supp
    assert with_supp != without_supp


def test_suppression_removes_host_red(tmp_path):
    """Фильтр ДО цвета/счётчиков: заглушённая P1 не делает хост/дайджест 🔴."""
    aggs = {GIN_SIG: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1),
                          suppressed_daily={"2026-09-30": 3})}
    supp = {GIN_SIG: {"reason": "r", "until": None}}
    text = er.render_digest(aggs, supp, now=NOW)
    # (а) сигнатуры нет в топах
    assert GIN_SHORT not in text
    # (б) хост НЕ 🔴 — счётчик P1 после фильтра = 0, цвет по пост-фильтровым счётчикам
    assert "🔴 lup" not in text
    assert "✅ Итог: 0 активных" in text
    # (в) ⏸-строка N=1, M>0
    assert "⏸ suppression: 1 сигнатур / 3 событий за 24ч" in text
    # без suppression — хост красный (снятие фильтра → ассерт (б) падает)
    text2 = er.render_digest(aggs, {}, now=NOW)
    assert "🔴 lup (knowledge): 1 активных" in text2


def test_suppression_data_driven_no_code_change(tmp_path):
    """Новая запись suppression.json чистит дайджест без изменения кода."""
    sig1, sig2 = "docker_logs|T|a", "docker_logs|T|b"
    aggs = {
        sig1: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1),
                   suppressed_daily={"2026-09-30": 2}),
        sig2: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=2),
                   suppressed_daily={"2026-09-30": 4}),
    }
    t1 = er.render_digest(aggs, {sig1: {"reason": "r1", "until": None}}, now=NOW)
    t2 = er.render_digest(aggs, {sig1: {"reason": "r1", "until": None},
                                 sig2: {"reason": "r2", "until": None}}, now=NOW)
    # k=1: sig1 вне топа, sig2 в топе; ⏸ N=1 M=2
    assert "T|a" not in t1 and "T|b" in t1
    assert "1 сигнатур / 2 событий" in t1
    # k=2: обе вне топа; ⏸ N=2 M=6 (тот же код, только данные)
    assert "T|a" not in t2 and "T|b" not in t2
    assert "2 сигнатур / 6 событий" in t2


def test_suppression_expired_not_filtered(tmp_path):
    """Истёкшая/битая suppression → НЕ фильтрует (parse-guard suppression_filters)."""
    sig = "docker_logs|T|expired"
    aggs = {sig: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1))}
    # today (MSK) = 2026-09-30; until=2026-09-29 — истекла
    expired = er.render_digest(aggs, {sig: {"reason": "r", "until": "2026-09-29"}}, now=NOW)
    assert "T|expired" in expired and "🔴 lup" in expired
    # битый until → не фильтрует
    broken = er.render_digest(aggs, {sig: {"reason": "r", "until": "not-a-date"}}, now=NOW)
    assert "T|expired" in broken and "🔴 lup" in broken


def test_digest_marker_shared(tmp_path):
    """DIGEST_MARKER — единый источник титула (P3-5; не дубль-литерал)."""
    assert er.DIGEST_MARKER == "🛰 Ошибки"
    text = er.render_digest({"s|A|x": _agg(priority="P1")}, {}, now=NOW)
    assert text.startswith(er.DIGEST_MARKER + " · ")


# ── build_digest: чтение sink (интеграция load_aggregates + load_suppression) ──

def test_build_digest_reads_sink(tmp_path):
    """build_digest(sink) читает aggregates + suppression.json (единый SSOT)."""
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "aggregates" / "signatures.json").write_text(json.dumps({
        GIN_SIG: _agg(priority="P1", count_7d=5, last_seen=_ago(NOW, hours=1),
                      suppressed_daily={"2026-09-30": 3}),
    }), encoding="utf-8")
    (tmp_path / "suppression.json").write_text(json.dumps({
        GIN_SIG: {"reason": "r", "until": None},
    }), encoding="utf-8")
    text = er.build_digest(tmp_path, now=NOW, full=True)
    assert GIN_SHORT not in text  # suppression прочитан из файла
    assert "✅ Итог: 0 активных" in text
    assert "⏸ suppression: 1 сигнатур / 3 событий за 24ч" in text


# ── Короткая форма дайджеста (решение оператора 02.10.2026: счётчик по хостам) ──

def test_short_counts_and_colors_per_host():
    """Строка на хост: 🔴 при P0/P1 · ⚠️ только P2; ничего лишнего в сообщении."""
    aggs = {
        "docker_logs|T|a": _agg(priority="P1", sources=["docker_logs"]),
        "docker_logs|T|b": _agg(priority="P2", sources=["docker_logs"]),
        "svyazi_error_log|T|c": _agg(priority="P2", sources=["svyazi_error_log"]),
    }
    lines = er.render_digest_short(aggs, {}, now=NOW).splitlines()
    assert lines[0].startswith(er.DIGEST_MARKER)
    assert lines[1] == "🔴 lup (knowledge): 2"
    assert lines[2] == "⚠️ chpd (Svyazi): 1"
    assert len(lines) == 3


def test_short_empty_is_green_with_both_hosts():
    """Пусто → обе строки нулевые + «✅ Проблем нет» (эталон оператора — отчёт бэкапов)."""
    text = er.render_digest_short({}, {}, now=NOW)
    assert "✅ lup (knowledge): 0" in text
    assert "✅ chpd (Svyazi): 0" in text
    assert text.rstrip().endswith("✅ Проблем нет")


def test_short_excludes_suppressed_and_p3():
    """Suppression и P3-шум в счётчик короткой формы не входят."""
    aggs = {
        "s|A|noise": _agg(priority="P1", sources=["svyazi_error_log"]),
        "s|A|p3": _agg(priority="P3", sources=["svyazi_error_log"]),
    }
    supp = {"s|A|noise": {"reason": "r", "until": None}}
    text = er.render_digest_short(aggs, supp, now=NOW)
    assert "chpd (Svyazi): 0" in text
    assert "🔴" not in text and "⚠️" not in text


def test_short_has_no_detail_lines():
    """В короткой форме нет топов/P3-шума/suppression/итога."""
    aggs = {"docker_logs|T|a": _agg(priority="P0")}
    text = er.render_digest_short(aggs, {}, now=NOW)
    for token in ("топ", "🔇", "⏸", "Итог"):
        assert token not in text
    assert "🔴 lup (knowledge): 1" in text


def test_build_digest_default_is_short_full_keeps_details(tmp_path):
    """build_digest: дефолт — короткая форма; full=True — подробная (топы/итог)."""
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "aggregates" / "signatures.json").write_text(json.dumps({
        "docker_logs|T|err": _agg(priority="P1", last_seen=_ago(NOW, hours=1)),
    }), encoding="utf-8")
    short = er.build_digest(tmp_path, now=NOW)
    full = er.build_digest(tmp_path, now=NOW, full=True)
    assert "🔴 lup (knowledge): 1" in short and "Итог" not in short
    assert "Итог" in full and "топ:" in full
