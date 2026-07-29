#!/usr/bin/env bash
# backup.sh — бэкап Qdrant (snapshot) + Markdown SSOT (git push / tar)
# Использование: ./backup.sh [--no-ssot] [--no-qdrant]
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

# --- Qdrant snapshot ---
create_qdrant_snapshot() {
    echo "[$(date -Iseconds)] Creating Qdrant snapshot..."
    curl -s -X POST "http://localhost:6333/collections/knowledge/snapshots" \
        -H "Content-Type: application/json" \
        -d '{"name": "backup-'"${TIMESTAMP}"'"}' || {
        echo "WARN: Qdrant snapshot failed (server not running?). Skipping."
        return 1
    }
    echo "[$(date -Iseconds)] Qdrant snapshot: backup-${TIMESTAMP}"
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
    # Qdrant snapshots хранятся в Qdrant; ротация — через API (опционально)
}

# --- Main ---
echo "=== MCP Knowledge Backup: ${TIMESTAMP} ==="

NO_QDRANT=false
NO_SSOT=false
for arg in "$@"; do
    case "$arg" in
        --no-qdrant) NO_QDRANT=true ;;
        --no-ssot)   NO_SSOT=true ;;
    esac
done

[ "$NO_QDRANT" = false ] && create_qdrant_snapshot
[ "$NO_SSOT" = false ] && backup_ssot_git
rotate_backups

echo "=== Backup completed: ${TIMESTAMP} ==="
