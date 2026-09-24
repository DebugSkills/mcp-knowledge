#!/usr/bin/env bash
# verify-deploy.sh — post-deploy проверки работающего стека (2026-09-24).
#
# ЗАЧЕМ: локальный dev-стек = прод для сообщества; `make push` прогоняет
# deploy и завершает этим скриптом. Отдельно: `make verify-deploy` в любой
# момент (стек должен быть поднят).
#
# Проверки (стиль scripts/preflight.sh: exit = число упавших):
#   V1  health      GET :8000/health → status=healthy + reconcile без error
#                   (печатает: status, reconcile.state, orphans, embedding.ok)
#   V2  logs        docker logs mcp-knowledge-server (tail N): 0 строк
#                   error|traceback|critical (нет docker/контейнера → SKIP)
#   V3  MCP tools   POST /mcp tools/list с read-ключом из .env → ≥30
#                   (нет ключа → SKIP с сообщением; ключ НЕ печатается)
#   V4  console     :8085, auth-aware: CONSOLE_AUTH=required+пароль →
#                   без кредов 401 + WWW-Authenticate, с паролем 200;
#                   auth off/пусто без пароля → 200. Пароль НЕ печатается.
#
# Env: VERIFY_WAIT (сек ожидания health, дефолт 120) · VERIFY_LOG_TAIL
#      (строк логов, дефолт 300) · VERIFY_MIN_TOOLS (дефолт 30) · --help
# Выход: exit = число упавших проверок (0 = зелёно). Секреты не печатаются.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
SERVER_HEALTH_URL="${SERVER_HEALTH_URL:-http://localhost:8000/health}"
MCP_URL="${MCP_URL:-http://localhost:8000/mcp}"
CONSOLE_URL="${CONSOLE_URL:-http://localhost:8085/}"
CONTAINER="${VERIFY_CONTAINER:-mcp-knowledge-server}"
WAIT="${VERIFY_WAIT:-120}"
LOG_TAIL="${VERIFY_LOG_TAIL:-300}"
MIN_TOOLS="${VERIFY_MIN_TOOLS:-30}"
PASSED=0; FAILED=0; SKIPPED=0; FAILED_IDS=()

# Цвет — только при TTY (в пайпе вывод чистый)
if [ -t 1 ]; then
    C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
    C_G=""; C_R=""; C_Y=""; C_B=""; C_0=""
fi

say_pass() { printf '%s[PASS]%s V%s %s · %s\n' "$C_G" "$C_0" "$1" "$2" "$3"; }
say_fail() { printf '%s[FAIL]%s V%s %s · %s\n' "$C_R" "$C_0" "$1" "$2" "$3"; }
say_skip() { printf '%s[SKIP]%s V%s %s · %s\n' "$C_Y" "$C_0" "$1" "$2" "$3"; }

# env_val <VAR>: значение из .env (строки `VAR=...`); значения НЕ логируются
env_val() {
    [ -f "$ENV_FILE" ] || return 0
    awk -v kv="$1=" 'index($0, kv) == 1 { print substr($0, length(kv) + 1); exit }' "$ENV_FILE"
}

# python для JSON (приоритет .venv, затем системный)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

finish() {
    echo ""
    echo "${C_B}═══ Verify-deploy: ${C_G}${PASSED} passed${C_0} / ${C_R}${FAILED} failed${C_0} (${FAILED_IDS[*]:-}) / ${C_Y}${SKIPPED} skipped${C_0} ═══${C_0}"
    exit "$FAILED"
}

help() {
    sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --help|-h) help ;;
        *) echo "Неизвестный флаг: $arg (см. --help)" >&2; exit 64 ;;
    esac
done

cd "$ROOT"
echo "${C_B}═══ mcp-knowledge verify-deploy · $(date -Iseconds) ═══${C_0}"

