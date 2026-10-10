#!/usr/bin/env bash
# prod-diag.sh — диагностический прогон прод-стека с ЗАПИСЬЮ ЛОГА для агента.
#
# ЗАЧЕМ: полная проверка «что работает» на узле (aikb) без чтения секретов моделью.
# Запускается root-ом (скрипт сам source-ит .env), пишет лог 0644 →
# агент читает: `cat /var/log/mcp-knowledge/diag/prod-diag-latest.log`.
#
# Проверки (exit = число FAIL):
#   D1  services   docker compose ps — все сервисы running/healthy
#   D2  health     GET /health — status=healthy, reconcile без error, embedding.ok
#   D3  metrics    GET /metrics — 200 + ключевые счётчики
#   D4  mcp-tools  POST /mcp tools/list — ≥ VERIFY_MIN_TOOLS (read-ключ из .env)
#   D5  console    :8085 auth-aware (без кредов 302→/login, с кредами 200)
#   D6  routes     14 UI-роутов консоли (authed) — каждый 200
#   D7  calibration маркер страницы /calibration (фикс F6-D-1: «Калибровка»)
#   D8  console-tls :8443 LAN-фасад (TLS + auth-aware)
#   D9  converter  sidecar kb-converter /health
#   D10 router     LiteLLM :4000/health (Bearer из .env; отсутствие → WARN)
#   D11 data       documents bind-mount + qdrant reachable
#   D12 zone-gate  I5 (private→local OFF) — поведенческая проба (Playwright S4) → WARN/INFO
#   D13 calib-api  :8700 — присутствие/отсутствие (WARN если нет, fail-soft)
#   D14 git        HEAD + чистота дерева
#   D15 images     digests kb-console / mcp-server
#
# Env: DIAG_LOG_DIR (дефолт /var/log/mcp-knowledge/diag) · DIAG_NO_LOG=1
#      SERVER_HEALTH_URL/MCP_URL/CONSOLE_URL/CONVERTER_HEALTH_URL · VERIFY_MIN_TOOLS
#      DIAG_ROUTES (переопределить список) · --help
# Выход: exit = число упавших (0 = зелёно). Секреты не печатаются.

set -uo pipefail

ROOT="${DIAG_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${DIAG_ENV_FILE:-$ROOT/.env}"
SERVER_HEALTH_URL="${SERVER_HEALTH_URL:-http://localhost:8000/health}"
MCP_URL="${MCP_URL:-http://localhost:8000/mcp}"
CONSOLE_URL="${CONSOLE_URL:-http://localhost:8085/}"
CONVERTER_HEALTH_URL="${CONVERTER_HEALTH_URL:-http://localhost:8660/health}"
ROUTER_HEALTH_URL="${ROUTER_HEALTH_URL:-http://localhost:4000/health}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333/readyz}"
CALIB_ADMIN_URL="${CALIB_ADMIN_URL:-http://localhost:8700/}"
MIN_TOOLS="${VERIFY_MIN_TOOLS:-30}"
DIAG_LOG_DIR="${DIAG_LOG_DIR:-/var/log/mcp-knowledge/diag}"
DIAG_NO_LOG="${DIAG_NO_LOG:-0}"
ROUTES="${DIAG_ROUTES:-/ /books /calibration /chat /documents /import /quality /queue /quotas /requests /search /status /tokens /users}"

PASSED=0; FAILED=0; WARNED=0; SKIPPED=0; FAILED_IDS=(); WARN_IDS=()

if [ -t 1 ]; then C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_C=$'\033[36m'; C_B=$'\033[1m'; C_0=$'\033[0m'; else C_G=""; C_R=""; C_Y=""; C_C=""; C_B=""; C_0=""; fi

