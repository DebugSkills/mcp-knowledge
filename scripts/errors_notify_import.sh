#!/usr/bin/env bash
# errors_notify_import.sh — notify.json 0600 из /etc/backup-status.env
# (code-2026-09-24-016, В2-блок в; sudo-helper, дельта 2 спеки §7.2в).
#
# ⚠️ Д1 (живая свивка): make errors-notify-import запускать БЕЗ внешнего sudo
# (sudo уже внутри рецепта Makefile). Двойной sudo (sudo make …) даёт
# SUDO_USER=root → root-владельца notify.json → cron-юзер не читает → TG
# молча skip. Прямой запуск: sudo bash scripts/errors_notify_import.sh
#   [--env /etc/backup-status.env] [--out …] [--host <тег>]
#   (sudo обязателен: env-файл root-only 0600; аналог check-backup.sh).
#
# Что делает: (1) парсит env-файл (grep, БЕЗ source): TG_TOKEN, TG_CHAT,
#   HTTPS_PROXY → proxy (fallback HTTP_PROXY), NO_PROXY (опц.);
#   (2) валидация: нет файла/переменной/proxy → ИМЯ в stderr (без значений,
#   R5) + exit≠0; политика хоста = прокси обязателен (§7.8);
#   (3) пишет notify.json АТОМАРНО (tmp+mv), 0600, владелец = resolve_owner
#   (SUDO_USER≠root → владелец каталога назначения → id -un+warning);
#   (4) fingerprint: chat=sha256(chat_id)[:12], proxy=set, host, path (R5);
#   (5) идемпотентен: повтор = тот же файл.
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
PRINT_OWNER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env)   ENV_FILE="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    --host)  HOST_ARG="$2"; shift 2 ;;
    --print-owner) PRINT_OWNER=1; shift ;;  # (скрытый, тесты Д1): только разрешить владельца
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

# ── Д1: разрешение владельца (устойчиво к двойному sudo) ──
# Порядок: SUDO_USER (непуст И не root) → владелец каталога назначения
# (stat -c %U dirname OUT — реальный cron-юзер владеет каталогом данных) →
# id -un + WARNING (последний резерв).
resolve_owner() {  # stdout: имя владельца; stderr: NOTE/WARNING при fallback
  local su="${SUDO_USER:-}"
  if [ -n "$su" ] && [ "$su" != "root" ]; then
    printf '%s\n' "$su"
    return 0
  fi
  local dir_owner
  dir_owner="$(stat -c %U "$(dirname "$OUT")" 2>/dev/null || true)"
  if [ -n "$dir_owner" ] && [ "$dir_owner" != "root" ]; then
    echo "NOTE: SUDO_USER='${su:-пуст}' (двойной sudo или чистый root) → владелец из каталога назначения: ${dir_owner}" >&2
    printf '%s\n' "$dir_owner"
    return 0
  fi
  echo "WARNING: SUDO_USER='${su:-пуст}', владелец каталога '${dir_owner:-?}' не определены — резерв: $(id -un); запускайте make errors-notify-import БЕЗ внешнего sudo" >&2
  id -un
}

if [ -n "$PRINT_OWNER" ]; then  # тест-хук Д1: без чтения env и без записи
  resolve_owner
  exit 0
fi

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

# ── владелец (Д1): SUDO_USER (не root) → каталог назначения → id -un ──
OWNER="$(resolve_owner)"
if ! chown "${OWNER}:" "$TMP" 2>/dev/null && ! chown "$OWNER" "$TMP"; then
  echo "WARNING: chown '$OWNER' не удался — владелец $(id -un); cron-пользователь должен совпадать (make БЕЗ внешнего sudo)" >&2
fi

mv -f "$TMP" "$OUT"
trap - EXIT

# ── 4) fingerprint (без секретов) ──
FP="$(printf '%s' "$TG_CHAT" | sha256sum | cut -c1-12)"
echo "notify.json: chat=${FP} proxy=set host=${HOST} path=${OUT}"
