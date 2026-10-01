#!/usr/bin/env bash
# =============================================================================
# airgap-bundle-ship.sh — ПЕРЕДАЧА офлайн-бандла 038 на узел (USB или сеть).
# Режимы: --usb MOUNT (rsync/cp+sha256+sync) · --host SSH (resumable rsync, rc 3
# без rsync на узле) · --pipe-via JUMP (чанковый cat|ssh, продолжение на частях).
# [N/M] баннеры + тайминги + tee-лог. rc: 0 ok · 1 ошибка · 2 usage · 3 нет rsync.
# =============================================================================
set -euo pipefail

GIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODE=""; USB=""; SSH_TARGET=""; PIPE_VIA=""
SRC="/kvm/update-bundles"; FILES="*.tar.gz"; DEST="/var/tmp/update-bundle"; REMOTE="aikb"
RSYNC_PATH=""; CHUNK="900M"; RETRIES=10; TIMEOUT=60
DRY_RUN=0; CHECKLIST_ONLY=0; LOG_FILE=""
FILES_GIVEN=0; ALLOW_LEGACY=0
LEGACY_NAME="mcp-kb-airgap-bundle.tar.gz"   # старый формат `make bundle` — не часть бандла offline-update
# средняя скорость канала lup→aikb (байт/с) для оценки времени переноса в pipe-режиме
CHAN_BYTES_PER_SEC=650117   # ≈0.62 MiB/s

usage() { cat <<'EOF'
Usage: airgap-bundle-ship.sh (--usb MOUNTPOINT | --host SSH_TARGET | --pipe-via JUMP_HOST)
    [--src DIR=/kvm/update-bundles] [--files '*.tar.gz'] [--dest DIR=/var/tmp/update-bundle]
    [--remote aikb] [--rsync-path CMD] [--chunk 900M] [--retries 10] [--timeout 60]
    [--dry-run] [--checklist-only] [--allow-legacy] [--log FILE] [--help]

Preflight обязателен: файлы проверяются на существование, легаси-имя
mcp-kb-airgap-bundle.tar.gz отвергается (--allow-legacy снимает запрет), а при
маске по умолчанию требуется пара mcp-kb-update-*.tar.gz + mcp-kb-models-*.tar.gz.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --usb) MODE=usb; shift; USB="${1:-}" ;;
        --host) MODE=host; shift; SSH_TARGET="${1:-}" ;;
        --pipe-via) MODE=pipe; shift; PIPE_VIA="${1:-}" ;;
        --src) shift; SRC="${1:-}" ;; --files) shift; FILES="${1:-}"; FILES_GIVEN=1 ;;
        --dest) shift; DEST="${1:-}" ;; --remote) shift; REMOTE="${1:-}" ;;
        --rsync-path) shift; RSYNC_PATH="${1:-}" ;; --chunk) shift; CHUNK="${1:-}" ;;
        --retries) shift; RETRIES="${1:-}" ;; --timeout) shift; TIMEOUT="${1:-}" ;;
        --dry-run) DRY_RUN=1 ;; --checklist-only) CHECKLIST_ONLY=1 ;;
        --allow-legacy) ALLOW_LEGACY=1 ;;
        --log) shift; LOG_FILE="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ОШИБКА: неизвестный флаг $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

have_pv() { command -v pv >/dev/null 2>&1; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }
STEP_T0=0; TOTAL=1
step() { STEP_T0="$(date +%s)"; echo ""; echo "═══ [$1/$TOTAL] $2 ═══"; }
elapsed() { echo "   ⏱ $1: $(( $(date +%s) - STEP_T0 ))s"; }

# двойной хоп JUMP → REMOTE (каталожный pipe проверен: sha256 сходится)
node_ssh() { ssh -o BatchMode=yes "$JUMP" "ssh -o BatchMode=yes $REMOTE $*"; }
# то же, но команда передаётся ОДНИМ аргументом узлу (экранированные кавычки на jump-хосте):
# глобы и редиректы исполняет shell УЗЛА, а не jump-хоста и не промежуточный bash -c
node_ssh_cmd() { ssh -o BatchMode=yes "$JUMP" "ssh -o BatchMode=yes $REMOTE \"$1\""; }
pipe_to_node() { local p="$1"; ssh -o BatchMode=yes "$JUMP" "ssh -o BatchMode=yes $REMOTE \"cat > $p\""; }

