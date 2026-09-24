#!/usr/bin/env bash
# errors_cron.sh — установка/снятие cron-джоб Error→Rule TG-уведомлений
# (code-2026-09-24-016, блок 5/6; sudo-инсталлятор, дельта 2 спеки §7.2в).
#
# Использование:
#   scripts/errors_cron.sh --install | --remove | --status
#       [--file PATH]     файл-модель crontab (preview/тесты; существующий непустой: сначала crontab -l > PATH; БЕЗ --file = реальный crontab)
#       [--data-root DIR] корень данных (по умолчанию $BASE/data; логи → DIR/logs/cron/)
#   scripts/errors_cron.sh --validate FILE   (скрытый) R8-валидатор: exit≠0 на относительных путях
#
# Что делает (--install):
#   1) бэкап текущего crontab в .trash/crontab-backup-<ts>.txt ДО правки (R6);
#   2) вынимает старый блок/сироты-строки (идемпотентность: 0 дублей);
#   3) добавляет маркер-блок из 3 джоб (collector */5 ПЕРВЫМ — P3-c, alerts */5,
#      weekly Пн 10:02) — все пути абсолютные + cd BASE, обёртки cron_wrap.sh
#      пишут [CRON]-строки (exit≠0 → P0-признак cron_nonzero, AC-collect-1);
#   4) R8-валидация блока ДО записи: cd-префикс абсолютный, путь-токены
#      абсолютные → иначе exit≠0 и crontab НЕ тронут;
#   5) config-оверлей: cron_logs += 3 наших лог-файла в
#      $DATA_ROOT/logs/errors/config.json — ТОЛЬКО в реальном режиме (в
#      --file preview пропущен; python3-merge union, чужие пути R9).
# --remove: вынимает ТОЛЬКО маркер-блок + строки с нашими 3 скриптами
#      (вне блока), config-оверлей НЕ трогает (логи могут ещё собираться).
#
# set -uo pipefail БЕЗ -e: валидационные exit — управляемые.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_ROOT="${DATA_ROOT:-$BASE/data}"
TRASH="$BASE/.trash"
MARK_BEGIN="# mcp-knowledge errors-notify (code-2026-09-24-016)"
MARK_END="# mcp-knowledge errors-notify (end)"
TS="$(date +%Y%m%d%H%M%S)"

# ── R8-валидатор: команды cron должны быть абсолютными (cd / + пути с /) ──
validate_file() {  # $1 = файл со строками cron; проверяем ТОЛЬКО наши джобы (R8)
  local bad=0 line cmd tok
  while IFS= read -r line; do
    case "$line" in ""|\#*) continue ;; esac
    case "$line" in *errors_collect*|*errors_alert*|*errors_report*|*cron_wrap*) ;; *) continue ;; esac
    cmd="$(printf '%s\n' "$line" | awk '{ $1=$2=$3=$4=$5=""; sub(/^ +/,""); print }')"
    if ! printf '%s\n' "$cmd" | grep -qE '^cd /'; then
      echo "R8 FAIL: команда без абсолютного cd: $line" >&2; bad=1; continue
    fi
    for tok in $(printf '%s\n' "$cmd" | tr ';|' '  ' | tr '\\' ' '); do
      case "$tok" in
        ../*|./*|..) echo "R8 FAIL: относительный путь: $tok" >&2; bad=1 ;;
        *scripts/errors_*|*scripts/cron_wrap.sh|*logs/cron/*|*/.venv/bin/python)
          case "$tok" in /*) : ;; *) echo "R8 FAIL: путь без ведущего /: $tok" >&2; bad=1 ;; esac ;;
      esac
    done
  done < "$1"
  [ "$bad" -eq 0 ] || { echo "R8: валидация НЕ пройдена" >&2; return 1; }
  echo "R8 OK: все команды с абсолютными путями"
  return 0
}

# ── наши 3 джобы (абсолютные пути; collector ПЕРВЫМ — P3-c) ──
cron_lines() {
  local CRON_DIR="$DATA_ROOT/logs/cron"
  cat <<EOF
$MARK_BEGIN
*/5 * * * * cd $BASE && bash $BASE/scripts/cron_wrap.sh collector $CRON_DIR/collector.log -- $BASE/.venv/bin/python $BASE/scripts/errors_collect.py >> $CRON_DIR/collector.log 2>&1
*/5 * * * * cd $BASE && bash $BASE/scripts/cron_wrap.sh alerts $CRON_DIR/alerts.log -- $BASE/.venv/bin/python $BASE/scripts/errors_alert.py >> $CRON_DIR/alerts.log 2>&1
2 10 * * 1 cd $BASE && bash $BASE/scripts/cron_wrap.sh weekly $CRON_DIR/weekly.log -- $BASE/.venv/bin/python $BASE/scripts/errors_report.py --send-tg >> $CRON_DIR/weekly.log 2>&1
$MARK_END
EOF
}