# ── лог ────────────────────────────────────────────────────────────────
LOG=""
setup_log() {
    [ "$DIAG_NO_LOG" = "1" ] && return 0
    local dir="$DIAG_LOG_DIR"
    mkdir -p "$dir" 2>/dev/null || dir=""
    if [ -z "$dir" ] || [ ! -w "$dir" ]; then
        dir="$ROOT/.trash/diag"; mkdir -p "$dir" 2>/dev/null || { LOG=""; return 0; }
        echo "⚠ DIAG_LOG_DIR недоступен на запись → лог в $dir" >&2
    fi
    chmod 0755 "$dir" 2>/dev/null || true
    LOG="$dir/prod-diag-$(date -u +%Y%m%dT%H%M%SZ).log"
    : > "$LOG" 2>/dev/null || { LOG=""; return 0; }
    chmod 0644 "$LOG" 2>/dev/null || true
}
# emit: строка в stdout И в лог
emit() { printf '%s\n' "$*"; [ -n "$LOG" ] && printf '%s\n' "$*" >> "$LOG" 2>/dev/null; return 0; }
say_pass() { PASSED=$((PASSED+1)); echo "${C_G}[PASS]${C_0} $1 $2 · $3"; [ -n "$LOG" ] && printf '[PASS] %s %s · %s\n' "$1" "$2" "$3" >> "$LOG"; return 0; }
say_fail() { FAILED=$((FAILED+1)); FAILED_IDS+=("$1"); echo "${C_R}[FAIL]${C_0} $1 $2 · $3"; [ -n "$LOG" ] && printf '[FAIL] %s %s · %s\n' "$1" "$2" "$3" >> "$LOG"; return 0; }
say_warn() { WARNED=$((WARNED+1)); WARN_IDS+=("$1"); echo "${C_Y}[WARN]${C_0} $1 $2 · $3"; [ -n "$LOG" ] && printf '[WARN] %s %s · %s\n' "$1" "$2" "$3" >> "$LOG"; return 0; }
say_skip() { SKIPPED=$((SKIPPED+1)); echo "${C_Y}[SKIP]${C_0} $1 $2 · $3"; [ -n "$LOG" ] && printf '[SKIP] %s %s · %s\n' "$1" "$2" "$3" >> "$LOG"; return 0; }
note() { echo "    $*"; [ -n "$LOG" ] && printf '    %s\n' "$*" >> "$LOG"; return 0; }

env_val() { [ -f "$ENV_FILE" ] || return 0; awk -v kv="$1=" 'index($0,kv)==1{print substr($0,length(kv)+1);exit}' "$ENV_FILE"; }

PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
CURL="curl -s --noproxy *"

help() { sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }
for a in "$@"; do case "$a" in --help|-h) help;; *) echo "Неизвестный флаг: $a" >&2; exit 64;; esac; done

# ── helpers ────────────────────────────────────────────────────────────
code() { curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 10 "$@" 2>/dev/null || true; }
code_auth() { local u="$1" p="$2" url="$3"; curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 12 -u "$u:$p" "$url" 2>/dev/null || true; }
# консольные креды (приоритет bootstrap-админа, как в verify-deploy)
console_creds() {
    local u p; u="$(env_val CONSOLE_ADMIN_USER)"; p="$(env_val CONSOLE_ADMIN_PASSWORD)"
    [ -n "$p" ] || { u="verify"; p="$(env_val CONSOLE_PASSWORD)"; }
    printf '%s|%s' "${u:-admin}" "$p"
}
detect_compose() {
    if [ -n "${COMPOSE_FILE:-}" ] && [ -f "$ROOT/${COMPOSE_FILE##*/}" ]; then echo "$ROOT/${COMPOSE_FILE##*/}"; return; fi
    for f in docker-compose.prod.yml docker-compose.yml; do [ -f "$ROOT/$f" ] && { echo "$ROOT/$f"; return; }; done
    echo ""
}

# ── checks ─────────────────────────────────────────────────────────────
d1_services() {
    if ! command -v docker >/dev/null 2>&1; then say_skip D1 services "docker недоступен"; return; fi
    local cf; cf="$(detect_compose)"; [ -n "$cf" ] || cf="$ROOT/docker-compose.prod.yml"
    local out; out="$(docker compose -f "$cf" ps --format '{{.Service}}|{{.State}}|{{.Health}}' 2>/dev/null || true)"
    if [ -z "$out" ]; then say_warn D1 services "compose ps пусто (файл ${cf##*/}?)"; return; fi
    local bad=0 total=0 svc st he
    while IFS='|' read -r svc st he; do
        [ -z "$svc" ] && continue; total=$((total+1))
        if [ "$st" != "running" ] || { [ -n "$he" ] && [ "$he" != "healthy" ]; }; then bad=$((bad+1)); note "✖ $svc state=$st health=${he:-—}"; fi
    done <<<"$out"
    if [ "$bad" -eq 0 ]; then say_pass D1 services "$total сервисов running/healthy"; else say_fail D1 services "$bad из $total не healthy (см. строки выше)"; fi
}

