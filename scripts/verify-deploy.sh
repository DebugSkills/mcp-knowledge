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
#                   [ERROR]/[CRITICAL]/CRITICAL/Traceback/[FATAL] — УРОВЕНЬ
#                   лога, не подстрока (INFO errors=1 — НЕ FAIL; нет docker → SKIP)
#   V3  MCP tools   POST /mcp tools/list с read-ключом из .env → ≥30
#                   (нет ключа → SKIP с сообщением; ключ НЕ печатается)
#   V4  console     :8085, auth-aware: CONSOLE_AUTH=required+пароль →
#                   без кредов 401 + WWW-Authenticate, с паролем 200;
#                   auth off/пусто без пароля → 200. Пароль НЕ печатается.
#
# Env: VERIFY_WAIT (сек ожидания health, дефолт 120) · VERIFY_LOG_TAIL
#      (строк логов, дефолт 300) · VERIFY_MIN_TOOLS (дефолт 30) ·
#      VERIFY_LOG_FILE / VERIFY_LOG_CMD (источник лога V2 вместо docker;
#      для тестов) · --help
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
LOG_FILE="${VERIFY_LOG_FILE:-}"   # тест-хук: V2 читает файл вместо docker logs
LOG_CMD="${VERIFY_LOG_CMD:-}"     # тест-хук: V2 читает вывод команды (bash -c)
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

# ── V2 logs: 0 строк [ERROR]/[CRITICAL]/CRITICAL/Traceback/[FATAL] ──
# Матчим УРОВЕНЬ лога (формат: `YYYY-MM-DD HH:MM:SS,mmm [LEVEL] name: msg`),
# а не подстроку: INFO/WARNING-строки с `errors=1`/`error_count=`/NotFound
# не являются сбоем. Источник лога: docker logs (дефолт) либо VERIFY_LOG_FILE
# (файл) / VERIFY_LOG_CMD (команда) — переопределяется для тестов.
V2_BAD_RE='\[(ERROR|FATAL)\]|CRITICAL|Traceback \(most recent call last\)'

v2_fetch_logs() {
    if [ -n "$LOG_FILE" ]; then
        tail -n "$LOG_TAIL" "$LOG_FILE" 2>/dev/null || true
    elif [ -n "$LOG_CMD" ]; then
        bash -c "$LOG_CMD" 2>&1 | tail -n "$LOG_TAIL" || true
    else
        docker logs --tail "$LOG_TAIL" "$CONTAINER" 2>&1 || true
    fi
}

v2_logs() {
    if [ -n "$LOG_FILE" ] && [ ! -r "$LOG_FILE" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V2")
        say_fail 2 "server logs" "VERIFY_LOG_FILE не читается: $LOG_FILE"
        return 0
    fi
    if [ -z "$LOG_FILE" ] && [ -z "$LOG_CMD" ]; then
        if ! command -v docker >/dev/null 2>&1; then
            SKIPPED=$((SKIPPED + 1)); say_skip 2 "server logs" "docker недоступен"
            return 0
        fi
        if [ -z "$(docker ps --filter "name=$CONTAINER" -q 2>/dev/null || true)" ]; then
            SKIPPED=$((SKIPPED + 1))
            say_skip 2 "server logs" "контейнер $CONTAINER не запущен"
            return 0
        fi
    fi
    local hits
    hits="$(v2_fetch_logs | grep -cE "$V2_BAD_RE" || true)"
    if [ "$hits" -eq 0 ]; then
        PASSED=$((PASSED + 1))
        say_pass 2 "server logs (tail=$LOG_TAIL)" "0 строк [ERROR]/CRITICAL/Traceback"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V2")
        say_fail 2 "server logs (tail=$LOG_TAIL)" \
            "$hits строк [ERROR]/CRITICAL/Traceback/[FATAL]:"
        v2_fetch_logs | grep -E "$V2_BAD_RE" | head -5 | sed 's/^/    | /' || true
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
    local mode user passwd code headers
    mode="$(env_val CONSOLE_AUTH)"
    # Per-user стор (непустой users.jsonl) отклоняет legacy CONSOLE_PASSWORD —
    # приоритет у bootstrap-админа (как в healthcheck консоли, трасса 030).
    user="$(env_val CONSOLE_ADMIN_USER)"; passwd="$(env_val CONSOLE_ADMIN_PASSWORD)"
    if [ -z "$passwd" ]; then user="verify"; passwd="$(env_val CONSOLE_PASSWORD)"; fi
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
        local code_auth www legacy
        code_auth="$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
            -u "${user}:${passwd}" "$CONSOLE_URL" 2>/dev/null || true)"
        # Переходный режим: если per-user креды не сработали, пробуем legacy-пароль.
        if [ "$code_auth" != "200" ] && [ "$user" != "verify" ]; then
            legacy="$(env_val CONSOLE_PASSWORD)"
            if [ -n "$legacy" ] && [ "$legacy" != "$passwd" ]; then
                code_auth="$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
                    -u "verify:${legacy}" "$CONSOLE_URL" 2>/dev/null || true)"
                [ "$code_auth" = "200" ] && user="verify"
            fi
        fi
        www="$(printf '%s' "$headers" | grep -i '^www-authenticate:' || true)"
        if [ "$code" = "401" ] && [ -n "$www" ] && [ "$code_auth" = "200" ]; then
            PASSED=$((PASSED + 1))
            say_pass 4 "console (auth=on, mode=${mode:-auto}, user=$user)" \
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

