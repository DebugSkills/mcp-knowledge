#!/usr/bin/env python3
"""errors_cleanup_cron_legacy.py — one-shot миграция legacy cron-ключей (018).

До code-2026-09-25-018 события cron exit=0 писались ПОЛНОЙ строкой
`[CRON] job=<j> exit=0 dur=<s> ts=<iso>`; deep_normalize не маскирует цифры
в `dur=0s`/`-25T01:` → каждая (джоба × час × dur) = отдельная P2-сигнатура
(2026-09-25: 62 ключа, все P2/active — N динамическое). Фикс 018 канонизировал
heartbeat-сообщение (`[CRON] job=<j> exit=0`, expected=True → P3/T), но
legacy-ключи сами не уходят: update_aggregates пересматривает ТОЛЬКО
сигнатуры с событиями цикла (errors_collect.py, цикл по by_sig), а
errors_prune удаляет только resolved ⇒ ОБЯЗАТЕЛЬНА one-shot миграция
(.boardData.md §7.8, Critic iter1 P1-1).

Критерий отбора (динамический; он же — проверка AC1 post-deploy):
  sig.startswith("cron_log|CRON|") AND " dur=" in sig
  AND priority=="P2" AND status=="active"
P0/P1-cron и все не-cron ключи НЕ трогаем никогда (даже с --confirm).
Что делаем: DELETE из aggregates/signatures.json (НЕ status-flip: resolved
при свежем last_seen лгал бы против E4-семантики «тишина ≥7d»; история —
в backup). Канонический heartbeat `… exit=<n>` dur= не содержит ⇒ не
выбирается; новые exit=0-события сходимся в ≤1 ключ на джобу.

Безопасность:
  * dry-run по умолчанию (план без записи); применение — --confirm;
  * backup ДО правки: байт-копия signatures.json →
    <репо-корень>/.trash/signatures-cron-cleanup-<ts>.json (репо-корень
    определяется по расположению скрипта, .gitignore:.trash; на прод-хосте
    это <clone>/.trash; база переопределяется --trash-dir);
  * валидация JSON до и после; ассерты: удалено ровно N выбранных,
    keys_before − deleted == keys_after, нетронутые ключи dict-идентичны;
  * идемпотентность: повторный запуск выбирает 0 (active-фильтр);
    пустой план → backup НЕ пишется.

Гонка lost-update (P2-N1): коллектор пишет signatures.json БЕЗ lock
(atomic_write_json даёт атомарность файла, но не read-modify-write).
Рекомендация (verify-then-repeat): --confirm запускать сразу после
завершения цикла коллектора (см. /var/log/mcp-errors-collect.log), затем
read-only срез AC1 (тот же критерий отбора); при N>0 — повторить --confirm
(идемпотентно, самоизлечивается).

Опционально --clean-alert-state (только с --confirm): вычистить orphan
CRON-записи в alert_state.json — ключи удалённых сигнатур (P3-N2; безвредны
и без чистки: classify_weekly/alerts итерируют только aggs) — с бэкапом.

Выход: 0 — dry-run/успех/нечего делать; 1 — невалидный JSON/ошибка.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import DATA_ROOT, PROJECT_DIR, atomic_write_json

CRON_PREFIX = "cron_log|CRON|"


def select_legacy(aggs: dict) -> list:
    """§7.8-1: legacy-форма (dur= в сигнатуре) + P2 + active. P0/P1 — никогда."""
    return [sig for sig, a in aggs.items()
            if sig.startswith(CRON_PREFIX) and " dur=" in sig
            and a.get("priority") == "P2" and a.get("status") == "active"]


def _priority_counts(aggs: dict) -> dict:
    """Прозрачность dry-run: legacy cron-ключи по приоритетам (P1 виден, НЕ удаляется)."""
    counts: dict = {}
    for sig, a in aggs.items():
        if sig.startswith(CRON_PREFIX) and " dur=" in sig:
            counts[a.get("priority", "?")] = counts.get(a.get("priority", "?"), 0) + 1
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Error→Rule: миграция legacy cron-ключей exit=0 (018; dry-run default)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    ap.add_argument("--confirm", action="store_true",
                    help="реальное удаление выбранных legacy-ключей (P2/active)")
    ap.add_argument("--trash-dir", default=None,
                    help="база backup-каталога (дефолт <репо-корень>/.trash)")
    ap.add_argument("--clean-alert-state", action="store_true",
                    help="с --confirm: вычистить orphan CRON-записи alert_state.json (P3-N2)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    agg_path = sink / "aggregates" / "signatures.json"
    try:
        aggs = json.loads(agg_path.read_text(encoding="utf-8"))  # валидация ДО
    except FileNotFoundError:
        print(f"cron-cleanup: signatures.json отсутствует ({agg_path}) — нечего чистить.")
        return 0
    except json.JSONDecodeError as exc:
        print(f"cron-cleanup: НЕВАЛИДНЫЙ JSON до правки ({agg_path}): {exc} — отказ.")
        return 1

    plan = select_legacy(aggs)
    legacy_total = sum(_priority_counts(aggs).values())
    print(f"=== errors cron-cleanup · sink={sink} ===")
    print(f"legacy cron-ключи (cron_log|CRON|… dur=): {legacy_total} "
          f"по приоритетам {_priority_counts(aggs)}")
    print(f"план удаления (P2+active): {len(plan)}")
    for s in plan[:5]:
        print(f"  ✂ {s[:110]}")
    if len(plan) > 5:
        print(f"  … и ещё {len(plan) - 5}")

    if not args.confirm:
        print("DRY-RUN: ничего не удалено (запуск с --confirm для применения; "
              "гонка P2-N1: confirm сразу после цикла коллектора, затем срез AC1, "
              "при N>0 повторить).")
        return 0
    if not plan:
        print("cron-cleanup: нечего удалять (идемпотентность: повтор — 0).")
        return 0

    trash = Path(args.trash_dir) if args.trash_dir else PROJECT_DIR / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = trash / f"signatures-cron-cleanup-{ts}.json"
    raw = agg_path.read_bytes()
    backup.write_bytes(raw)  # байт-копия ДО правки (обратимость, P3-N1)

    before = json.loads(raw.decode("utf-8"))
    keys_before = set(before)
    for s in plan:
        aggs.pop(s, None)
    atomic_write_json(agg_path, aggs)
    after = json.loads(agg_path.read_text(encoding="utf-8"))  # валидация ПОСЛЕ
    assert set(after) == keys_before - set(plan), "keys_before − deleted == keys_after"
    assert len(keys_before) - len(plan) == len(after)
    for s in set(after) & keys_before:
        assert after[s] == before[s], f"нетронутый ключ изменён: {s[:80]}"
    print(f"backup: {backup}")
    print(f"CLEANED: {len(plan)} legacy cron-ключей (P2/active); "
          f"нетронуто {len(after)}; JSON валиден.")

    if args.clean_alert_state:
        alert_path = sink / "alert_state.json"
        try:
            alert_raw = alert_path.read_bytes()
            alert = json.loads(alert_raw)
        except FileNotFoundError:
            alert_raw, alert = None, {}
        orphans = [s for s in plan if s in alert]
        if alert_raw is not None and orphans:
            abackup = trash / f"alert-state-cron-cleanup-{ts}.json"
            abackup.write_bytes(alert_raw)
            for s in orphans:
                alert.pop(s, None)
            atomic_write_json(alert_path, alert)
            print(f"alert_state: вычищено {len(orphans)} orphan CRON-записей "
                  f"(backup {abackup.name}).")
        else:
            print("alert_state: orphan CRON-записей нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
