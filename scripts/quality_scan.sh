#!/usr/bin/env bash
# quality_scan.sh — триггерит periodic quality scan (Фаза 4, задача 4.8)
# Использование:
#   ./quality_scan.sh              # полный скан
#   ./quality_scan.sh --domain eng # скан одного домена
#   ./quality_scan.sh --dry-run    # проверка доступности эндпоинта
#
# Вызывает scanner.run_scan() через MCP JSON-RPC (как reindex.sh).
# Добавить в cron:
#   0 3 * * * cd /app && ./scripts/quality_scan.sh >> /var/log/quality-scan.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_URL="${MCP_URL:-http://localhost:8000/mcp}"
API_KEY="${MCP_WRITE_KEY:-dev-write-key-001}"
TIMEOUT="${QUALITY_SCAN_TIMEOUT:-600}"
DRY_RUN=false
DOMAIN="${QUALITY_SCAN_DOMAIN:-}"

# Парсинг аргументов
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --help)
            echo "Usage: $0 [--domain DOMAIN] [--dry-run]"
            echo "  Triggers quality scan via MCP endpoint."
            echo "  POST $MCP_URL → tools/call { name: run_quality_scan }"
            exit 0
            ;;
        *) shift ;;
    esac
done

echo "[$(date -Iseconds)] Starting quality scan... (domain=${DOMAIN:-all})"

if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] Would call: POST $MCP_URL"
    echo "[DRY-RUN] Tool: run_quality_scan, params: { domain: ${DOMAIN:-null} }"
    exit 0
fi

# Формируем JSON-RPC запрос (как reindex.sh)
ARGS="{}"
if [ -n "$DOMAIN" ]; then
    ARGS="{\"domain\": \"$DOMAIN\"}"
fi

RESPONSE=$(curl -s -X POST "$MCP_URL" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    --max-time "$TIMEOUT" \
    -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"tools/call\", \"params\": {\"name\": \"run_quality_scan\", \"arguments\": $ARGS}}" 2>&1) || {
    echo "ERROR: curl failed (exit=$?). MCP server may not be running."
    echo "  URL: $MCP_URL"
    exit 2
}

# Проверяем ответ
if echo "$RESPONSE" | grep -q '"error"'; then
    echo "ERROR: MCP returned error:"
    echo "$RESPONSE" | head -5
    exit 3
fi

echo "[$(date -Iseconds)] Quality scan completed."
echo "Response: $(echo "$RESPONSE" | head -c 200)"
