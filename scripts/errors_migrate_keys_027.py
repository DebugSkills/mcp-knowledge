#!/usr/bin/env python3
"""errors_migrate_keys_027.py — M-A′ key-migration merge (трасса code-2026-09-26-027).

Зачем. D1 (маскирование длительностей) изменил ключи сигнатур: исторические
ключи, различавшиеся ТОЛЬКО цифрами `dur=`/латентности, слипаются в один
(замер: 14 018 GIN-сигнатур из 14 201). Ребилд из raw ОТВЕРГНУТ как lossy
(raw пост-гвардовый: suppressed-события и burst-поля в raw отсутствуют,
`update_aggregates` пишет `day = now` ⇒ 17 дней схлопнулись бы в один бак).

Что делает (только перенос ключей, без ребилда):
  1. Карта `old_key → new_key` строится по `aggregates.last_example.message`
     (единственное место, где живёт исходная строка; в alert_state её нет).
  2. `aggregates`: слияние записей с одинаковым new_key с СОХРАНЕНИЕМ
     count_total/daily/suppressed_total/burst_*/first_seen/last_seen/actors/
     sources/last_example/has_non_routine/slow; priority — максимум severity.
  3. `alert_state`: переименование по карте; коллизии — консервативный merge
     (status: regressed > investigating > new/known > resolved).
  4. Орфаны alert_state (нет пары в aggregates) НЕ удаляются — в манифест.
  5. Бэкап обоих файлов + манифест орфанов в .trash/ (обратимость).

Безопасность:
  * dry-run по умолчанию (печатает группы слияния, орфаны, дельты размеров);
  * идемпотентность: повторный прогон = no-op (файлы не перезаписываются);
  * лок коллектора: если collector_state.json писался < LOCK_FRESH_SECONDS назад
    (cron */5), запись требует ещё и --force (read-modify-write vs cron).

Выход: 0 — dry-run/успех/no-op; 1 — ошибка/лок без --force.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (
    DATA_ROOT,
    PROJECT_DIR,
    atomic_write_json,
    deep_normalize,
    load_json,
)

LOCK_FRESH_SECONDS = 300
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STATUS_ORDER = {"regressed": 0, "investigating": 1, "active": 2, "new": 2,
                "known": 3, "resolved": 4}


def new_key_for(old_key: str, last_example: dict | None):
    """old_key → new_key по last_example.message; None, если пересчёт невозможен."""
    if not last_example or not last_example.get("message"):
        return None
    parts = old_key.split("|", 2)
    if len(parts) != 3:
        return None
    source, key_mid, _old = parts
    return f"{source}|{key_mid}|{deep_normalize(last_example['message'])}"


def _merge_agg(dst: dict, src: dict) -> dict:
    """Слияние агрегатов с сохранением счётчиков (порядок: dst — база)."""
    out = dict(dst)
    out["count_total"] = int(dst.get("count_total", 0)) + int(src.get("count_total", 0))
    daily = dict(dst.get("daily") or {})
    for d, n in (src.get("daily") or {}).items():
        daily[d] = daily.get(d, 0) + n
    out["daily"] = daily
    out["suppressed_total"] = int(dst.get("suppressed_total", 0)) + int(src.get("suppressed_total", 0))
    out["first_seen"] = min(x for x in (dst.get("first_seen"), src.get("first_seen")) if x)
    out["last_seen"] = max(x for x in (dst.get("last_seen"), src.get("last_seen")) if x)
    out["actors"] = sorted(set(dst.get("actors") or []) | set(src.get("actors") or []))[:50]
    out["sources"] = sorted(set(dst.get("sources") or []) | set(src.get("sources") or []))
    # last_example — самый свежий
    de, se = dst.get("last_example") or {}, src.get("last_example") or {}
    out["last_example"] = se if (se.get("ts") or "") > (de.get("ts") or "") else de
    # флаги — OR (иначе D2-routine «отменит» историю не-рутины)
    out["has_non_routine"] = bool(dst.get("has_non_routine") or src.get("has_non_routine"))
    out["slow"] = bool(dst.get("slow") or src.get("slow"))
    out["burst"] = bool(dst.get("burst") or src.get("burst"))
    if dst.get("burst_ts") or src.get("burst_ts"):
        out["burst_ts"] = max(x for x in (dst.get("burst_ts"), src.get("burst_ts")) if x)
    if dst.get("burst_count_5m") or src.get("burst_count_5m"):
        out["burst_count_5m"] = max(dst.get("burst_count_5m", 0), src.get("burst_count_5m", 0))
    # severity — максимум; status: active побеждает
    prios = [p for p in (dst.get("priority"), src.get("priority")) if p]
    out["priority"] = min(prios, key=lambda p: PRIORITY_ORDER.get(p, 9)) if prios else dst.get("priority")
    statuses = [s for s in (dst.get("status"), src.get("status")) if s]
    out["status"] = "active" if "active" in statuses else (statuses[0] if statuses else dst.get("status"))
    ts_vals = [v for v in (dst.get("fixed_at"), src.get("fixed_at")) if v]
    # active ⇒ fixed_at=None (иначе weekly пометит ложный regressed при следующем цикле)
    out["fixed_at"] = None if out["status"] == "active" else (max(ts_vals) if ts_vals else None)
    out["class"] = dst.get("class") or src.get("class")
    return out


def _merge_alert(dst: dict, src: dict) -> dict:
    """Консервативный merge alert-записей (статус по приоритету)."""
    out = dict(dst)
    s_pri = min((dst.get("status"), src.get("status")),
                key=lambda s: STATUS_ORDER.get(s, 9)) if dst.get("status") or src.get("status") else None
    out["status"] = s_pri
    out["first_seen"] = min(x for x in (dst.get("first_seen"), src.get("first_seen")) if x)
    out["last_seen"] = max(x for x in (dst.get("last_seen"), src.get("last_seen")) if x)
    out["investigating"] = bool(dst.get("investigating") or src.get("investigating"))
    out["reported_by_user"] = bool(dst.get("reported_by_user") or src.get("reported_by_user"))
    weeks = [w for w in (dst.get("last_reported_week"), src.get("last_reported_week")) if w]
    out["last_reported_week"] = max(weeks) if weeks else None
    for f in ("cooldown_until", "alert_cooldown_until", "p0_alerted_at",
              "burst_alerted_at", "fixed_at"):
        vals = [v for v in (dst.get(f), src.get(f)) if v]
        out[f] = max(vals) if vals else None
    if out["status"] == "regressed":
        out["fixed_at"] = None  # рецидив обнуляет фиксацию (как classify_weekly)
    return out


def migrate(aggs: dict, alert: dict):
    """→ (new_aggs, new_alert, stats). Чистая функция (без I/O) — тестируема."""
    # 1. карта old → new
    mapping, underivable = {}, []
    for old_key, a in aggs.items():
        nk = new_key_for(old_key, a.get("last_example"))
        if nk is None:
            underivable.append(old_key)
            continue
        mapping[old_key] = nk

    # 2. aggregates: слияние по new_key
    new_aggs, groups = {}, 0
    for old_key, a in aggs.items():
        nk = mapping.get(old_key, old_key)  # невыводимый — как есть
        if nk in new_aggs:
            new_aggs[nk] = _merge_agg(new_aggs[nk], a)
            groups += 1
        else:
            new_aggs[nk] = dict(a)

    # 3. alert_state: переименование + merge коллизий
    new_alert, a_groups = {}, 0
    for old_key, st in alert.items():
        if not isinstance(st, dict):
            new_alert[old_key] = st  # служебные (_alerts_meta)
            continue
        nk = mapping.get(old_key, old_key)
        if nk in new_alert:
            new_alert[nk] = _merge_alert(new_alert[nk], st)
            a_groups += 1
        else:
            new_alert[nk] = dict(st)

    orphans = sorted(k for k in new_alert
                     if k not in new_aggs and isinstance(new_alert[k], dict))
    stats = {"mapping": len(mapping), "underivable": underivable,
             "agg_groups": groups, "alert_groups": a_groups,
             "agg_before": len(aggs), "agg_after": len(new_aggs),
             "alert_before": len(alert), "alert_after": len(new_alert),
             "orphans": orphans}
    return new_aggs, new_alert, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Error→Rule: key-migration merge 027 (dry-run default)")
    ap.add_argument("--sink", default=None)
    ap.add_argument("--confirm", action="store_true", help="реальная запись")
    ap.add_argument("--force", action="store_true",
                    help="игнорировать свежий лок коллектора (cron */5 пишет прямо сейчас)")
    ap.add_argument("--trash-dir", default=None,
                    help="база бэкапов (дефолт <репо-корень>/.trash)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    agg_path = sink / "aggregates" / "signatures.json"
    alert_path = sink / "alert_state.json"
    aggs = load_json(agg_path, {})
    alert = load_json(alert_path, {})
    new_aggs, new_alert, st = migrate(aggs, alert)

    print(f"=== errors migrate-keys-027 · sink={sink} ===")
    print(f"карта: {st['mapping']} ключей; невыводимых: {len(st['underivable'])}")
    print(f"aggregates: {st['agg_before']} → {st['agg_after']} "
          f"(слито групп: {st['agg_groups']})")
    print(f"alert_state: {st['alert_before']} → {st['alert_after']} "
          f"(слито групп: {st['alert_groups']})")
    print(f"орфаны alert_state (нет пары в aggregates, НЕ удаляем): {len(st['orphans'])}")
    for o in st["orphans"][:5]:
        print(f"  ⚠ {o[:110]}")
    for u in st["underivable"][:5]:
        print(f"  ⚠ без last_example (ключ оставлен как есть): {u[:100]}")

    changed = (json.dumps(new_aggs, sort_keys=True) != json.dumps(aggs, sort_keys=True)
               or json.dumps(new_alert, sort_keys=True) != json.dumps(alert, sort_keys=True))
    if not changed:
        print("NO-OP: ключи уже нормализованы (идемпотентность) — записи не будет.")
        return 0
    if not args.confirm:
        print("DRY-RUN: запись не выполнена (--confirm для применения; "
              "перед реальным прогоном остановить cron коллектора).")
        return 0

    # лок коллектора: cron */5 мог писать state только что
    state_file = sink / "collector_state.json"
    if state_file.exists() and not args.force:
        age = datetime.now(timezone.utc).timestamp() - state_file.stat().st_mtime
        if age < LOCK_FRESH_SECONDS:
            print(f"ЛОК: collector_state.json писался {int(age)} с назад "
                  f"(< {LOCK_FRESH_SECONDS} с) — останови cron коллектора "
                  f"(`make errors-cron-remove`) или повтори с --force.")
            return 1

    trash = Path(args.trash_dir) if args.trash_dir else PROJECT_DIR / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (trash / f"errors-migrate-027-aggregates-{ts}.json").write_text(
        json.dumps(aggs, ensure_ascii=False, indent=1), encoding="utf-8")
    (trash / f"errors-migrate-027-alert-{ts}.json").write_text(
        json.dumps(alert, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {"ts": ts, "stats": {k: v for k, v in st.items() if k != "mapping"},
                "backups": [f"errors-migrate-027-aggregates-{ts}.json",
                            f"errors-migrate-027-alert-{ts}.json"]}
    (trash / f"errors-migrate-027-manifest-{ts}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    atomic_write_json(agg_path, new_aggs)
    atomic_write_json(alert_path, new_alert)
    print(f"MIGRATED: aggregates {st['agg_before']}→{st['agg_after']}, "
          f"alert {st['alert_before']}→{st['alert_after']}; "
          f"бэкапы + манифест: .trash/errors-migrate-027-*-{ts}.*")
    # инвариант (P1-1): alert ⊆ aggs ∪ orphans
    viol = [k for k in new_alert if isinstance(new_alert[k], dict)
            and k not in new_aggs and k not in set(st["orphans"])]
    if viol:
        print(f"ВНИМАНИЕ: нарушен инвариант alert ⊆ aggs ∪ orphans ({len(viol)} ключей)")
        return 1
    print("инвариант alert ⊆ aggregates ∪ orphans — соблюдён ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
