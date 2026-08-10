#!/usr/bin/env bash
# backup.sh — бэкап Qdrant (snapshot) + Markdown SSOT (git push / tar)
# Использование: ./backup.sh [--no-ssot] [--no-qdrant] [--test-restore]
#   --test-restore  Проверить полный цикл: snapshot → restore → verify → cleanup
# Cron: ежедневно в 3:00

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
SNAPSHOT_DIR="$DATA_DIR/qdrant/snapshots"
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
backup_ssot_git() {
    echo "[$(date -Iseconds)] Backing up SSOT (git push to bare)..."
    if [ -d "$KNOWLEDGE_DIR/.git" ]; then
        cd "$KNOWLEDGE_DIR"
        git push backup main 2>&1 || {
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

# --- Ротация старых бэкапов ---
rotate_backups() {
    echo "[$(date -Iseconds)] Rotating backups older than ${RETENTION_DAYS} days..."
    find "$BACKUP_DIR" -name "knowledge-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # 2026-08-09: ротация Qdrant-снапшотов (раньше копились бесконечно).
    # Файлы снапшотов теперь в bind-mount (data/qdrant/snapshots) — удаляем по mtime.
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # остаточные .checksum файлы
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot.checksum" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
}

# --- Main ---
echo "=== MCP Knowledge Backup: ${TIMESTAMP} ==="

NO_QDRANT=false
NO_SSOT=false
TEST_RESTORE=false
for arg in "$@"; do
    case "$arg" in
        --no-qdrant)    NO_QDRANT=true ;;
        --no-ssot)      NO_SSOT=true ;;
        --test-restore) TEST_RESTORE=true ;;
        --help|-h)
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--test-restore]"
            echo ""
            echo "Options:"
            echo "  --no-ssot       Skip SSOT (Markdown) backup"
            echo "  --no-qdrant     Skip Qdrant snapshot"
            echo "  --test-restore  Run full restore test cycle and exit"
            echo "  --help, -h      Show this help"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $arg"
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--test-restore]"
            exit 1
            ;;
    esac
done

# G1.2: --test-restore runs a standalone validation cycle
if [ "$TEST_RESTORE" = true ]; then
    test_restore
    exit $?
fi

# Regular backup flow
[ "$NO_QDRANT" = false ] && create_qdrant_snapshot
[ "$NO_SSOT" = false ] && backup_ssot_git
rotate_backups

echo "=== Backup completed: ${TIMESTAMP} ==="
