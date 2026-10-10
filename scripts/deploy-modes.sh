#!/usr/bin/env bash
# =============================================================================
# deploy-modes.sh — единый диспетчер air-gap деплоя (3 режима), сторона ИСТОЧНИКА (lup).
#
# Трасса: code-2026-10-10-deploy-modes (Ф1). Канон: plans/code-2026-10-10-deploy-modes-plan.md
#
# Режимы (MODE):
#   full-usb — ПОЛНЫЙ: код + модели, перенос флешкой (USB)
#   full-net — ПОЛНЫЙ: код + модели, перенос по сети (resumable, nested-jump lup->aikb)
#   code-net — ТОЛЬКО КОД по сети (быстро; фиксы/фичи, когда модели НЕ менялись)
#
# Шаги (STEP): pack (собрать пакет) · ship (передать) · all (pack+ship).
#   Узловая сторона (aikb) — отдельными целями: bundle-unpack (full-*) · airgap-update (code-net).
#
# Диспетчер НЕ переписывает движки 038 — валидирует MODE и маппит на существующие:
#   airgap-bundle-pack.sh · airgap-pack-subset.sh · airgap-bundle-ship.sh
#
# Контурный инвариант: между контурами едут ТОЛЬКО код и модели;
# корпус знаний и индексы Qdrant — НИКОГДА.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE=""; MODE_GIVEN=0; STEP="all"; OUT="$GIT_ROOT/artifacts"
USB=""; HOST=""; PIPE_VIA=""; RSYNC_PATH_ARG=""; DEST=""; MATRIX=0; DRY=0
EXTRA="${ARGS:-}"
ALLOW_MODEL_DRIFT="${AIRGAP_ALLOW_MODEL_DRIFT:-0}"

die()  { echo "ОШИБКА: $*" >&2; exit 1; }
info() { echo "[deploy-modes] $*" >&2; }

usage() { cat <<'EOF'
Usage: scripts/deploy-modes.sh MODE=<full-usb|full-net|code-net> [STEP=pack|ship|all]
       [--out DIR] [--usb MOUNT] [--host SSH_TARGET | --pipe-via JUMP_HOST]
       [--rsync-path CMD] [--dry-run] [--matrix] [--help]

Режимы:  full-usb (код+модели, USB) · full-net (код+модели, сеть) · code-net (ТОЛЬКО код, сеть)
Без MODE — печатает матрицу режимов.  --dry-run — печатает команды, не исполняя.
Переменные:  ARGS="…" доп. аргументы pack-движка · AIRGAP_ALLOW_MODEL_DRIFT=1 обход ОВ5-guard.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    MODE=*) MODE="${1#MODE=}"; if [ -n "$MODE" ]; then MODE_GIVEN=1; fi ;;
    STEP=*) STEP="${1#STEP=}" ;;
    --out) shift; OUT="${1:-}" ;;
    --usb) shift; USB="${1:-}" ;;
    --host) shift; HOST="${1:-}" ;;
    --pipe-via) shift; PIPE_VIA="${1:-}" ;;
    --rsync-path) shift; RSYNC_PATH_ARG="${1:-}" ;;
    --dest) shift; DEST="${1:-}" ;;
    --dry-run) DRY=1 ;;
    --matrix) MATRIX=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
  shift
done

print_matrix() { cat <<'EOF'

  Air-gap деплой — 3 режима (единый Makefile-UX: источник lup · узел aikb)

  MODE       Состав        pack + ship (lup)           узел aikb
  ---------  ------------  ---------------------------  ---------------------------
  full-usb   код + модели  bundle-pack + ship --usb    bundle-unpack -> deploy.yml
  full-net   код + модели  bundle-pack + ship --host   bundle-unpack -> deploy.yml
  code-net   ТОЛЬКО код    subset-pack + ship --files  airgap-update BUNDLE=...
  ---------  ------------  ---------------------------  ---------------------------

  Выбор режима: менялись ли модели · есть ли носитель · время · контурный инвариант.
  Инвариант: едут ТОЛЬКО код и модели; корпус знаний и индексы Qdrant — НИКОГДА.

  Примеры:
    make airgap MODE=full-usb STEP=all USB=/media/usb
    make airgap MODE=full-net STEP=all HOST=aikb          # или PIPE=aikb (pipe-fallback)
    make airgap MODE=code-net STEP=pack
    make airgap                                           # эта матрица
EOF
}

if [ "$MATRIX" -eq 1 ] || [ "$MODE_GIVEN" -eq 0 ]; then print_matrix; exit 0; fi

if [ -z "$STEP" ]; then STEP="all"; fi
case "$MODE" in
  full-usb|full-net|code-net) ;;
  *) die "MODE='$MODE' недопустим. Ожидается: full-usb | full-net | code-net (см. make airgap)";;
esac
case "$STEP" in pack|ship|all) ;; *) die "STEP='$STEP' недопустим (pack|ship|all)";; esac

NEED_SHIP=0
if [ "$STEP" = "ship" ] || [ "$STEP" = "all" ]; then NEED_SHIP=1; fi
case "$MODE" in
  full-usb)
    if [ "$NEED_SHIP" -eq 1 ] && [ -z "$USB" ]; then
      die "MODE=full-usb: нужен USB=<точка монтирования> (напр. USB=/media/usb)"
    fi ;;
  full-net|code-net)
    if [ "$NEED_SHIP" -eq 1 ] && [ -z "$HOST" ] && [ -z "$PIPE_VIA" ]; then
      die "MODE=$MODE: нужен HOST=<ssh-цель> или PIPE=<jump-хост>"
    fi ;;