FILES_LIST=()
resolve_files() {
    # ВАЖНО: сам $FILES нельзя подставлять в for без set -f — иначе shell развернёт
    # маску по ТЕКУЩЕМУ каталогу (CWD) и подменит её чужим файлом (боевой инцидент:
    # легаси-бандл в корне репо перехватывал '*.tar.gz').
    FILES_LIST=()
    local pat f
    local -a pats=()
    set -f
    pats=( $FILES )
    set +f
    [ ${#pats[@]} -gt 0 ] || return 0
    shopt -s nullglob
    for pat in "${pats[@]}"; do
        for f in "$SRC"/$pat; do FILES_LIST+=("$f"); done
    done
    shopt -u nullglob
}

# Preflight-валидация: существование, запрет легаси-имени, обязательная пара update+models.
validate_artifacts() {
    resolve_files
    [ ${#FILES_LIST[@]} -gt 0 ] || die "нет файлов по маске '$FILES' в $SRC (проверьте --src)"
    local f b size total=0
    for f in "${FILES_LIST[@]}"; do
        [ -f "$f" ] || die "файл не найден: $f (маска '$FILES' в $SRC)"
        b="$(basename "$f")"; size="$(stat -c%s "$f")"; total=$(( total + size ))
        if [ "$b" = "$LEGACY_NAME" ] && [ "$ALLOW_LEGACY" -ne 1 ]; then
            die "это НЕ бандл offline-update: $b (старый формат \`make bundle\`). Ожидается пара mcp-kb-update-*.tar.gz + mcp-kb-models-*.tar.gz (+ python-3.11-slim.tar.gz); пересборка: make bundle-pack ARGS=\"--out $SRC\" (или --allow-legacy, если это осознанно)"
        fi
        echo "   • $b  $(( size / 1024 / 1024 )) МиБ"
    done
    echo "   файлов: ${#FILES_LIST[@]}, суммарно $(( total / 1024 / 1024 )) МиБ"
    if [ "$FILES_GIVEN" -eq 0 ]; then
        local has_update=0 has_models=0
        for f in "${FILES_LIST[@]}"; do b="$(basename "$f")"
            case "$b" in mcp-kb-update-*.tar.gz) has_update=1 ;; mcp-kb-models-*.tar.gz) has_models=1 ;; esac
        done
        [ "$has_update" -eq 1 ] && [ "$has_models" -eq 1 ] \
            || die "в $SRC нет пары mcp-kb-update-*.tar.gz + mcp-kb-models-*.tar.gz (содержимое: $(ls -m "$SRC" 2>/dev/null || echo пусто)). Соберите: make bundle-pack ARGS=\"--out $SRC\""
    fi
    [ "$MODE" != "pipe" ] || echo "   ETA pipe-режима ≈ $(( total / CHAN_BYTES_PER_SEC / 60 )) мин (канал ≈0.62 MiB/s, докачка частями)"
}

print_checklist() { cat <<EOF

ЧЕК-ЛИСТ УЗЛА (после переноса бандла):
  1. airgap-bundle-unpack.sh --bundle $DEST/<файл> [--data-root PATH] [--models-image X] [--python-base Y]
  2. ansible-playbook playbooks/deploy.yml (RUNBOOK 038 §Э6-Э8)
  3. Приёмка /health: embedding.loaded=true · points>0 · reconcile без error
  4. ⚠️ МОДЕЛИ ставятся ДО первого старта стека (unpack до docker compose up)
EOF
}

print_plan() {
    echo "ПЛАН (--dry-run, ничего не передаётся):"
    case "$MODE" in
        usb) printf '  [1/4] preflight: %s записываема + место ≥ размер+5%%\n  [2/4] копирование rsync --partial (fallback cp)\n  [3/4] сверка sha256 каждого файла\n  [4/4] sync + чек-лист узла\n' "$USB" ;;
        host) printf '  [1/3] preflight: rsync на %s; нет → rc 3 + apt\n  [2/3] rsync --append-verify (resumable, retries=%s)\n  [3/3] сверка sha256 на узле + чек-лист\n' "$REMOTE" "$RETRIES" ;;
        pipe) printf '  [1/3] preflight: файлы + split -b %s\n  [2/3] передача частей (cat|ssh) + sha256 части\n  [3/3] сборка cat частей > файл + финальная sha256 + чек-лист\n' "$CHUNK" ;;
    esac
}