d2_health() {
    local body; body="$(curl -sf -m 10 "$SERVER_HEALTH_URL" 2>/dev/null || true)"
    [ -n "$body" ] || { say_fail D2 health "нет ответа $SERVER_HEALTH_URL"; return; }
    [ -n "$PY" ] || { say_fail D2 health "python недоступен"; return; }
    local s; s="$(printf '%s' "$body" | "$PY" -c '
import json,sys
d=json.load(sys.stdin); rec=d.get("reconcile") or {}; emb=(d.get("checks") or {}).get("embedding") or {}
err=rec.get("error"); print(d.get("status","?"),rec.get("state","?"),"none" if err in (None,"") else "ERROR","ok" if emb.get("ok") else "FAIL",emb.get("dim","?"),sep="|")' 2>/dev/null)"
    [ -n "$s" ] || { say_fail D2 health "JSON не разбирается"; return; }
    local st state err emb dim; IFS='|' read -r st state err emb dim <<<"$s"
    note "status=$st reconcile.state=$state orphans_n/a embedding.ok=$emb dim=$dim"
    if [ "$st" = healthy ] && [ "$err" = none ] && [ "$emb" = ok ]; then say_pass D2 health "status=healthy, reconcile без error, embedding ok(dim=$dim)"; else say_fail D2 health "status=$st err=$err embedding=$emb"; fi
}

d3_metrics() {
    local body; body="$(curl -sf -m 10 "http://localhost:8000/metrics" 2>/dev/null || true)"
    if [ -z "$body" ]; then say_warn D3 metrics "нет ответа /metrics"; return; fi
    local n; n="$(printf '%s' "$body" | grep -cE '^[a-zA-Z_]' || true)"
    note "метрик-строк: $n"
    say_pass D3 metrics "200, строк метрик=$n"
}

d4_tools() {
    local key; key="$(env_val MCP_READ_KEYS)"; key="${key%%,*}"; [ -n "$key" ] || key="$(env_val MCP_API_KEY)"
    [ -n "$key" ] || { say_skip D4 mcp-tools "нет MCP_READ_KEYS/MCP_API_KEY в .env"; return; }
    local n; n="$(curl -s -m 30 -X POST "$MCP_URL" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "X-API-Key: $key" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null | "$PY" -c '
import json,sys
raw=sys.stdin.read()
if raw.lstrip().startswith(("event:","data:")): raw="".join(l[5:] for l in raw.splitlines() if l.startswith("data:"))
print(len(json.loads(raw)["result"]["tools"]))' 2>/dev/null)"
    if [ -z "$n" ]; then say_fail D4 mcp-tools "запрос/разбор не удался (ключ не выводится)"; return; fi
    if [ "$n" -ge "$MIN_TOOLS" ]; then say_pass D4 mcp-tools "$n инструментов (порог $MIN_TOOLS)"; else say_fail D4 mcp-tools "$n < порога $MIN_TOOLS"; fi
}

d5_console_auth() {
    local creds u p; creds="$(console_creds)"; u="${creds%%|*}"; p="${creds#*|}"
    local c0; c0="$(code "$CONSOLE_URL")"
    [ "$c0" = 000 ] || [ -z "$c0" ] && { say_fail D5 console "недоступна $CONSOLE_URL"; return; }
    local loc; loc="$(curl -s --noproxy '*' -D - -o /dev/null -m 10 "$CONSOLE_URL" 2>/dev/null | grep -i '^location:' | grep -i '/login' || true)"
    if [ -z "$p" ]; then say_warn D5 console "auth — пароль не задан (unauthed=$c0)"; return; fi
    local ca; ca="$(code_auth "$u" "$p" "$CONSOLE_URL")"
    if [ "$c0" = 302 ] && [ -n "$loc" ] && [ "$ca" = 200 ]; then say_pass D5 console "auth=on user=$u · без кредов 302→/login, с кредами 200"
    elif [ "$c0" = 200 ]; then say_pass D5 console "auth=off · 200 без кредов"
    else say_fail D5 console "unauthed=$c0 (login-Location:$([ -n "$loc" ] && echo yes||echo no)) authed=$ca — ожидалось 302+/login / 200"; fi
}