# ── V5 console-tls: TLS-фасад kb-console для доступа из локальной сети (трасса 030) ──
# Проверяем: с LAN-адреса фасад отвечает TLS-ом, без кредов 401 + WWW-Authenticate,
# с кредами 200. Всё через --noproxy (иначе корпоративный Squid отвечает 403).
# Нет CONSOLE_LAN_IP → SKIP (фасад не сконфигурирован — это валидная конфигурация).
v5_console_tls() {
    local lan_ip cidr url code code_auth www user passwd
    lan_ip="$(env_val CONSOLE_LAN_IP)"
    cidr="$(env_val CONSOLE_LAN_CIDR)"
    if [ -z "$lan_ip" ]; then
        SKIPPED=$((SKIPPED + 1))
        say_skip 5 "console-tls" "CONSOLE_LAN_IP не задан — TLS-фасад не сконфигурирован"
        return 0
    fi
    url="https://${lan_ip}:8443/"

    # per-user стор может быть активен → приоритет у bootstrap-админа (как в healthcheck)
    user="$(env_val CONSOLE_ADMIN_USER)"; [ -n "$user" ] || user="verify"
    passwd="$(env_val CONSOLE_ADMIN_PASSWORD)"
    [ -n "$passwd" ] || passwd="$(env_val CONSOLE_PASSWORD)"

    code="$(curl -s --noproxy '*' -k -o /dev/null -w '%{http_code}' -m 10 "$url" 2>/dev/null || true)"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V5")
        say_fail 5 "console-tls $url" "нет ответа (фасад не поднят? docker logs kb-console-tls)"
        return 0
    fi
    www="$(curl -s --noproxy '*' -k -D - -o /dev/null -m 10 "$url" 2>/dev/null | grep -i '^www-authenticate:' || true)"

    if [ -z "$passwd" ]; then
        SKIPPED=$((SKIPPED + 1))
        say_skip 5 "console-tls (lan=$lan_ip cidr=${cidr:-—})" "пароль консоли не задан — проверить креды нечем (код $code)"
        return 0
    fi
    code_auth="$(curl -s --noproxy '*' -k -o /dev/null -w '%{http_code}' -m 12 \
        -u "${user}:${passwd}" "$url" 2>/dev/null || true)"
    if [ "$code" = "401" ] && [ -n "$www" ] && [ "$code_auth" = "200" ]; then
        PASSED=$((PASSED + 1))
        say_pass 5 "console-tls (lan=$lan_ip cidr=${cidr:-—})" \
            "TLS ок, без кредов 401+WWW-Authenticate, с кредами 200"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("V5")
        say_fail 5 "console-tls (lan=$lan_ip)" \
            "без кредов=$code (WWW-Authenticate: $([ -n "$www" ] && echo yes || echo no)), с кредами=$code_auth — ожидалось 401+hdr / 200"
    fi
    return 0
}

v1_health
v2_logs
v3_tools
v4_console
v5_console_tls

finish