if [ "$CHECKLIST_ONLY" -eq 1 ]; then print_checklist; exit 0; fi

case "$MODE" in
    usb) [ -n "$USB" ] || { echo "ОШИБКА: --usb требует MOUNTPOINT" >&2; usage >&2; exit 2; }; JUMP="" ;;
    host) [ -n "$SSH_TARGET" ] || { echo "ОШИБКА: --host требует SSH_TARGET" >&2; usage >&2; exit 2; }; JUMP="$SSH_TARGET" ;;
    pipe) [ -n "$PIPE_VIA" ] || { echo "ОШИБКА: --pipe-via требует JUMP_HOST" >&2; usage >&2; exit 2; }; JUMP="$PIPE_VIA" ;;
    "") echo "ОШИБКА: укажите --usb | --host | --pipe-via" >&2; usage >&2; exit 2 ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then print_plan; echo "   preflight:"; validate_artifacts; exit 0; fi

# ранний гейт usb: mountpoint существует (ДО лога — rc 2 без побочек)
if [ "$MODE" = "usb" ] && [ ! -d "$USB" ]; then echo "ОШИБКА: --usb $USB не существует" >&2; exit 2; fi

TS="$(date +%Y%m%d-%H%M%S)"
[ -n "$LOG_FILE" ] || LOG_FILE="$SRC/ship-$TS.log"
mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "лог: $LOG_FILE"

cmd_usb() {
    TOTAL=4
    step 1 "preflight (mountpoint + место)"
    [ -w "$USB" ] || die "точка монтирования $USB не записываема"
    validate_artifacts
    total="$(du -cb "${FILES_LIST[@]}" | tail -1 | cut -f1)"
    usb_free="$(df -P -B1 "$USB" | awk 'NR==2 {print $4}')"
    [ "${usb_free:-0}" -ge $(( total * 105 / 100 )) ] || die "свободно на USB меньше размера+5%"
    echo "   файлов: ${#FILES_LIST[@]}, суммарно $(( total / 1024 / 1024 )) МиБ"

    step 2 "копирование"
    if command -v rsync >/dev/null 2>&1; then
        rsync -ah --partial --info=progress2 "${FILES_LIST[@]}" "$USB/"
    else
        echo "   rsync нет локально → cp -a"; cp -a "${FILES_LIST[@]}" "$USB/"
    fi

    step 3 "сверка sha256"
    for f in "${FILES_LIST[@]}"; do
        b="$(basename "$f")"
        s="$(sha256sum "$f" | awk '{print $1}')"; d="$(sha256sum "$USB/$b" | awk '{print $1}')"
        [ "$s" = "$d" ] || die "sha256 расхождение: $b"
        echo "   OK $b"
    done

    step 4 "sync + чек-лист"
    sync; print_checklist
    echo ""; echo "   Напоминание: umount $USB перед извлечением носителя."
}