d6_routes() {
    local creds u p; creds="$(console_creds)"; u="${creds%%|*}"; p="${creds#*|}"
    [ -n "$p" ] || { say_skip D6 routes "нет кредов консоли"; return; }
    local base="${CONSOLE_URL%/}" bad=0 r c
    for r in $ROUTES; do c="$(code_auth "$u" "$p" "$base$r")"; [ "$c" = 200 ] || { bad=$((bad+1)); note "✖ $r → $c"; }; done
    local total; total="$(printf '%s\n' $ROUTES | grep -c . || true)"
    if [ "$bad" -eq 0 ]; then say_pass D6 routes "все $total роутов → 200"; else say_fail D6 routes "$bad из $total роутов не 200 (см. строки выше)"; fi
}

d7_calibration() {
    local creds u p; creds="$(console_creds)"; u="${creds%%|*}"; p="${creds#*|}"
    [ -n "$p" ] || { say_skip D7 calibration "нет кредов консоли"; return; }
    local body; body="$(curl -s --noproxy '*' -m 10 -u "$u:$p" "${CONSOLE_URL%/}/calibration" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -qiE 'калибровк|calibration'; then say_pass D7 calibration "страница /calibration содержит маркер (фикс F6-D-1 live)"
    else say_fail D7 calibration "маркер «Калибровка» не найден на /calibration"; fi
}

d8_console_tls() {
    local ip; ip="$(env_val CONSOLE_LAN_IP)"; [ -n "$ip" ] || { say_skip D8 console-tls "CONSOLE_LAN_IP не задан"; return; }
    local url="https://${ip}:8443/"; local c0; c0="$(curl -s --noproxy '*' -k -o /dev/null -w '%{http_code}' -m 10 "$url" 2>/dev/null || true)"
    [ "$c0" = 000 ] || [ -z "$c0" ] && { say_fail D8 console-tls "нет ответа $url"; return; }
    local creds u p; creds="$(console_creds)"; u="${creds%%|*}"; p="${creds#*|}"
    local ca="—"; [ -n "$p" ] && ca="$(curl -s --noproxy '*' -k -o /dev/null -w '%{http_code}' -m 12 -u "$u:$p" "$url" 2>/dev/null || true)"
    if [ "$c0" = 302 ] && [ "$ca" = 200 ]; then say_pass D8 console-tls "TLS ок (lan=$ip), 302→/login, authed 200"; else say_warn D8 console-tls "unauthed=$c0 authed=$ca (lan=$ip)"; fi
}

d9_converter() {
    local body; body="$(curl -sf -m 10 "$CONVERTER_HEALTH_URL" 2>/dev/null || true)"
    if [ -z "$body" ]; then say_fail D9 converter "нет ответа $CONVERTER_HEALTH_URL (sidecar не поднят?)"; return; fi
    local st; st="$(printf '%s' "$body" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("status","?"))' 2>/dev/null)"
    [ -n "$st" ] && say_pass D9 converter "status=$st" || say_warn D9 converter "невалидный JSON: $(printf '%s' "$body" | head -c 80)"
}

d10_router() {
    local key; key="$(env_val LITELLM_MASTER_KEY)"
    local c; c="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 8 -H "Authorization: Bearer $key" "$ROUTER_HEALTH_URL" 2>/dev/null || true)"
    if [ "$c" = 000 ] || [ -z "$c" ]; then say_warn D10 router "LiteLLM :4000 недоступен (не в этом стеке?)"; elif [ "$c" = 200 ]; then say_pass D10 router "LiteLLM /health 200"; else say_warn D10 router "LiteLLM /health=$c"; fi
}

