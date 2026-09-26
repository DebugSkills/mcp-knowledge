#!/usr/bin/env python3
"""errors_guard.py — write-side гвард цикла Error→Rule (code-2026-09-23-008, В2).

Канон: .knowledge/docs/observability/self-improvement-loop.md §13.3/§11.3;
спека .boardData.md §7 «storm-guard»; решения оператора OQ 4/4: cap=5/60 с ·
подавленное НЕ в raw + двухканальный перенос · burst=[GUARD] P1-эскалация
БЕЗ немедленных алертов (M7 weekly-only) · suppression-лист пустой по умолчанию.

Три механизма (§7.2–§7.4):
  • cap/sampling — минутные ведра по сигнатуре (ts-based, детерминированные):
    immune-классы проходят безусловно и НЕ выжигают лимит (P2-1); подавленное
    НЕ пишется в raw: suppressed_pending переносится в следующее разрешённое
    событие (suppressed_count + sampled=true), suppressed_delta — в агрегат
    немедленно («подавленное не терять», §13.3:188).
  • burst-детектор — считает ПОЛНЫЙ поток цикла (до гварда): ≥burst_abs(50)
    ИЛИ ×burst_ratio(10) к среднему за burst_window_cycles(12) циклов → маркер
    [GUARD] с ЯВНЫМ priority_hint="burst" (→ P1/T) + burst_ts/burst_count_5m
    жертве; re-arm-стейтмашина armed→fired→held→re-armed, кулдаун 24 цикла —
    только анти-флаппинг НОВОГО срабатывания (дедуп внутри held безусловен).
  • suppression-лист — ${SINK}/suppression.json: ключ ТОЛЬКО точная сигнатура
    (никогда диапазон кодов/regex — §13.3:189); traceback не глушится абсолютно;
    4xx-с-актором — лишь явной записью оператора (reason+audit.jsonl).

Состояние — в collector_state.json (guard/burst-секции; прецедент breached,
R7: отдельный state-файл гварда НЕ создаётся). Kill-switch guard.enabled=false —
поведение бит-в-бит прежнее (state не трогается). Python ≥3.9, stdlib-only.
"""

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (  # sibling-импорт по прецеденту errors_report.py:31
    DATA_ROOT,
    atomic_write_json,
    load_json,
    make_event,
    now_iso,
    parse_ts,
)

# §7.2 матрица иммунитета (заморожена спекой): свой дедуп + P0-lifecycle
GUARD_IMMUNE_SOURCES = frozenset({"host", "health", "docker_events"})
GUARD_COOLDOWN_CYCLES = 24  # ≈2 ч при cron */5 — анти-флаппинг re-arm (§7.3)
SUPPRESSION_FILE = "suppression.json"
AUDIT_FILE = "audit.jsonl"


def _is_4xx_with_actor(ev) -> bool:
    """user-impact (§13.3:190): 4xx-ответ конкретному актору."""
    st = ev.get("status")
    return bool(ev.get("actor_id")) and isinstance(st, int) and 400 <= st < 500


def _is_cap_immune(ev) -> bool:
    """Автоматикой НЕ глушатся: host/health/docker_events (свой дедуп, R5),
    traceback (драгоценный контекст), [GUARD]-маркер, 4xx-с-актором."""
    if ev.get("source") in GUARD_IMMUNE_SOURCES:
        return True
    if ev.get("priority_hint") == "traceback":
        return True
    if ev.get("marker") == "GUARD":
        return True
    return _is_4xx_with_actor(ev)


def _is_suppression_immune(ev) -> bool:
    """Лист НЕ глушит: traceback — абсолютно; host/health/docker_events и
    [GUARD] — иммунны (§7.2 матрица). 4xx-с-актором — только явной записью."""
    if ev.get("priority_hint") == "traceback":
        return True
    if ev.get("source") in GUARD_IMMUNE_SOURCES:
        return True
    return ev.get("marker") == "GUARD"


def _suppression_active(entry, today) -> bool:
    """until=null (unchanged) — бессрочно; YYYY-MM-DD >= сегодня — активно."""
    until = entry.get("until")
    return not until or str(until) >= today


# ── Burst-детектор «×N за 5 мин» (§7.3, пробел «б») ──

