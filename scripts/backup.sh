#!/usr/bin/env bash
# backup.sh — бэкап Qdrant (snapshot) + Markdown SSOT (git push / tar)
# Использование: ./backup.sh [--no-ssot] [--no-qdrant] [--test-restore]
#   --test-restore  Проверить полный цикл: snapshot → restore → verify → cleanup
# Cron: ежедневно в 3:00

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# DATA_ROOT (code-2026-09-22-002): корень данных ВНЕ git-клона. Env-override:
# прод передаёт DATA_ROOT через crontab-env (deploy.yml, cron-секция), cron-скрипты
# не умеют .env; дефолт = прежнее dev-поведение (data внутри клона).
DATA_ROOT="${DATA_ROOT:-$PROJECT_DIR/data}"
BACKUP_DIR="$DATA_ROOT/backups"
SNAPSHOT_DIR="$DATA_ROOT/qdrant/snapshots"
KNOWLEDGE_DIR="$PROJECT_DIR/../knowledge"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RETENTION_DAYS=7

QDDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

# --- Общие утилиты ---

# get_collection_points <collection_name> → число
get_collection_points() {
    local collection="$1"
    curl -s "${QDDRANT_URL}/collections/${collection}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null
}

# --- Qdrant snapshot ---
# 2026-08-09: снапшот ВСЕХ коллекций, КРОМЕ ws-*.
# Решение владельца (docs/qdrant-ws-collections-message.md в feature/Svyazi):
# коллекции ws-* НЕ принадлежат Svyazi (подтверждено, 2026-08-09) — они чужие,
# в бэкапы mcp-knowledge не включаются. Снапшотим только свои (knowledge и др.).
create_qdrant_snapshot() {
    echo "[$(date -Iseconds)] Creating Qdrant snapshots (excluding ws-*)..."
    local collections
    collections=$(curl -s "${QDDRANT_URL}/collections" \
        | python3 -c "import sys,json; print(' '.join(c['name'] for c in json.load(sys.stdin)['result']['collections'] if not c['name'].startswith('ws-')))" 2>/dev/null)
    if [ -z "$collections" ]; then
        echo "WARN: Qdrant snapshot failed (server not running?). Skipping."
        return 1
    fi
    echo "    Collections: ${collections}"
    local c ok=1
    for c in $collections; do
        local resp actual
        resp=$(curl -s -X POST "${QDDRANT_URL}/collections/${c}/snapshots" \
            -H "Content-Type: application/json" \
            -d '{"name": "backup-'"${TIMESTAMP}"'"}' 2>&1)
        # Qdrant игнорирует переданный name и генерирует фактическое имя файла
        # ({collection}-{id}-{timestamp}.snapshot) — берём его из ответа API.
        actual=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null)
        if [ -n "$actual" ]; then
            echo "    ✔ ${c}: ${actual}"
            validate_snapshot "${c}" "${actual}" || ok=0
        else
            echo "    ✖ ${c}: ${resp}"
            ok=0
        fi
    done
    return $ok
}

# --- Snapshot validation (G1.1) ---
# validate_snapshot <collection> <snapshot_name>
validate_snapshot() {
    local collection="$1"
    local snapshot_name="$2"
    local snapshot_file="${SNAPSHOT_DIR}/${collection}/${snapshot_name}"

    echo "[$(date -Iseconds)] Validating snapshot: ${snapshot_name}..."

    if [ ! -f "${snapshot_file}" ]; then
        echo "ERROR: Snapshot file not found: ${snapshot_file}"
        echo "       Check that Qdrant snapshots directory is mounted correctly."
        exit 1
    fi

    local size
    size=$(stat -c%s "${snapshot_file}" 2>/dev/null || echo 0)
    if [ "${size}" -eq 0 ]; then
        echo "ERROR: Snapshot file is empty: ${snapshot_file}"
        exit 1
    fi

    local size_human
    if command -v numfmt &>/dev/null; then
        size_human=$(numfmt --to=iec "${size}")
    else
        size_human="${size} bytes"
    fi

    echo "OK: Snapshot validated — ${snapshot_name} (size: ${size_human})"
    return 0
}