esac

# ─── ОВ5 guard: code-net fail-closed при изменении моделей ───────────────────
models_dir() {
  local d="${OLLAMA_MODELS:-}"
  if [ -n "$d" ] && [ -d "$d" ]; then printf '%s' "$d"; return; fi
  local c
  for c in "$GIT_ROOT/../models_cache" /kvm/models /var/lib/ollama/models; do
    if [ -d "$c" ]; then printf '%s' "$c"; return; fi
  done
  printf ''
}
models_fingerprint() {
  local d; d="$(models_dir)"
  if [ -z "$d" ]; then printf 'no-models-dir'; return; fi
  ( cd "$d" && find . -type f -printf '%P %s %T@\n' 2>/dev/null | LC_ALL=C sort | sha256sum | awk '{print $1}' )
}
guard_code_net() {
  if [ "$ALLOW_MODEL_DRIFT" = "1" ]; then
    info "ОВ5 guard: пропущен (AIRGAP_ALLOW_MODEL_DRIFT=1)"; return 0
  fi
  local base="$OUT/.airgap-models.sha256"
  if [ ! -f "$base" ]; then
    info "WARN ОВ5 (code-net): baseline моделей не найден — предполагаю, что модели узла НЕ менялись."
    info "     Если модели менялись — используйте MODE=full-net (он переносит и модели)."
    return 0
  fi
  local fp want; fp="$(models_fingerprint)"; want="$(cat "$base")"
  if [ "$fp" != "$want" ]; then
    echo "ОШИБКА: ОВ5 guard (fail-closed): модели изменились (fingerprint $fp != baseline $want)." >&2
    echo "  Режим code-net модели НЕ переносит. Действие: make airgap MODE=full-net STEP=all HOST=aikb" >&2
    echo "  Осознанный обход: AIRGAP_ALLOW_MODEL_DRIFT=1 make airgap MODE=code-net ..." >&2
    exit 1
  fi
  info "ОВ5 guard: модели не изменились (fingerprint совпал)"
}
save_models_baseline() {
  mkdir -p "$OUT"
  models_fingerprint > "$OUT/.airgap-models.sha256" 2>/dev/null || true
  info "baseline моделей обновлён: $OUT/.airgap-models.sha256"
}

# ─── Маппинг режим -> команды pack/ship (поверх движков 038) ─────────────────
PACK=(); SHIP=()
case "$MODE" in
  full-usb)
    PACK=( "$SCRIPT_DIR/airgap-bundle-pack.sh" --out "$OUT" )
    SHIP=( "$SCRIPT_DIR/airgap-bundle-ship.sh" --usb "$USB" --src "$OUT" ) ;;
  full-net)
    PACK=( "$SCRIPT_DIR/airgap-bundle-pack.sh" --out "$OUT" )
    SHIP=( "$SCRIPT_DIR/airgap-bundle-ship.sh" --src "$OUT" )
    if [ -n "$HOST" ]; then SHIP+=( --host "$HOST" ); fi
    if [ -n "$PIPE_VIA" ]; then SHIP+=( --pipe-via "$PIPE_VIA" ); fi
    if [ -n "$DEST" ]; then SHIP+=( --dest "$DEST" ); fi ;;
  code-net)
    PACK=( "$SCRIPT_DIR/airgap-pack-subset.sh" --out "$OUT" )
    SHIP=( "$SCRIPT_DIR/airgap-bundle-ship.sh" --src "$OUT" --files 'mcp-kb-update-*.tar.gz' )
    if [ -n "$HOST" ]; then SHIP+=( --host "$HOST" ); fi
    if [ -n "$PIPE_VIA" ]; then SHIP+=( --pipe-via "$PIPE_VIA" ); fi
    if [ -n "$DEST" ]; then SHIP+=( --dest "$DEST" ); fi ;;
esac
if [ -n "$RSYNC_PATH_ARG" ]; then SHIP+=( --rsync-path "$RSYNC_PATH_ARG" ); fi
if [ -n "$EXTRA" ]; then read -r -a _extra <<< "$EXTRA"; PACK+=( "${_extra[@]}" ); fi

run() { if [ "$DRY" -eq 1 ]; then printf 'DRY: '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }

info "MODE=$MODE STEP=$STEP OUT='$OUT' USB='$USB' HOST='$HOST' PIPE='$PIPE_VIA' DRY=$DRY"
info "NOTE общий outdir: subset (code-net) и полный (full-*) пишут mcp-kb-update-<ISO>.tar.gz в '$OUT' — различаются только ISO-меткой; убирайте старые пакеты."

if [ "$MODE" = "code-net" ] && [ "$STEP" != "ship" ]; then guard_code_net; fi

case "$STEP" in
  pack) run "${PACK[@]}" ;;
  ship) run "${SHIP[@]}" ;;
  all)
    run "${PACK[@]}"
    if [ "$MODE" != "code-net" ] && [ "$DRY" -eq 0 ]; then save_models_baseline; fi
    run "${SHIP[@]}" ;;
esac

if [ "$DRY" -eq 1 ]; then info "DRY-run завершён (ничего не исполнено)."; fi