def detect_bursts(full_counts, burst_state, cfg, now=None, routine_sigs=None):
    """Полный поток цикла → ([GUARD]-маркеры, burst_delta жертвам).

    Мутирует burst_state[sig] = {history≤12, state, last_fire_ts,
    cycles_since_fire, last_seen}. Срабатывание: count ≥ burst_abs ИЛИ
    (mean_prev ≥ 1 И count ≥ burst_ratio × mean_prev). Held — маркер не
    дублируется, burst_ts не обновляется; спад ≥1 цикл → re-armed; новое
    срабатывание — только после кулдана 24 цикла (анти-флаппинг).

    029-A4: routine_sigs (optional) — сигнатуры, чьи события все expected=True.
    Routine-шторм → маркер-литерал ``[GUARD] burst_routine: …`` с
    priority_hint="burst_routine" (→ P2/T, не P1/TG). Error-шторм — путь
    hint="burst" без изменений. Поле ``routine`` сохраняется в burst_delta.
    """
    g = cfg.get("guard") or {}
    burst_abs = int(g.get("burst_abs", 50))
    burst_ratio = float(g.get("burst_ratio", 10))
    window = int(g.get("burst_window_cycles", 12))
    ttl_days = int(g.get("state_ttl_days", 7))
    now = now or now_iso()
    routine_sigs = routine_sigs or set()
    markers, burst_delta = [], {}
    for sig in sorted(full_counts):
        count = full_counts[sig]
        st = burst_state.get(sig) or {"history": [], "state": "armed",
                                      "last_fire_ts": "", "cycles_since_fire": None,
                                      "last_seen": now}
        burst_state[sig] = st
        st["last_seen"] = now
        history = st.get("history") or []
        mean_prev = (sum(history) / len(history)) if history else 0.0
        threshold = count >= burst_abs or (mean_prev >= 1.0 and count >= burst_ratio * mean_prev)
        history.append(count)
        st["history"] = history[-window:]
        state, fired = st.get("state", "armed"), False
        if state in ("fired", "held"):
            st["state"] = "held" if threshold else "re-armed"
        elif threshold:  # armed | re-armed: кулдаун — гейт нового срабатывания
            since = st.get("cycles_since_fire")
            if since is None or since >= GUARD_COOLDOWN_CYCLES:
                routine = bool(sig in routine_sigs)
                if routine:
                    markers.append(make_event(
                        now, "guard",
                        f"[GUARD] burst_routine: {sig[:120]} count={count}/5min (threshold)",
                        level="ERROR", marker="GUARD", priority_hint="burst_routine",
                    ))
                else:
                    markers.append(make_event(
                        now, "guard",
                        f"[GUARD] burst: {sig[:120]} count={count}/5min (threshold)",
                        level="ERROR", marker="GUARD", priority_hint="burst",
                    ))
                st["state"], st["last_fire_ts"] = "fired", now
                st["cycles_since_fire"] = 0
                burst_delta[sig] = {"burst_ts": now, "burst_count_5m": count,
                                    "routine": routine}
                fired = True
            else:
                st["state"] = "held"  # порог есть, кулдаун не прошёл
        if not fired and st.get("cycles_since_fire") is not None:
            st["cycles_since_fire"] += 1
    # TTL: неактивные burst-записи не копим (R1 — state не распухает)
    cutoff = (parse_ts(now) - timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sig in [s for s, st in burst_state.items()
                if not st.get("last_seen") or st["last_seen"] < cutoff]:
        burst_state.pop(sig, None)
    return markers, burst_delta


# ── Cap/sampling: минутные ведра + suppression (§7.2, §7.4) ──

def _suppress(ev, sig, guard_state, suppressed_delta):
    """Подавленное НЕ пишется в raw: pending (перенос) + delta (агрегат немедленно)."""
    st = guard_state.get(sig) or {"minute": ev["ts"][:16], "allowed": 0,
                                  "suppressed_pending": 0}
    guard_state[sig] = st
    st["suppressed_pending"] = st.get("suppressed_pending", 0) + 1
    st["last_seen"] = max(st.get("last_seen", ""), ev["ts"])
    suppressed_delta[sig] = suppressed_delta.get(sig, 0) + 1


def _ttl_cleanup(guard_state, ttl_days, now):
    """Записи с pending==0 и last_seen старше TTL удаляются (§7.2)."""
    now_dt = parse_ts(now)
    for sig in [s for s, st in guard_state.items()
                if not st.get("suppressed_pending")
                and (not st.get("last_seen")
                     or (now_dt - parse_ts(st["last_seen"])).days >= ttl_days)]:
        guard_state.pop(sig, None)


def apply_write_guard(events, state, cfg, suppression=None, now=None):
    """→ (allowed, markers, suppressed_delta, burst_delta); мутирует state.

    Единственная точка вызова — errors_collect.main (между
    mark_expected_restarts и append_events). События меняются на месте только
    аддитивно (suppressed_count/sampled). Immune-события проходят в raw
    безусловно, НЕ инкрементируют allowed и НЕ трогают pending (P2-1).
    Minute rollover: allowed сбрасывается, suppressed_pending ПЕРЕНОСИТСЯ.
    """
    g = cfg.get("guard") or {}
    if not g.get("enabled", True):
        return list(events), [], {}, {}  # kill-switch: бит-в-бит прежнее поведение
    now = now or now_iso()
    # burst считает ПОЛНЫЙ поток (до гварда) — иначе гвард съест свой сигнал
    full_counts = {}
    # 029-A1: routine_sigs — сигнатуры, чьи события все expected=True
    # (сгруппированы по сигнатуре для detect_bursts).
    sig_events = {}
    for ev in events:
        sig = ev["signature"]
        full_counts[sig] = full_counts.get(sig, 0) + 1
        sig_events.setdefault(sig, []).append(ev)
    routine_sigs = {sig for sig, evs in sig_events.items()
                    if all(e.get("expected", False) for e in evs)}
    markers, burst_delta = detect_bursts(full_counts, state.setdefault("burst", {}),
                                         cfg, now, routine_sigs=routine_sigs)
    suppression = suppression or {}
    today = now[:10]
    cap = int(g.get("cap_per_minute", 5))
    guard_state = state.setdefault("guard", {})
    allowed, suppressed_delta = [], {}
    for ev in events:
        sig = ev["signature"]
        entry = suppression.get(sig)
        if entry and _suppression_active(entry, today) and not _is_suppression_immune(ev):
            _suppress(ev, sig, guard_state, suppressed_delta)  # осознанное глушение
            continue
        if _is_cap_immune(ev):
            allowed.append(ev)
            continue
        st = guard_state.get(sig) or {}
        minute = ev["ts"][:16]
        if st.get("minute") != minute:  # новое минутное ведро (ts-based, R3)
            st = {"minute": minute, "allowed": 0,
                  "suppressed_pending": st.get("suppressed_pending", 0)}
            guard_state[sig] = st
        st["last_seen"] = max(st.get("last_seen", ""), ev["ts"])
        if st["allowed"] >= cap:
            _suppress(ev, sig, guard_state, suppressed_delta)
            continue
        st["allowed"] += 1
        pending = st.get("suppressed_pending", 0)
        if pending > 0:  # двухканальный перенос: канал события (§7.2)
            ev["suppressed_count"] = pending
            ev["sampled"] = True
            st["suppressed_pending"] = 0
        allowed.append(ev)
    _ttl_cleanup(guard_state, int(g.get("state_ttl_days", 7)), now)
    return allowed, markers, suppressed_delta, burst_delta


# ── Suppression-лист known-noise: файл + CLI + audit (§7.4) ──

def load_suppression(sink) -> dict:
    data = load_json(Path(sink) / SUPPRESSION_FILE, {})
    return data if isinstance(data, dict) else {}


def _audit(sink, action, sig, reason, until, actor) -> None:
    """append в audit.jsonl (значений событий НЕ пишем — §13.1-3)."""
    path = Path(sink) / AUDIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "action": action, "sig": sig,
                            "reason": reason, "until": until, "actor": actor},
                           ensure_ascii=False) + "\n")


