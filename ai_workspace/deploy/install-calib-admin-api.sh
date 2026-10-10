#!/usr/bin/env bash
# calib-admin-api installer — host-side admin-API калибровки AI-верстака.
# Trace: arch-2026-10-09-calib-admin-ui (Ф1). Канон: docs/operations/calib-admin-api-runbook.md
#
# ЗАПУСК (нужен root):
#   sudo ai_workspace/deploy/install-calib-admin-api.sh --sync-env
#   sudo ai_workspace/deploy/install-calib-admin-api.sh --rotate-key --sync-env
#   sudo ai_workspace/deploy/install-calib-admin-api.sh --gpu-k 2 --sync-env
#   sudo ai_workspace/deploy/install-calib-admin-api.sh --uninstall
#
# ЧТО ДЕЛАЕТ (идемпотентно):
#   1) ключ: переиспользует из /etc/calib-admin-api.env, иначе генерирует (openssl rand -hex 32)
#   2) /etc/calib-admin-api.env (root:root 0600) — CALIB_API_KEY + CALIB_API_PORT=8700
#      + WS_GPU_K=<K> (Ф-B, arch-2026-10-10-ai-ws-p2-1 R3): calib-admin-api —
#      ЕДИНСТВЕННЫЙ рантайм-читатель WS_GPU_K (gpu.py env_gpu_k); K = --gpu-k N,
#      иначе авто-детект scripts/gpu_k_detect.py (fail-мусор/нет nvidia-smi → 1, I13)
#   3) [--sync-env] тот же ключ → repo .env (owner каталога, 0600); kb-console подхватит
#   4) unit → /etc/systemd/system/ + daemon-reload + enable --now
#   5) статус + probe 401 (сервис жив, auth включён)
# Ключ НИКОГДА не печатается. Инварианты: bind 127.0.0.1, workers=1.
set -euo pipefail

SVC=calib-admin-api
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SELF_DIR/calib-admin-api.service"
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"
ENV_FILE=/etc/calib-admin-api.env
UNIT_DST="/etc/systemd/system/$SVC.service"
PORT_DEFAULT=8700

ROTATE=0; SYNC_ENV=0; UNINSTALL=0; GPU_K=""
while [ $# -gt 0 ]; do case "$1" in
  --rotate-key) ROTATE=1 ;;
  --sync-env)   SYNC_ENV=1 ;;
  --gpu-k)      [ $# -ge 2 ] || { echo "[install] --gpu-k требует значение (int >= 1)" >&2; exit 2; }
                GPU_K="$2"; shift ;;
  --uninstall)  UNINSTALL=1 ;;
  -h|--help)    sed -n '2,19p' "$0"; exit 0 ;;
  *) echo "[install] неизвестный аргумент: $1" >&2; exit 2 ;;
esac; shift; done

log(){ printf '[install] %s\n' "$*"; }
die(){ printf '[install] ОШИБКА: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "нужен root: sudo $0 $*"

if [ "$UNINSTALL" = 1 ]; then
  log "uninstall: stop/disable + удаление unit"
  systemctl disable --now "$SVC" 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl daemon-reload
  log "готово. $ENV_FILE и ключ в .env НЕ трогаю — удалите вручную при необходимости"
  exit 0
fi

[ -f "$UNIT_SRC" ] || die "нет unit: $UNIT_SRC"
[ -x "$REPO_ROOT/.venv/bin/python" ] || die "нет .venv: $REPO_ROOT/.venv/bin/python"

KEY=""
if [ "$ROTATE" = 0 ] && [ -f "$ENV_FILE" ]; then
  KEY="$(grep -m1 '^CALIB_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
fi
if [ -z "$KEY" ]; then
  KEY="$(openssl rand -hex 32)"
  log "ключ сгенерирован"
else
  log "ключ переиспользован из $ENV_FILE (--rotate-key чтобы сменить)"
fi

# K (Ф-B R3): --gpu-k > авто-детект gpu_k_detect.py > 1 (fail-safe, I13)
if [ -z "$GPU_K" ]; then
  GPU_K="$(python3 "$REPO_ROOT/scripts/gpu_k_detect.py" --k-only 2>/dev/null || true)"
fi
case "$GPU_K" in
  ''|*[!0-9]*) GPU_K=1 ;;
  *) [ "$GPU_K" -ge 1 ] 2>/dev/null || GPU_K=1 ;;
esac
log "WS_GPU_K=$GPU_K (--gpu-k чтобы переопределить; авто-детект gpu_k_detect.py)"

umask 077
printf 'CALIB_API_KEY=%s\nCALIB_API_PORT=%s\nWS_GPU_K=%s\n' "$KEY" "${CALIB_API_PORT:-$PORT_DEFAULT}" "$GPU_K" > "$ENV_FILE"
chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"
log "$ENV_FILE записан (root:root 0600)"
umask 022

if [ "$SYNC_ENV" = 1 ]; then
  DOTENV="$REPO_ROOT/.env"
  OWNER="$(stat -c '%U:%G' "$REPO_ROOT")"
  touch "$DOTENV"
  TMP="$(mktemp)"
  grep -v '^CALIB_API_KEY=' "$DOTENV" > "$TMP" 2>/dev/null || true
  printf 'CALIB_API_KEY=%s\n' "$KEY" >> "$TMP"
  cat "$TMP" > "$DOTENV"; rm -f "$TMP"
  chown "$OWNER" "$DOTENV"; chmod 600 "$DOTENV"
  log "ключ синхронизирован в $DOTENV (owner $OWNER, 0600)"
else
  log "NB: .env не тронут. Добавьте ключ в .env вручную либо запустите с --sync-env"
fi

cp "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now "$SVC"
log "сервис установлен и запущен ($UNIT_DST)"

sleep 1
log "is-active: $(systemctl is-active "$SVC" || true)"
CODE="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CALIB_API_PORT:-$PORT_DEFAULT}/calib/model" 2>/dev/null || echo 000)"
log "probe /calib/model без ключа → HTTP $CODE (ожидается 401 = сервис жив, auth включён)"
log "логи: journalctl -u $SVC -n 20 --no-pager"
if [ "$SYNC_ENV" = 1 ]; then
  log "далее: docker compose up -d --force-recreate kb-console   # чтобы консоль взяла ключ"
fi