d11_data() {
    # documents bind-mount (runtime)
    if command -v docker >/dev/null 2>&1 && [ -n "$(docker ps --filter 'name=mcp-knowledge-server' -q 2>/dev/null || true)" ]; then
        local mt; mt="$(docker inspect mcp-knowledge-server --format '{{range .Mounts}}{{if eq .Destination "/app/data/documents"}}{{.Type}}{{end}}{{end}}' 2>/dev/null || true)"
        [ "$mt" = bind ] && say_pass D11 data "documents bind-mount активен" || say_fail D11 data "documents mount Type=${mt:-—}"
    else say_skip D11 data "нет контейнера mcp-knowledge-server"; fi
    # qdrant
    local q; q="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 8 "$QDRANT_URL" 2>/dev/null || true)"
    [ "$q" = 200 ] && say_pass D12 qdrant "readyz 200" || say_warn D12 qdrant "readyz=$q ($QDRANT_URL)"
}

d13_calib_api() {
    local c; c="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 6 "$CALIB_ADMIN_URL" 2>/dev/null || true)"
    if [ -z "$c" ] || [ "$c" = 000 ]; then say_warn D13 calib-api ":8700 не слушает (страница /calibration деградирует, fail-soft)"; else say_pass D13 calib-api ":8700 слушает (код $c)"; fi
}

d14_git() {
    local h; h="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
    local dirty; dirty="$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    note "HEAD=$h dirty_files=$dirty"
    [ "$dirty" = 0 ] && say_pass D14 git "HEAD=$h, дерево чисто" || say_warn D14 git "HEAD=$h, изменённых файлов=$dirty"
}

d15_images() {
    command -v docker >/dev/null 2>&1 || { say_skip D15 images "docker недоступен"; return; }
    local a b; a="$(docker image inspect -f '{{.Id}}' kb-console:prod 2>/dev/null | head -c 19 || true)"; b="$(docker image inspect -f '{{.Id}}' mcp-knowledge-mcp-server:latest 2>/dev/null | head -c 19 || true)"
    note "kb-console:prod=${a:-—}  mcp-server:latest=${b:-—}"
    [ -n "$a$b" ] && say_pass D15 images "digests прочитаны" || say_skip D15 images "образы не найдены"
}

d16_zone_gate() {
    say_warn D16 zone-gate "I5 (private→local OFF) — поведенческая проба вне HTTP (Playwright S4); здесь не проверяется"
}

# ── системный контур (железо/рантайм, важный для программы) ───────────
d20_host() {
    note "uname: $(uname -sr)  arch=$(uname -m)  host=$(hostname)"
    note "uptime: $(uptime -p 2>/dev/null || uptime 2>/dev/null)"
    note "cpu: nproc=$(nproc 2>/dev/null || echo '?')  load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
    local mem; mem="$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.1fGiB total / %.1fGiB avail", t/1048576, a/1048576}' /proc/meminfo 2>/dev/null)"
    note "ram: $mem"
    say_pass D20 host "host/cpu/ram/uptime собраны"
}

d21_disk() {
    local paths="$ROOT"; [ -n "${DATA_ROOT:-}" ] && paths="$paths $DATA_ROOT"; [ -d /var/lib/docker ] && paths="$paths /var/lib/docker"
    local line; while IFS= read -r line; do [ -n "$line" ] && note "$line"; done < <(df -h $paths 2>/dev/null | tail -n +2)
    local high; high="$(df -P $paths 2>/dev/null | tail -n +2 | awk '{gsub("%","",$5); if($5+0>=85) print $6" ("$5"%)"}' | tr '\n' ' ')"
    if [ -n "$high" ]; then say_warn D21 disk "занято ≥85%: $high"; else say_pass D21 disk "ключевые ФС ниже 85%"; fi
}