# --- Test Restore (G1.2) ---
# Полный цикл: snapshot → restore to temp collection → verify point count → cleanup
test_restore() {
    echo "=== Test Restore: Snapshot → Temp Collection → Verify → Cleanup ==="
    echo ""

    # Step 1: Create a fresh snapshot for testing
    echo "[$(date -Iseconds)] Step 1/5: Creating test snapshot 'test-restore-${TIMESTAMP}'..."
    local snapshot_resp test_snapshot
    snapshot_resp=$(curl -s -X POST "${QDDRANT_URL}/collections/knowledge/snapshots" \
        -H "Content-Type: application/json" \
        -d '{"name": "test-restore-'"${TIMESTAMP}"'"}' 2>&1) || {
        echo "ERROR: Failed to create snapshot for test restore. Is Qdrant running?"
        return 1
    }
    # Qdrant генерирует фактическое имя файла — берём из ответа API
    test_snapshot=$(echo "$snapshot_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null)
    echo "    Actual snapshot: ${test_snapshot}"
    echo "    Response: ${snapshot_resp}"

    # Step 1b: Validate the snapshot
    validate_snapshot "knowledge" "${test_snapshot}" || return 1
    echo ""

    # Step 2: Get point count of the original collection
    echo "[$(date -Iseconds)] Step 2/5: Getting point count of 'knowledge'..."
    local orig_count
    orig_count=$(get_collection_points "knowledge") || {
        echo "ERROR: Failed to get point count of 'knowledge' collection"
        return 1
    }
    echo "    Original collection 'knowledge' has ${orig_count} points"
    echo ""

    # Step 3: Restore snapshot to a temporary collection
    local test_collection="knowledge_restore_test"
    echo "[$(date -Iseconds)] Step 3/5: Restoring snapshot to '${test_collection}'..."
    local restore_resp
    restore_resp=$(curl -s -X PUT "${QDDRANT_URL}/collections/${test_collection}/snapshots/recover" \
        -H "Content-Type: application/json" \
        -d '{"location": "file:///qdrant/storage/snapshots/knowledge/'"${test_snapshot}"'"}' 2>&1) || {
        echo "ERROR: Failed to restore snapshot to '${test_collection}'"
        return 1
    }
    echo "    Response: ${restore_resp}"

    # Qdrant processes snapshot recovery asynchronously — wait for it
    echo "    Waiting for restore to complete..."
    sleep 3
    echo ""

    # Step 4: Verify point count matches
    echo "[$(date -Iseconds)] Step 4/5: Verifying point count of '${test_collection}'..."
    local restored_count
    restored_count=$(get_collection_points "${test_collection}") || {
        echo "ERROR: Failed to get point count of '${test_collection}'"
        # Attempt cleanup even on failure
        curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" > /dev/null 2>&1 || true
        return 1
    }
    echo "    Restored collection '${test_collection}' has ${restored_count} points"

    if [ "${orig_count}" -eq "${restored_count}" ]; then
        echo ""
        echo "✅ RESTORE TEST PASSED: Point counts match (${orig_count} == ${restored_count})"
    else
        echo ""
        echo "❌ RESTORE TEST FAILED: Point count mismatch"
        echo "   Original:  ${orig_count}"
        echo "   Restored:  ${restored_count}"
        # Cleanup before exiting with error
        curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" > /dev/null 2>&1 || true
        return 1
    fi
    echo ""

    # Step 5: Cleanup — delete temporary collection
    echo "[$(date -Iseconds)] Step 5/5: Cleaning up temporary collection '${test_collection}'..."
    local delete_resp
    delete_resp=$(curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" 2>&1) || {
        echo "WARN: Failed to delete temporary collection '${test_collection}' (manual cleanup may be required)"
    }
    echo "    Deleted: ${delete_resp}"

    echo ""
    echo "=== Test Restore completed successfully ==="
    return 0
}

# --- SSOT backup (git push to bare remote) ---
# P0-1 (code-2026-09-22-002): git -C вместо cd — cd без возврата ломал
# последующие функции с относительными путями (backup_console_state тихо скипала).
backup_ssot_git() {
    echo "[$(date -Iseconds)] Backing up SSOT (git push to bare)..."
    if [ -d "$KNOWLEDGE_DIR/.git" ]; then
        git -C "$KNOWLEDGE_DIR" push backup main 2>&1 || {
            echo "WARN: git push backup failed. Falling back to tar."
            backup_ssot_tar
        }
    else
        echo "WARN: $KNOWLEDGE_DIR is not a git repo. Falling back to tar."
        backup_ssot_tar
    fi
}

# --- SSOT backup (tar fallback) ---
backup_ssot_tar() {
    echo "[$(date -Iseconds)] Backing up SSOT (tar)..."
    mkdir -p "$BACKUP_DIR"
    tar -czf "$BACKUP_DIR/knowledge-${TIMESTAMP}.tar.gz" \
        -C "$(dirname "$KNOWLEDGE_DIR")" "$(basename "$KNOWLEDGE_DIR")" 2>&1
    echo "[$(date -Iseconds)] SSOT tar: $BACKUP_DIR/knowledge-${TIMESTAMP}.tar.gz"
}

# --- Console state backup (kb-console-roles Ф4.4, P2-5a) ---
# users.jsonl (pbkdf2-хэши — секретов нет) + users_audit.jsonl + tokens.jsonl
# (тот же класс данных; дыра в бэкап-контуре отмечена ещё в 001).
# Восстановление = копия файлов + рестарт контейнеров.
# P0-1 (code-2026-09-22-002): АБСОЛЮТНЫЕ пути от $DATA_ROOT — прежние относительные
# data/console ломались после cd "$KNOWLEDGE_DIR" в backup_ssot_git → тихий скип.
backup_console_state() {
    echo "[$(date -Iseconds)] Backing up console state (users/tokens)..."
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    local items=()
    local dir
    for dir in "$DATA_ROOT/console" "$DATA_ROOT/tokens"; do
        if [ -d "$dir" ] && ls "$dir"/*.jsonl >/dev/null 2>&1; then
            items+=("$dir")
        fi
    done
    if [ ${#items[@]} -eq 0 ]; then
        echo "[$(date -Iseconds)] Console state: нет users/tokens файлов — пропуск."
        return 0
    fi
    # -C "$DATA_ROOT": в таре относительные имена (console/, tokens/) —
    # распаковка restore-скриптом в любой каталог без разворока абсолютных путей
    tar -czf "$BACKUP_DIR/console-state-${TIMESTAMP}.tar.gz" -C "$DATA_ROOT" \
        $(for dir in "${items[@]}"; do basename "$dir"; done) 2>&1
    echo "[$(date -Iseconds)] Console state tar: $BACKUP_DIR/console-state-${TIMESTAMP}.tar.gz"
}

# --- Secrets backup (.env; P1-4/P2-9, code-2026-09-22-002) ---
# .env = ключи всех уровней + пароль консоли — единственный незеркалируемый
# конфиг. vault.yml в тар НЕ включаем (сам зашифрован ansible-vault, едет на
# offsite отдельно). Guard P2-9: dev .env может отсутствовать (gitignored,
# необязателен) — skip с сообщением, НЕ ошибка (set -euo pipefail не роняет nightly).
backup_secrets() {
    echo "[$(date -Iseconds)] Backing up secrets (.env)..."
    if [ -f "$PROJECT_DIR/.env" ]; then
        mkdir -p "$BACKUP_DIR"
        chmod 700 "$BACKUP_DIR"
        tar -czf "$BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz" -C "$PROJECT_DIR" .env 2>&1
        chmod 600 "$BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz"
        echo "[$(date -Iseconds)] Secrets tar: $BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz (mode 600)"
    else
        echo "[$(date -Iseconds)] secrets: .env отсутствует — пропуск (не ошибка)."
    fi
}

# --- Errors state backup (Error→Rule Ф4, code-2026-09-22-003) ---
# Sink-состояние цикла Error→Rule: config.json + alert_state.json +
# aggregates/signatures.json. Без raw-событий (восстанавливаемы ретро-сканом
# логов) и БЕЗ notify.json — TG-токен в тары НЕ попадает (P2-10; notify.json
# рендерится заново ansible errors.yml setup из vault).
backup_errors_state() {
    echo "[$(date -Iseconds)] Backing up errors state (config/aggregates/alert_state)..."
    local sink="$DATA_ROOT/logs/errors"
    if [ ! -d "$sink" ]; then
        echo "[$(date -Iseconds)] errors state: sink отсутствует — пропуск (не ошибка)."
        return 0
    fi
    local items=()
    local f
    for f in config.json alert_state.json; do
        [ -f "$sink/$f" ] && items+=("$f")
    done
    [ -f "$sink/aggregates/signatures.json" ] && items+=("aggregates/signatures.json")
    if [ ${#items[@]} -eq 0 ]; then
        echo "[$(date -Iseconds)] errors state: нет файлов состояния — пропуск."
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    # -C "$sink": относительные имена (config.json, aggregates/) — как console-state
    tar -czf "$BACKUP_DIR/errors-state-${TIMESTAMP}.tar.gz" -C "$sink" "${items[@]}" 2>&1
    echo "[$(date -Iseconds)] errors state tar: $BACKUP_DIR/errors-state-${TIMESTAMP}.tar.gz"
}

# --- Ротация старых бэкапов ---
rotate_backups() {
    echo "[$(date -Iseconds)] Rotating backups older than ${RETENTION_DAYS} days..."
    find "$BACKUP_DIR" -name "knowledge-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "console-state-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "secrets-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # Error→Rule state (Ф4): тот же retention, что остальные тары
    find "$BACKUP_DIR" -name "errors-state-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # 2026-08-09: ротация Qdrant-снапшотов (раньше копились бесконечно).
    # Файлы снапшотов теперь в bind-mount (data/qdrant/snapshots) — удаляем по mtime.
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # остаточные .checksum файлы
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot.checksum" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # Ф3 P2-7: weekly-каталоги старше ~4 недель (по mtime каталога, имена не парсятся)
    find "$SNAPSHOT_DIR/weekly" -mindepth 1 -maxdepth 1 -mtime +28 -exec rm -rf {} + 2>/dev/null || true
}

# --- Weekly-4 снапшоты (P2-7, code-2026-09-22-002 Ф3) ---
# Имена Qdrant-снапшотов произвольные ({collection}-{uuid}-{ts}.snapshot) —
# find по суффсуксу даты невозможен. Вместо этого: по воскресеньям (date +%u = 7)
# каталог weekly/%G-W%V, куда КОПИРУЕТСЯ последний по mtime снапшот каждой
# коллекции; ротация по mtime каталогов (-mtime +28 ≈ 4 недели, имена не парсятся).
# Offsite (§11) забирает только weekly/-поддерево.
backup_weekly_snapshots() {
    if [ "$(date +%u)" != "7" ]; then
        return 0
    fi
    local week_dir="$SNAPSHOT_DIR/weekly/$(date +%G-W%V)"
    echo "[$(date -Iseconds)] Weekly-4: копируем последние снапшоты в ${week_dir}..."
    mkdir -p "$week_dir"
    local coll_dir latest
    for coll_dir in "$SNAPSHOT_DIR"/*/; do
        [ -d "$coll_dir" ] || continue
        local cname
        cname="$(basename "$coll_dir")"
        [ "$cname" = "weekly" ] && continue
        latest="$(ls -t "$coll_dir"/*.snapshot 2>/dev/null | head -1 || true)"
        if [ -n "$latest" ]; then
            cp -p "$latest" "$week_dir/"
            echo "    ✔ ${cname}: $(basename "$latest")"
        else
            echo "    — ${cname}: снапшотов нет, пропуск"
        fi
    done
    echo "[$(date -Iseconds)] Weekly-4: готово ($(ls "$week_dir" | wc -l) файлов)."
}

# --- Console-state untar-drill (P2-6, code-2026-09-22-002 Ф3) ---
# Последний console-state-тар: tar -tzf (целостность) → untar во временный каталог
# → каждая строка users.jsonl/users_audit.jsonl/tokens.jsonl парсится json.loads
# → cleanup. Возвращает 0/1; отсутствие тара — skip (return 0 с сообщением).
verify_console_drill() {
    echo "[$(date -Iseconds)] Console untar-drill (P2-6)..."
    local tar_file
    tar_file="$(ls -t "$BACKUP_DIR"/console-state-*.tar.gz 2>/dev/null | head -1 || true)"
    if [ -z "$tar_file" ]; then
        echo "    — console-state-таров нет — drill пропущен (не ошибка)."
        return 0
    fi
    echo "    Tar: $tar_file"
    tar -tzf "$tar_file" > /dev/null || {
        echo "    ✖ tar -tzf FAILED (архив битый): $tar_file"
        return 1
    }
    local tmp
    tmp="$(mktemp -d)"
    tar -xzf "$tar_file" -C "$tmp" || { rm -rf "$tmp"; return 1; }
    local jsonl rc=0
    while IFS= read -r jsonl; do
        local n
        n="$(python3 -c "
import sys, json
ok = 0
with open(sys.argv[1], encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            ok += 1
        except json.JSONDecodeError as e:
            print(f'BAD line {i}: {e}', file=sys.stderr)
            sys.exit(1)
print(ok)
" "$jsonl")" || rc=1
        if [ "$rc" -eq 0 ]; then
            echo "    ✔ $(basename "$jsonl"): ${n} строк, все валидный JSON"
        else
            echo "    ✖ $(basename "$jsonl"): битые строки JSON"
            break
        fi
    done < <(find "$tmp" -name '*.jsonl' -type f | sort)
    rm -rf "$tmp"
    return "$rc"
}

# --- Verify-режим (--verify, code-2026-09-22-002 Ф3; В2(б) «не молчи когда всё ок») ---
# НЕ создаёт новые бэкапы — проверяет существующие:
#   1) qdrant test-restore (ПОЛНЫЙ цикл recover — ТРЕБУЕТ живого Qdrant;
#      с --no-qdrant пропускается с сообщением — это задокументированное поведение)
#   2) sha256-контрольная сумма последних таров каждого типа (отсутствие = skip)
#   3) console untar-drill (P2-6)
# Итог: «RESTORE TEST PASSED/FAILED», non-zero exit при провале.
verify_mode() {
    echo "=== VERIFY MODE: восстановимость бэкапов ==="
    local fails=0 checks=0 rc

    # 1. Qdrant test-restore
    if [ "$NO_QDRANT" = true ]; then
        echo "[SKIP] Qdrant test-restore: --no-qdrant (требует живого Qdrant :6333)."
    else
        checks=$((checks + 1))
        test_restore && rc=0 || rc=1
        [ "$rc" -ne 0 ] && fails=$((fails + 1))
    fi

    # 2. sha256 последних таров каждого типа
    local kind f
    for kind in knowledge console-state secrets; do
        f="$(ls -t "$BACKUP_DIR"/${kind}-*.tar.gz 2>/dev/null | head -1 || true)"
        if [ -z "$f" ]; then
            echo "[SKIP] sha256 ${kind}: таров нет."
            continue
        fi
        checks=$((checks + 1))
        if sha256sum "$f"; then
            echo "    ✔ sha256 OK: $(basename "$f")"
        else
            echo "    ✖ sha256 FAILED: $f"
            fails=$((fails + 1))
        fi
    done

    # 3. Console untar-drill
    checks=$((checks + 1))
    verify_console_drill && rc=0 || rc=1
    [ "$rc" -ne 0 ] && fails=$((fails + 1))

    echo ""
    if [ "$fails" -eq 0 ] && [ "$checks" -gt 0 ]; then
        echo "✅ RESTORE TEST PASSED (${checks} проверок, 0 провалов)"
        return 0
    elif [ "$checks" -eq 0 ]; then
        echo "❌ RESTORE TEST FAILED: нечего проверять (0 проверок) — бэкапов нет?"
        return 1
    else
        echo "❌ RESTORE TEST FAILED: ${fails}/${checks} проверок провалено"
        return 1
    fi
}

# --- Main ---
echo "=== MCP Knowledge Backup: ${TIMESTAMP} ==="

NO_QDRANT=false
NO_SSOT=false
TEST_RESTORE=false
VERIFY=false
for arg in "$@"; do
    case "$arg" in
        --no-qdrant)    NO_QDRANT=true ;;
        --no-ssot)      NO_SSOT=true ;;
        --test-restore) TEST_RESTORE=true ;;
        --verify)       VERIFY=true ;;
        --help|-h)
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--test-restore] [--verify]"
            echo ""
            echo "Options:"
            echo "  --no-ssot       Skip SSOT (Markdown) backup"
            echo "  --no-qdrant     Skip Qdrant snapshot"
            echo "  --test-restore  Run full restore test cycle and exit"
            echo "  --verify        Verify existing backups (test-restore + sha256 + console-drill) and exit"
            echo "  --help, -h      Show this help"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $arg"
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--test-restore] [--verify]"
            exit 1
            ;;
    esac
done

# G1.2: --test-restore runs a standalone validation cycle
if [ "$TEST_RESTORE" = true ]; then
    test_restore
    exit $?
fi

# Ф3 (code-2026-09-22-002): --verify проверяет существующие бэкапы
# (с --no-qdrant Qdrant-часть пропускается — работает без живого сервера).
if [ "$VERIFY" = true ]; then
    verify_mode
    exit $?
fi

# Regular backup flow
[ "$NO_QDRANT" = false ] && create_qdrant_snapshot
[ "$NO_QDRANT" = false ] && backup_weekly_snapshots   # P2-7: вс-копии (no-op в остальные дни)
[ "$NO_SSOT" = false ] && backup_ssot_git
backup_console_state   # kb-console-roles Ф4.4: users.jsonl + users_audit + tokens
backup_secrets         # code-2026-09-22-002 P1-4: .env (guard P2-9 — skip без файла)
backup_errors_state    # Error→Rule Ф4: config/aggregates/alert_state (без notify.json)
rotate_backups

echo "=== Backup completed: ${TIMESTAMP} ==="