# 028-B5: публичное имя для переиспользования (resolve в errors_alert.py).
# Приватное `_audit` сохранено как алиас — обратная совместимость тестов/вызовов.
audit_event = _audit


def cli_add(sink, sig, reason, until, actor) -> int:
    data = load_suppression(sink)
    data[sig] = {"reason": reason, "until": until, "added_at": now_iso(),
                 "added_by": actor}
    atomic_write_json(Path(sink) / SUPPRESSION_FILE, data)
    _audit(sink, "add", sig, reason, until, actor)
    print(f"suppressed: {sig[:120]} (until={until or 'unchanged'}; reason={reason})")
    return 0


def cli_remove(sink, sig, actor) -> int:
    data = load_suppression(sink)
    if sig not in data:
        print(f"not in suppression list: {sig[:120]}")
        return 1
    rec = data.pop(sig)
    atomic_write_json(Path(sink) / SUPPRESSION_FILE, data)
    _audit(sink, "remove", sig, rec.get("reason"), rec.get("until"), actor)
    print(f"removed: {sig[:120]}")
    return 0


def cli_list(sink) -> int:
    data = load_suppression(sink)
    if not data:
        print("(suppression-лист пуст — канонический дефолт 008, OQ-4)")
        return 0
    today = now_iso()[:10]
    for sig, rec in sorted(data.items()):
        state = "active" if _suppression_active(rec, today) else "EXPIRED"
        print(f"[{state}] {sig[:150]}")
        print(f"    reason={rec.get('reason')} until={rec.get('until') or 'unchanged'} "
              f"added_by={rec.get('added_by')} at={rec.get('added_at')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="errors_guard — suppression-лист known-noise (008); гвард цикла — в errors_collect")
    ap.add_argument("--sink", default=None,
                    help="override каталога sink (дефолт $DATA_ROOT/logs/errors)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="глушить ТОЧНУЮ сигнатуру (regex/коды запрещены)")
    p_add.add_argument("sig")
    p_add.add_argument("--reason", required=True, help="зачем (идёт в audit.jsonl)")
    p_add.add_argument("--until", default=None, help="YYYY-MM-DD (нет = бессрочно)")
    p_rem = sub.add_parser("remove", help="снять глушение")
    p_rem.add_argument("sig")
    sub.add_parser("list", help="показать лист")
    args = ap.parse_args(argv)
    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    actor = os.environ.get("USER") or getpass.getuser()
    if args.cmd == "add":
        if args.until:
            try:
                datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                print("--until должен быть датой YYYY-MM-DD")
                return 1
        return cli_add(sink, args.sig, args.reason, args.until, actor)
    if args.cmd == "remove":
        return cli_remove(sink, args.sig, actor)
    return cli_list(sink)


if __name__ == "__main__":
    sys.exit(main())