d22_gpu() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        [ "${DIAG_EXPECT_GPU:-0}" = "1" ] && say_fail D22 gpu "nvidia-smi отсутствует, а GPU ожидается (DIAG_EXPECT_GPU=1)" || say_warn D22 gpu "nvidia-smi отсутствует (нет GPU/драйвера)"
        return
    fi
    local q; q="$(nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null || true)"
    [ -n "$q" ] || { say_fail D22 gpu "nvidia-smi не вернул данные"; return; }
    note "gpu: $q"
    local freemb; freemb="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)"
    if [ -n "$freemb" ] && [ "$freemb" -lt 1500 ]; then say_warn D22 gpu "свободно VRAM ${freemb} MiB (<1.5G) — риск OOM для LLM/эмбеддера"; else say_pass D22 gpu "GPU ок (free=${freemb:-?}MiB)"; fi
}

d23_gpu_stack() {
    # torch обычно НЕ в сервер-контейнере (эмбеддер → Ollama). Проверяем:
    # (a) torch на хосте (.venv), (b) кто реально на GPU (compute-процессы).
    local hostpy="$ROOT/.venv/bin/python"
    if [ -x "$hostpy" ]; then
        local t; t="$("$hostpy" -c 'import torch;print("torch",torch.__version__,"cuda",torch.cuda.is_available())' 2>/dev/null || true)"
        [ -n "$t" ] && note "host .venv: $t"
    fi
    command -v nvidia-smi >/dev/null 2>&1 || { say_skip D23 gpu-stack "нет nvidia-smi"; return; }
    local apps; apps="$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null || true)"
    if [ -n "$apps" ]; then note "GPU-процессы:"; while IFS= read -r l; do [ -n "$l" ] && note "  $l"; done <<<"$apps"; say_pass D23 gpu-stack "GPU используется ($(printf '%s\n' "$apps" | grep -c . ) проц.)"; else say_warn D23 gpu-stack "на GPU нет активных процессов (модели не загружены / GPU-idle)"; fi
}

d24_ollama() {
    local tags; tags="$(curl -s --noproxy '*' -m 6 http://localhost:11434/api/tags 2>/dev/null || true)"
    if [ -z "$tags" ]; then say_warn D24 ollama ":11434 недоступен (Ollama не в этом стеке?)"; return; fi
    local names n; names="$(printf '%s' "$tags" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(", ".join(m.get("name","?") for m in d.get("models",[])))' 2>/dev/null || true)"
    n="$(printf '%s' "$tags" | "$PY" -c 'import json,sys;print(len(json.load(sys.stdin).get("models",[])))' 2>/dev/null || echo '?')"
    note "ollama models: ${names:-—}"
    say_pass D24 ollama "Ollama жив (моделей: $n)"
}

d25_docker() {
    command -v docker >/dev/null 2>&1 || { say_skip D25 docker "docker недоступен"; return; }
    note "docker: $(docker version --format 'server={{.Server.Version}} api={{.Server.APIVersion}}' 2>/dev/null || true)"
    local df1; df1="$(docker system df 2>/dev/null | sed -n '2,4p' | tr -s ' ')"
    [ -n "$df1" ] && note "docker df: $df1"
    say_pass D25 docker "docker info/df собраны"
}

d26_units() {
    command -v systemctl >/dev/null 2>&1 || { say_skip D26 units "systemctl нет"; return; }
    local u; u="$(systemctl list-units --type=service --no-legend 2>/dev/null | grep -iE 'mcp-knowledge|kb-console|ollama|qdrant' | awk '{print $1"="$3}' | tr '\n' ' ')"
    if [ -n "$u" ]; then note "$u"; say_pass D26 units "systemd-юниты: найдены"; else say_warn D26 units "юниты mcp/kb/ollama/qdrant не найдены (docker-only?)"; fi
}