cmd_host() {
    TOTAL=3
    step 1 "preflight (rsync на узле $REMOTE)"
    local rc=0 out=""
    out="$(node_ssh "command -v rsync" 2>/dev/null)" || rc=$?
    if [ "$rc" -eq 0 ]; then echo "   rsync на узле: $out"
    elif [ "$rc" -eq 1 ]; then
        echo "rsync НЕ найден на $REMOTE. Установите на узле (root):" >&2
        echo "  ssh -o BatchMode=yes $JUMP \"ssh -o BatchMode=yes $REMOTE apt-get install -y rsync\"" >&2
        echo "  затем повторите передачу (--host)." >&2
        exit 3
    else die "не удалось подключиться к $REMOTE через $JUMP (ssh rc=$rc)"; fi
    validate_artifacts
    node_ssh "mkdir -p $DEST" || die "не удалось создать $DEST на $REMOTE (права? путь?)"
    local rsh="ssh -o BatchMode=yes $JUMP" rpath="${RSYNC_PATH:-rsync}"

    step 2 "передача rsync (resumable)"
    local attempt=1
    while [ "$attempt" -le "$RETRIES" ]; do
        echo "   attempt $attempt/$RETRIES"
        if rsync -a --partial --append-verify --info=progress2 --timeout="$TIMEOUT" \
             -e "$rsh" --rsync-path "$rpath" "${FILES_LIST[@]}" "$JUMP:$DEST/"; then break; fi
        attempt=$(( attempt + 1 ))
        [ "$attempt" -le "$RETRIES" ] && { echo "   пауза 5с …"; sleep 5; }
    done
    [ "$attempt" -le "$RETRIES" ] || die "rsync не завершился за $RETRIES попыток"

    step 3 "сверка sha256 (на узле) + чек-лист"
    for f in "${FILES_LIST[@]}"; do
        b="$(basename "$f")"
        s="$(sha256sum "$f" | awk '{print $1}')"
        d="$(node_ssh "sha256sum $DEST/$b" 2>/dev/null | LC_ALL=C awk '{print $1}')"
        [ "$s" = "$d" ] || die "sha256 расхождение на узле: $b"
        echo "   OK $b"
    done
    print_checklist
}

cmd_pipe() {
    TOTAL=3
    step 1 "preflight (файлы + чанки)"
    validate_artifacts
    # узел: каталог приёма и .ship-part обязаны существовать (иначе первый же chunk падает)
    node_ssh "mkdir -p $DEST/.ship-part" \
        || die "не удалось создать $DEST/.ship-part на $REMOTE (права? путь?)"
    echo "   узел $REMOTE:$DEST готов (.ship-part создан)"
    echo "   чанк: $CHUNK, retries: $RETRIES"
    local tmpd; tmpd="$(mktemp -d /tmp/kilo/airgap-ship.XXXXXX)"

    step 2 "передача частей (cat|ssh, resumable)"
    for f in "${FILES_LIST[@]}"; do
        local b; b="$(basename "$f")"
        echo "   файл: $b"
        split -b "$CHUNK" "$f" "$tmpd/$b.part-"
        for part in "$tmpd"/"$b".part-*; do
            local pname psize psum rsum
            pname="$(basename "$part")"; psize="$(stat -c%s "$part")"
            psum="$(sha256sum "$part" | awk '{print $1}')"
            rsum="$(node_ssh "sha256sum $DEST/.ship-part/$pname" 2>/dev/null | LC_ALL=C awk '{print $1}')" || rsum=""
            if [ "$rsum" = "$psum" ]; then echo "     skip $pname (уже передана)"; continue; fi
            if have_pv; then pv -s "$psize" -N "$pname" -p -e -r -b < "$part" | pipe_to_node "$DEST/.ship-part/$pname"
            else cat "$part" | pipe_to_node "$DEST/.ship-part/$pname"; fi
            rsum="$(node_ssh "sha256sum $DEST/.ship-part/$pname" 2>/dev/null | LC_ALL=C awk '{print $1}')" || rsum=""
            [ "$rsum" = "$psum" ] || die "sha256 части $pname не сошлась"
            echo "     ok $pname"
        done
    done

    step 3 "сборка + финальная сверка + чек-лист"
    for f in "${FILES_LIST[@]}"; do
        local b; b="$(basename "$f")"
        node_ssh_cmd "cat $DEST/.ship-part/$b.part-* > $DEST/$b"
        local s d
        s="$(sha256sum "$f" | awk '{print $1}')"
        d="$(node_ssh "sha256sum $DEST/$b" 2>/dev/null | LC_ALL=C awk '{print $1}')"
        [ "$s" = "$d" ] || die "финальная sha256 расхождение: $b"
        node_ssh_cmd "rm -f $DEST/.ship-part/$b.part-*"
        echo "   OK $b (части удалены на узле)"
    done
    rm -rf "$tmpd"; print_checklist
}

case "$MODE" in usb) cmd_usb ;; host) cmd_host ;; pipe) cmd_pipe ;; esac
echo ""
echo "✅ bundle-ship завершён. Лог: $LOG_FILE"