# ── вычистка нашего блока + строк-сирот (наши скрипты вне блока) ──
strip_ours() {  # stdin → stdout
  awk -v mb="$MARK_BEGIN" -v me="$MARK_END" '
    $0 == mb { inblock = 1 }
    inblock && $0 == me { inblock = 0; next }
    !inblock && !/errors_(collect|alert|report)\.py|errors_cron\.sh/ { print }
    inblock { next }
  '
}

# ── config-оверлей cron_logs (R9): python3-мердж union + бэкап ──
overlay_config() {
  python3 - "$DATA_ROOT" "$TRASH" <<'PYEOF'
import json, sys, shutil
from pathlib import Path

data_root, trash = Path(sys.argv[1]), Path(sys.argv[2])
cfgp = data_root / "logs" / "errors" / "config.json"
cfgp.parent.mkdir(parents=True, exist_ok=True)
cfg = {}
if cfgp.exists():
    try:
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        shutil.copy2(cfgp, trash / f"errors-config-backup-{sys.argv[3] if len(sys.argv) > 3 else 'x'}.json")
    except (ValueError, OSError):
        cfg = {}
cron_dir = data_root / "logs" / "cron"
ours = [str(cron_dir / f"{n}.log") for n in ("collector", "alerts", "weekly")]
cl = list(cfg.get("cron_logs", []))
for p in ours:
    if p not in cl:
        cl.append(p)
cfg["cron_logs"] = cl
cfgp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"[errors_cron] config overlay: cron_logs={len(cl)} (наших +3) → {cfgp}")
PYEOF
}

# ── чтение/запись crontab (модель-файл или реальный) ──
read_crontab() {
  if [ -n "$MODEL_FILE" ]; then
    [ -f "$MODEL_FILE" ] && cat "$MODEL_FILE" || true
  else
    crontab -l 2>/dev/null || true
  fi
}

write_crontab() {  # $1 = файл с новым контентом
  if [ -n "$MODEL_FILE" ]; then
    local tmp
    tmp="$(mktemp "$(dirname "$MODEL_FILE")/.crontab.XXXXXX")"
    cat "$1" > "$tmp" && mv -f "$tmp" "$MODEL_FILE"
  else
    crontab "$1"
  fi
}

# ── парсинг аргументов ──
ACTION=""
MODEL_FILE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install|--remove|--status) ACTION="${1#--}"; shift ;;
    --file) MODEL_FILE="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --validate)
      shift
      if [ "${1:-}" = "" ] || [ ! -f "${1:-}" ]; then
        echo "--validate требует существующий FILE" >&2; exit 2
      fi
      validate_file "$1"; exit $?
      ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

[ -n "$ACTION" ] || { echo "укажите --install | --remove | --status (см. --help)" >&2; exit 2; }

# Д2 (живая свивка 016): --file-модель ОБЯЗАНА существовать и быть непустой.
# Отсутствующий файл молча читался как ПУСТОЙ crontab → preview выглядел как
# «удаление всех пользовательских джоб». Отказ ДО любых правок/бэкапов.
if [ -n "$MODEL_FILE" ] && { [ ! -f "$MODEL_FILE" ] || [ ! -s "$MODEL_FILE" ]; }; then
  echo "[errors_cron] ОТКАЗ: файл-модель '$MODEL_FILE' отсутствует или пуст." >&2
  echo "  Модель должна содержать текущий crontab. Сначала снимите его:" >&2
  echo "    crontab -l > $MODEL_FILE" >&2
  exit 2
fi

mkdir -p "$TRASH" "$DATA_ROOT/logs/cron"

