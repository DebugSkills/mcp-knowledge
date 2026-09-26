#!/usr/bin/env python3
"""errors_prune.py — ретенция sink цикла Error→Rule (Ф4, code-2026-09-22-003).

Безопасность (двойной гейт):
  * dry-run по умолчанию: печатает план («что исчезнет»), НИЧЕГО не удаляет;
  * реальное удаление ТОЛЬКО при --confirm И config.prune.enabled=true
    (kill-switch: config.json рендерится ansible errors.yml setup; на dev
    дефолт false — см. errors_collect.DEFAULT_CONFIG).

Что чистит (retention_days=90 из config):
  * events/raw/YYYY-MM-DD.jsonl старше cutoff — целиком (по дню);
  * сигнатуры в aggregates/signatures.json: resolved и last_seen старше
    retention → удаляются; hold-защита: investigating=true (alert_state)
    или last_seen свежее hold_days (14) → НЕ трогаем.
  * **027-D3: истечение «мёртвых» сигнатур** (alert_state.json — единственный
    владелец статусов): status ∈ {new, known, resolved} и last_seen старше
    stale_sig_days (дефолт 45) → удаляются из alert_state И aggregates;
    hold-защита: investigating=true, status=regressed — НИКОГДА не истекают.
    Живой dry-run на молодом sink (17 суток) по определению даёт 0 — проверка
    синтетикой (T27-7), не «успех по тишине».

Перед первым реальным удалением — tar-срез events/prune-backup-<ts>.tar.gz
удаляемых файлов (обратимость; храним последние 4 среза, старые — в план).

Выход: 0 dry-run/успех; 1 — --confirm при kill-switch off или ошибка.
"""

import argparse
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (
    DATA_ROOT,
    atomic_write_json,
    load_config,
    load_json,
    parse_ts,
)

PRUNE_BACKUPS_KEEP = 4
# 027-D3: срок жизни «мёртвой» сигнатуры (никогда не повторялась) в alert_state.
# 45 суток = 4 недели + запас: E4-тишина 7d, daily-keep 21d, 90d — про объём raw,
# не про state. Переопределяется config.stale_sig_days (не хардкод в логике).
STALE_SIG_DAYS_DEFAULT = 45


def _fmt_mb(n: int) -> str:
    return f"{n / 1e6:.2f} МБ"


def plan(sink: Path, cfg: dict, alert: dict):
    """→ (day_files, sigs, stale_sigs, old_backups, retention).

    sigs — resolved-сигнатуры из aggregates (E4-ретенция, как было);
    stale_sigs — 027-D3: «мёртвые» сигнатуры из alert_state (единственный
    владелец статусов) старше stale_sig_days; удаляются из ОБОИХ файлов.
    """
    now = datetime.now(timezone.utc)
    retention = int(cfg.get("retention_days", 90))
    hold_days = int(cfg.get("hold_days", 14))
    stale_days = int(cfg.get("stale_sig_days", STALE_SIG_DAYS_DEFAULT))
    cutoff = now - timedelta(days=retention)
    hold_cutoff = now - timedelta(days=hold_days)
    stale_cutoff = now - timedelta(days=stale_days)

    raw_dir = sink / "events" / "raw"
    day_files = []
    if raw_dir.exists():
        for p in sorted(raw_dir.glob("*.jsonl")):
            try:  # имя файла = голый день YYYY-MM-DD (parse_ts его не ест)
                day = datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # не-дата в имени — не трогаем
            if day < cutoff:
                day_files.append(p)

    aggs = load_json(sink / "aggregates" / "signatures.json", {})
    sigs = []
    for sig, a in aggs.items():
        try:
            last = parse_ts(a.get("last_seen", ""))
        except ValueError:
            continue  # битая дата — hold, не трогаем
        if last >= cutoff:
            continue
        if a.get("status") != "resolved":
            continue
        st = alert.get(sig) or {}
        if st.get("investigating") or last >= hold_cutoff:
            continue  # hold: ручной разбор / свежий last_seen
        sigs.append(sig)

    # 027-D3: «мёртвые» сигнатуры (никогда не повторяются) — по alert_state.
    # Скоуп строго {new, known, resolved}: regressed (рецидив) и investigating
    # (ручной разбор) не истекают НИКОГДА — это незакрытые дела, не «мусор».
    stale_sigs = []
    for sig, st in (alert or {}).items():
        if not isinstance(st, dict):
            continue  # служебные ключи (_alerts_meta и пр.)
        if st.get("status") not in ("new", "known", "resolved"):
            continue
        if st.get("investigating"):
            continue
        try:
            last = parse_ts(st.get("last_seen", ""))
        except ValueError:
            continue  # битая дата — hold
        if last < stale_cutoff:
            stale_sigs.append(sig)

    ev_dir = sink / "events"
    old_backups = sorted(ev_dir.glob("prune-backup-*.tar.gz"))[:-PRUNE_BACKUPS_KEEP] \
        if ev_dir.exists() else []
    return day_files, sigs, stale_sigs, old_backups, retention


