#!/usr/bin/env bash
# quality_scan.sh — триггерит quality scanner (Фаза 4)
# Использование: ./quality_scan.sh
# Вызывает scanner.run_scan() внутри mcp-server

set -euo pipefail

echo "[$(date -Iseconds)] Starting quality scan..."

curl -s -X POST "http://localhost:8000/mcp/quality/scan" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${MCP_WRITE_KEY:-dev-write-key-001}" \
    -d '{}' 2>&1 || {
    echo "WARN: Quality scan endpoint not available (Фаза 4 not deployed)"
    exit 1
}

echo "[$(date -Iseconds)] Quality scan completed."