case "$ACTION" in
  install)
    CURRENT="$(read_crontab)"
    # бэкап ДО правки (R6); $() съедает trailing \n — восстанавливаем printf
    BACKUP="$TRASH/crontab-backup-$TS.txt"
    printf '%s\n' "$CURRENT" > "$BACKUP"
    # новый контент: чужое (без нашего блока/сирот) + наш блок
    NEW="$(printf '%s\n' "$CURRENT" | strip_ours)"
    BLOCK="$(cron_lines)"
    # P3-c: collector первым — порядок в BLOCK фиксирован выше
    if [ -n "${ERRORS_CRON_FORCE_REL:-}" ]; then  # тест-хук R8: относительный путь
      BLOCK="$(printf '%s\n' "$BLOCK" | sed "s|cd $BASE|cd rel-dir|; s|$BASE/|./|g")"
    fi
    OUT_TMP="$(mktemp "$TRASH/.newcrontab.XXXXXX")"
    { printf '%s' "$NEW"; [ -n "$NEW" ] && printf '\n'; printf '%s\n' "$BLOCK"; } > "$OUT_TMP"
    # R8: валидация ДО записи — кривой блок не должен попасть в crontab
    if ! validate_file "$OUT_TMP" >/dev/null 2>"$TRASH/.validate-$TS.err"; then
      cat "$TRASH/.validate-$TS.err" >&2
      rm -f "$OUT_TMP" "$TRASH/.validate-$TS.err" "$BACKUP"
      echo "[errors_cron] R8 FAIL: crontab НЕ изменён (валидация до записи)" >&2
      exit 1
    fi
    rm -f "$TRASH/.validate-$TS.err"
    if ! validate_file "$OUT_TMP" >/dev/null; then exit 1; fi  # (недостижимо; паранойя)
    write_crontab "$OUT_TMP"
    rm -f "$OUT_TMP"
    # Д3 (живая свивка 016): preview (--file) НЕ мутирует реальный config.json —
    # оверлей cron_logs только в реальном режиме.
    if [ -n "$MODEL_FILE" ]; then
      echo "[errors_cron] config overlay: пропущен (режим --file)"
    else
      overlay_config "$TS"
    fi
    echo "[errors_cron] install OK: 3 джобы (collector */5, alerts */5, weekly Пн 10:02); бэкап: $BACKUP"
    if [ -n "$MODEL_FILE" ]; then
      echo "[errors_cron] режим --file: реальный crontab НЕ тронут (модель: $MODEL_FILE)"
    fi
    ;;
  remove)
    CURRENT="$(read_crontab)"
    BACKUP="$TRASH/crontab-backup-$TS.txt"
    printf '%s\n' "$CURRENT" > "$BACKUP"
    OUT_TMP="$(mktemp "$TRASH/.newcrontab.XXXXXX")"
    printf '%s\n' "$CURRENT" | strip_ours | sed -e :a -e '/^\n*$/{$d;N;};/\n$/ba' > "$OUT_TMP"
    write_crontab "$OUT_TMP"
    rm -f "$OUT_TMP"
    echo "[errors_cron] remove OK: маркер-блок + строки errors_{collect,alert,report} вынуты; config-оверлей оставлен; бэкап: $BACKUP"
    ;;
  status)
    CURRENT="$(read_crontab)"
    CNT="$(printf '%s\n' "$CURRENT" | grep -cE 'errors_(collect|alert|report)\.py' || true)"
    if printf '%s\n' "$CURRENT" | grep -qF "$MARK_BEGIN"; then
      echo "errors-notify cron: установлен (маркер-блок найден, джоб: $CNT)"
      printf '%s\n' "$CURRENT" | grep -E 'errors_(collect|alert|report)\.py' | sed 's/^/  /'
    elif [ "${CNT:-0}" -gt 0 ]; then
      echo "errors-notify cron: ЧАСТИЧНО (строки без маркер-блока: $CNT) — запустите --install для миграции"
    else
      echo "errors-notify cron: not installed"
    fi
    CFG="$DATA_ROOT/logs/errors/config.json"
    if [ -f "$CFG" ]; then
      python3 -c "import json,sys; cl=json.load(open('$CFG')).get('cron_logs',[]); ours=[p for p in cl if 'logs/cron/' in p]; print(f'config overlay: {len(ours)}/3 наших логов в cron_logs')" 2>/dev/null || echo "config overlay: config.json не читается"
    else
      echo "config overlay: нет config.json"
    fi
    ;;
esac