def make_backup_slice(sink: Path, day_files, sigs, stale_sigs=None) -> Path | None:
    """tar-срез удаляемого (дни + снапшот aggregates + снапшот alert_state)."""
    stale_sigs = stale_sigs or []
    if not day_files and not sigs and not stale_sigs:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (sink / "events").mkdir(parents=True, exist_ok=True)  # 027-D3: бэкап может быть
    # D3-only (0 дней/0 sigs, но есть истёкшие) — каталога может не быть
    path = sink / "events" / f"prune-backup-{ts}.tar.gz"
    tmp = path.with_name(path.name + ".tmp")
    import io
    with tarfile.open(tmp, "w:gz") as tar:
        for f in day_files:
            tar.add(f, arcname=f"raw/{f.name}")
        if sigs:
            aggs = load_json(sink / "aggregates" / "signatures.json", {})
            snap = {s: aggs[s] for s in sigs if s in aggs}
            data = json.dumps(snap, ensure_ascii=False, indent=1).encode()
            info = tarfile.TarInfo("aggregates-pruned-signatures.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if stale_sigs:
            # 027-D3: бэкап alert-половины (обратимость: вернуть file-into-place)
            alert = load_json(sink / "alert_state.json", {})
            snap = {s: alert[s] for s in stale_sigs if s in alert}
            data = json.dumps(snap, ensure_ascii=False, indent=1).encode()
            info = tarfile.TarInfo("alert-state-pruned-signatures.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    os.replace(tmp, path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Error→Rule: prune sink (dry-run default)")
    ap.add_argument("--sink", default=None)
    ap.add_argument("--confirm", action="store_true",
                    help="реальное удаление (требует config.prune.enabled=true)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    if not sink.exists():
        print(f"prune: sink отсутствует ({sink}) — нечего чистить.")
        return 0
    cfg = load_config(sink)
    alert = load_json(sink / "alert_state.json", {})

    day_files, sigs, stale_sigs, old_backups, retention = plan(sink, cfg, alert)
    lines = 0
    for f in day_files:
        with open(f, encoding="utf-8") as fh:
            lines += sum(1 for _ in fh)
    size = sum(f.stat().st_size for f in day_files)

    stale_days = int(cfg.get("stale_sig_days", STALE_SIG_DAYS_DEFAULT))
    print(f"=== errors prune · sink={sink} · retention={retention}d "
          f"· stale-sig={stale_days}d ===")
    print(f"план: {len(day_files)} raw-дней ({lines} строк, {_fmt_mb(size)}), "
          f"{len(sigs)} resolved-сигнатур, {len(stale_sigs)} истёкших сигнатур "
          f"(alert_state), {len(old_backups)} старых prune-бэкапов")
    for f in day_files[:5]:
        print(f"  ✂ день: {f.name}")
    for s in sigs[:5]:
        print(f"  ✂ сигнатура: {s[:110]}")
    for s in stale_sigs[:5]:
        print(f"  ✂ истёкшая (D3): {s[:110]}")
    if len(day_files) + len(sigs) + len(stale_sigs) > 10:
        print(f"  … и ещё {len(day_files) + len(sigs) + len(stale_sigs) - 10}")

    if not args.confirm:
        print("DRY-RUN: ничего не удалено (запуск с --confirm для применения; "
              "план = «что исчезнет»).")
        return 0

    if not cfg.get("prune", {}).get("enabled"):
        print("KILL-SWITCH: config.prune.enabled=false — удаление ЗАПРЕЩЕНО "
              "(включить в ansible: vault/group_vars → errors.yml setup).")
        return 1

    if not day_files and not sigs and not stale_sigs and not old_backups:
        print("prune: нечего удалять.")
        return 0

    backup_slice = make_backup_slice(sink, day_files, sigs, stale_sigs)
    if backup_slice:
        print(f"backup-срез перед удалением: {backup_slice.name}")

    for f in day_files:
        f.unlink()
    for f in old_backups:
        f.unlink()
    if sigs or stale_sigs:
        aggs_path = sink / "aggregates" / "signatures.json"
        aggs = load_json(aggs_path, {})
        for s in sigs:
            aggs.pop(s, None)
        for s in stale_sigs:  # 027-D3: чистка обеих половин
            aggs.pop(s, None)
        atomic_write_json(aggs_path, aggs)
    if stale_sigs:
        alert_path = sink / "alert_state.json"
        alert2 = load_json(alert_path, {})
        for s in stale_sigs:
            alert2.pop(s, None)
        atomic_write_json(alert_path, alert2)
    print(f"PRUNED: {len(day_files)} дней ({lines} строк, {_fmt_mb(size)}), "
          f"{len(sigs)} сигнатур, {len(stale_sigs)} истёкших (D3), "
          f"{len(old_backups)} старых бэкап-срезов.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