# ── V1 health: status=healthy + reconcile без error (со стартовым окном) ──
v1_health() {
    local body='' deadline=$((SECONDS + WAIT))
    while :; do
        body="$(curl -sf -m 10 "$SERVER_HEALTH_URL" 2>/dev/null || true)"
        [ -n "$body" ] && break
        [ "$SECONDS" -ge "$deadline" ] && break
        sleep 3
    done
    if [ -z "$body" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V1")
        say_fail 1 "health" "нет ответа $SERVER_HEALTH_URL за ${WAIT}s"
        return 0
    fi
    if [ -z "$PY" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V1")
        say_fail 1 "health" "python недоступен для разбора JSON"
        return 0
    fi
    local summary
    if ! summary="$(printf '%s' "$body" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
rec = d.get("reconcile") or {}
emb = (d.get("checks") or {}).get("embedding") or {}
err = rec.get("error")
print(d.get("status", "?"), rec.get("state", "?"),
      "none" if err in (None, "") else "ERROR",
      str(rec.get("orphans", "?")), "ok" if emb.get("ok") else "FAIL", sep="|")
')"; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V1")
        say_fail 1 "health" "JSON не разбирается: $(printf '%s' "$body" | head -c 120)"
        return 0
    fi
    local status state rerr orphans emb
    IFS='|' read -r status state rerr orphans emb <<<"$summary"
    printf '    status=%s · reconcile.state=%s · orphans=%s · embedding.ok=%s\n' \
        "$status" "$state" "$orphans" "$emb"
    if [ "$status" = "healthy" ] && [ "$rerr" = "none" ]; then
        PASSED=$((PASSED + 1))
        say_pass 1 "health /health" "status=healthy, reconcile без error"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V1")
        say_fail 1 "health /health" "status=$status, reconcile.error=$rerr"
    fi
    return 0
}

# ── V2 logs: 0 строк error|traceback|critical в последних N строках ──
v2_logs() {
    if ! command -v docker >/dev/null 2>&1; then
        SKIPPED=$((SKIPPED + 1)); say_skip 2 "server logs" "docker недоступен"
        return 0
    fi
    if [ -z "$(docker ps --filter "name=$CONTAINER" -q 2>/dev/null || true)" ]; then
        SKIPPED=$((SKIPPED + 1))
        say_skip 2 "server logs" "контейнер $CONTAINER не запущен"
        return 0
    fi
    local hits
    hits="$(docker logs --tail "$LOG_TAIL" "$CONTAINER" 2>&1 \
        | grep -icE 'error|traceback|critical' || true)"
    if [ "$hits" -eq 0 ]; then
        PASSED=$((PASSED + 1))
        say_pass 2 "server logs (tail=$LOG_TAIL)" "0 строк error/traceback/critical"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V2")
        say_fail 2 "server logs (tail=$LOG_TAIL)" "$hits строк error/traceback/critical:"
        docker logs --tail "$LOG_TAIL" "$CONTAINER" 2>&1 \
            | grep -iE 'error|traceback|critical' | head -5 | sed 's/^/    | /' || true
    fi
    return 0
}

# ── V3 MCP tools/list: ≥ VERIFY_MIN_TOOLS инструментов с read-ключом ──
v3_tools() {
    local key
    key="$(env_val MCP_READ_KEYS)"; key="${key%%,*}"
    [ -n "$key" ] || key="$(env_val MCP_API_KEY)"
    if [ -z "$key" ]; then
        SKIPPED=$((SKIPPED + 1))
        say_skip 3 "MCP tools/list" "нет MCP_READ_KEYS/MCP_API_KEY в .env"
        return 0
    fi
    local n
    if ! n="$(curl -s -m 30 -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "X-API-Key: $key" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null \
        | "$PY" -c '
import json, sys
raw = sys.stdin.read()
# streamable-http может отдать SSE-фреймы: учитываем строки "data: ..."
if raw.lstrip().startswith(("event:", "data:")):
    raw = "".join(l[5:] for l in raw.splitlines() if l.startswith("data:"))
d = json.loads(raw)
print(len(d["result"]["tools"]))
' 2>/dev/null)" || [ -z "$n" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V3")
        say_fail 3 "MCP tools/list ($MCP_URL)" "запрос/разбор не удался (ключ не выводится)"
        return 0
    fi
    if [ "$n" -ge "$MIN_TOOLS" ]; then
        PASSED=$((PASSED + 1))
        say_pass 3 "MCP tools/list" "$n инструментов (порог $MIN_TOOLS)"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V3")
        say_fail 3 "MCP tools/list" "$n инструментов < порога $MIN_TOOLS"
    fi
    return 0
}

# ── V4 console: auth-aware проверка :8085 (пароль не печатается) ──
v4_console() {
    local mode passwd code headers
    mode="$(env_val CONSOLE_AUTH)"
    passwd="$(env_val CONSOLE_PASSWORD)"
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$CONSOLE_URL" 2>/dev/null || true)"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V4")
        say_fail 4 "console $CONSOLE_URL" "недоступна (нет HTTP-ответа)"
        return 0
    fi
    headers="$(curl -s -D - -o /dev/null -m 10 "$CONSOLE_URL" 2>/dev/null || true)"

    # auth_on: required(+пароль) или auto с заданным паролем (auth фактически включён)
    local auth_on=0
    if { [ "$mode" = "required" ] || [ "$mode" = "auto" ] || [ -z "$mode" ]; } \
       && [ -n "$passwd" ]; then
        auth_on=1
    fi

    if [ "$auth_on" = "1" ]; then
        local code_auth www
        code_auth="$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
            -u "verify:$passwd" "$CONSOLE_URL" 2>/dev/null || true)"
        www="$(printf '%s' "$headers" | grep -i '^www-authenticate:' || true)"
        if [ "$code" = "401" ] && [ -n "$www" ] && [ "$code_auth" = "200" ]; then
            PASSED=$((PASSED + 1))
            say_pass 4 "console (auth=on, mode=${mode:-auto})" \
                "без кредов 401+WWW-Authenticate, с кредами 200"
        else
            FAILED=$((FAILED + 1)); FAILED_IDS+=("V4")
            say_fail 4 "console (auth=on, mode=${mode:-auto})" \
                "без кредов=$code (WWW-Authenticate: $([ -n "$www" ] && echo yes || echo no)), с кредами=$code_auth — ожидалось 401+hdr / 200"
        fi
    else
        # auth off/пусто без пароля → консоль открыта: без кредов 200
        if [ "$code" = "200" ]; then
            PASSED=$((PASSED + 1))
            say_pass 4 "console (auth=off${mode:+, mode=$mode})" "200 без кредов"
        else
            FAILED=$((FAILED + 1)); FAILED_IDS+=("V4")
            say_fail 4 "console (auth=off${mode:+, mode=$mode})" \
                "получен $code без кредов — ожидался 200"
        fi
    fi
    return 0
}

v1_health
v2_logs
v3_tools
v4_console

finish
