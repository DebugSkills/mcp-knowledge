#!/usr/bin/env bash
# errors_notify_import.sh — notify.json 0600 из /etc/backup-status.env
# (code-2026-09-24-016, В2-блок в; sudo-helper, дельта 2 спеки §7.2в).
#
# Запуск (sudo обязателен: env-файл root-only 0600; sudo -n недоступен на
# хосте — это ОДНА ручная команда оператора, аналог check-backup.sh):
#   sudo bash scripts/errors_notify_import.sh [--env /etc/backup-status.env]
#        [--out "$DATA_ROOT/logs/errors/notify.json"] [--host <тег>]
#
# Что делает:
#   1) парсит env-файл (grep, БЕЗ source/исполнения): TG_TOKEN, TG_CHAT,
#      HTTPS_PROXY → proxy (fallback HTTP_PROXY — паттерн check-backup.sh:29),
#      NO_PROXY (опц.);
#   2) валидация: нет файла/переменной/proxy → ИМЯ в stderr (без значений!)
#      + exit≠0; политика хоста = прокси обязателен (§7.8 — не молчать);
#   3) пишет notify.json {"bot_token","chat_id","proxy","no_proxy"(опц.),"host"}
#      АТОМАРНО (tmp+mv), mode 0600, владелец/группа = $SUDO_USER (chown) —
#      иначе пользовательский cron не прочитает; SUDO_USER пуст (чистый root/
#      su -) → владелец НЕ меняется + warning в stderr (файл валиден, но cron
#      ladmin его не увидит — запускайте sudo от целевого юзера);
#   4) печатает ТОЛЬКО fingerprint: chat=sha256(chat_id)[:12], proxy=set|unset,
#      host, path — токен/прокси-креды НИКОГДА не в stdout/stderr/логах (R5);
#   5) идемпотентен: повтор = тот же файл (детерминированные данные).
#
# Прод-интеграция: НЕ рендерится ansible (переживает деплои, как suppression
# 008); формат совместим с errors-notify.json.j2. set -u: unbound = баг скрипта.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_ROOT="${DATA_ROOT:-$BASE/data}"

ENV_FILE="/etc/backup-status.env"
OUT="$DATA_ROOT/logs/errors/notify.json"
HOST_ARG=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env)   ENV_FILE="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    --host)  HOST_ARG="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

fail_var() {  # $1 = имя переменной — ТОЛЬКО имя, без значения (R5)
  echo "errors_notify_import: missing variable: $1 (in $ENV_FILE)" >&2
  exit 1
}

# ── 1) парсинг env-файла (grep: KEY=VALUE, кавычки по краям срезаем) ──
if [ ! -f "$ENV_FILE" ]; then
  echo "errors_notify_import: env file not found: $ENV_FILE" >&2
  exit 1
fi

get_var() {  # $1 = KEY → value (последнее вхождение) или пусто
  grep -E "^[[:space:]]*${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//" || true  # опц. ключ: grep-miss ≠ ошибка
}

TG_TOKEN="$(get_var TG_TOKEN)"
TG_CHAT="$(get_var TG_CHAT)"
HTTPS_PROXY_V="$(get_var HTTPS_PROXY)"
HTTP_PROXY_V="$(get_var HTTP_PROXY)"
NO_PROXY_V="$(get_var NO_PROXY)"

# ── 2) валидация (имена в stderr, без значений) ──
[ -n "$TG_TOKEN" ] || fail_var TG_TOKEN
[ -n "$TG_CHAT" ] || fail_var TG_CHAT
PROXY_V="${HTTPS_PROXY_V:-$HTTP_PROXY_V}"
if [ -z "$PROXY_V" ]; then
  echo "errors_notify_import: missing variable: HTTPS_PROXY (и fallback HTTP_PROXY) — политика хоста: прокси обязателен (in $ENV_FILE)" >&2
  exit 1
fi

HOST="${HOST_ARG:-$(hostname -s)}"

# ── 3) атомарная запись 0600 (json-escape: бэкслеш и кавычка) ──
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp "$(dirname "$OUT")/.notify.json.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

{
  printf '{\n'
  printf '  "bot_token": "%s",\n' "$(json_escape "$TG_TOKEN")"
  printf '  "chat_id": "%s",\n' "$(json_escape "$TG_CHAT")"
  printf '  "proxy": "%s",\n' "$(json_escape "$PROXY_V")"
  if [ -n "$NO_PROXY_V" ]; then
    printf '  "no_proxy": "%s",\n' "$(json_escape "$NO_PROXY_V")"
  fi
  printf '  "host": "%s"\n' "$(json_escape "$HOST")"
  printf '}\n'
} > "$TMP"
chmod 600 "$TMP"

# владелец = $SUDO_USER (cron-пользователь должен читать файл); пуст → не менять
if [ -n "${SUDO_USER:-}" ]; then
  chown "${SUDO_USER}:" "$TMP" 2>/dev/null || chown "$SUDO_USER" "$TMP"
else
  echo "WARNING: SUDO_USER пуст — владелец $(id -un); cron-пользователь должен совпадать (запустите sudo от целевого юзера)" >&2
fi

mv -f "$TMP" "$OUT"
trap - EXIT

# ── 4) fingerprint (без секретов) ──
FP="$(printf '%s' "$TG_CHAT" | sha256sum | cut -c1-12)"
echo "notify.json: chat=${FP} proxy=set host=${HOST} path=${OUT}"
