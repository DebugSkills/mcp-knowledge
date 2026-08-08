#!/usr/bin/env bash
# reindex.sh — полный перестроение индекса из Markdown SSOT
# Использование: ./reindex.sh
# Вызывает MCP Tool reindex() внутри mcp-server

set -euo pipefail

echo "=== Starting full reindex from Markdown SSOT ==="
echo "[$(date -Iseconds)] Triggering reindex via MCP..."

# Вызов reindex через MCP JSON-RPC (как quality_scan.sh)
curl -s -X POST "http://localhost:8000/mcp" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${MCP_WRITE_KEY:-dev-write-key-001}" \
    -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "reindex", "arguments": {}}}' 2>&1 || {
    echo "WARN: MCP reindex failed. Alternative: restart mcp-server (reconciliation при старте #19)"
    exit 1
}

echo "[$(date -Iseconds)] Reindex completed."