# ── D17: tool-loop smoke headless (F6-D-2) — реальный LLM+MCP внутри контейнера ──
d17_toolloop() {
    command -v docker >/dev/null 2>&1 || { say_skip D17 tool-loop "docker недоступен"; return; }
    local cn="${DIAG_CONSOLE_CONTAINER:-kb-console}"
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$cn"; then
        cn="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'kb-console$' | head -1)"
    fi
    [ -n "$cn" ] || { say_skip D17 tool-loop "контейнер kb-console не найден"; return; }
    local out
    out="$(timeout "${DIAG_TURN_TIMEOUT:-200}" docker exec -i "$cn" python - <<'PYEOF' 2>/dev/null | tail -1
import asyncio, json, os, traceback
diag = {"ws_llm_set": bool(os.environ.get("WS_LLM_URL")), "ws_mcp_set": bool(os.environ.get("WS_MCP_URL")), "ws_mcp_key_set": bool(os.environ.get("WS_MCP_KEY"))}
import kb_console.core.tool_loop as tl
calls = []
_orig = tl._execute_tool_call
async def _wrap(mcp, call, zone):
    try:
        calls.append(((call or {}).get("function") or {}).get("name") or (call or {}).get("name"))
    except Exception:
        calls.append("?")
    return await _orig(mcp, call, zone)
tl._execute_tool_call = _wrap
from kb_console.core.chat_turn import chat_turn
msgs = [{"role": "user", "content": "Найди в базе знаний через инструмент search_knowledge материалы про протокол MCP и перечисли названия."}]
try:
    res = asyncio.run(chat_turn(msgs, session_id="prod-diag", zone="private", max_iters=4))
    diag.update(tool_calls=[c for c in calls if c], text_len=len((res or {}).get("text", "")), ok=any(calls))
except Exception as e:
    diag.update(error=type(e).__name__, msg=str(e)[:200], ok=False, trace=traceback.format_exc()[-600:])
print(json.dumps(diag, ensure_ascii=False))
PYEOF
)"
    if [ -z "$out" ]; then say_warn D17 tool-loop "нет ответа от контейнера $cn (LLM/MCP недоступны?)"; return; fi
    local ok calls tl_; ok="$(printf '%s' "$out" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("ok"))' 2>/dev/null)"
    calls="$(printf '%s' "$out" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(",".join(d.get("tool_calls",[])) or "-")' 2>/dev/null)"
    tl_="$(printf '%s' "$out" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("text_len", d.get("error","?")))' 2>/dev/null)"
    if [ "$ok" = "True" ]; then say_pass D17 tool-loop "tool-loop сработал: инструмент($calls) вызван, ответ ~${tl_} симв."
    else say_fail D17 tool-loop "инструмент НЕ вызван"; printf '%s\n' "$out" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print("  WS_LLM_set=%s WS_MCP_set=%s"% (d.get("ws_llm_set"),d.get("ws_mcp_set")));print("  trace:",d.get("trace",d.get("msg",""))[-500:])' 2>/dev/null | sed "s/^/    /" || true; fi
}

setup_log
emit "${C_B}═══ mcp-knowledge prod-diag · $(date -Iseconds) · host=$(hostname) ═══${C_0}"
emit "log=$LOG  root=$ROOT  compose=$(detect_compose)"
echo ""
emit "${C_B}── системный контур ──${C_0}"; d20_host; d21_disk; d22_gpu; d23_gpu_stack; d24_ollama; d25_docker; d26_units; emit "${C_B}── прикладной контур ──${C_0}"; d1_services; d2_health; d3_metrics; d4_tools; d5_console_auth; d6_routes; d7_calibration; d8_console_tls; d9_converter; d10_router; d11_data; d13_calib_api; d14_git; d15_images; d16_zone_gate; d17_toolloop
emit ""
emit "${C_B}═══ ИТОГ prod-diag: ${C_G}${PASSED} passed${C_0} / ${C_R}${FAILED} failed${C_0} (${FAILED_IDS[*]:-}) / ${C_Y}${WARNED} warn${C_0} (${WARN_IDS[*]:-}) / ${SKIPPED} skipped ═══${C_0}"
if [ -n "$LOG" ]; then printf 'ИТОГ: %s passed / %s failed / %s warn / %s skipped\n' "$PASSED" "$FAILED" "$WARNED" "$SKIPPED" >> "$LOG"
    cp -f "$LOG" "$DIAG_LOG_DIR/prod-diag-latest.log" 2>/dev/null || cp -f "$LOG" "$ROOT/.trash/diag/prod-diag-latest.log" 2>/dev/null || true
    chmod 0644 "$DIAG_LOG_DIR/prod-diag-latest.log" 2>/dev/null || true
    echo "лог: $LOG  (+prod-diag-latest.log)"; fi
exit "$FAILED"
