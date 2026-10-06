#!/usr/bin/env bash
# ingress_probe.sh — живой read-only probe ingress-контура (Ф2 #4, arch-2026-10-05-ai-workspace).
#
# Назначение: подтвердить ДОСТИЖИМОСТЬЮ/НЕДОСТИЖИМОСТЬЮ то, что декларирует
# статический гейт kb-console/tests/test_ingress_internal_only.py:
#   1) LiteLLM :4000 НЕ достижим с ХОСТА (порт не published — compose.gateway.yml);
#   2) LiteLLM :4000 НЕ достижим из ЧУЖОЙ docker-сети (default bridge): контейнер
#      вне сети mcp-knowledge_default не должен пробиваться к внутренним сервисам;
#   3) ws-redis :6379 НЕ достижим с хоста (порт не published — compose.workspace.yml);
#   4) MCP :8000 с хоста: достижим — это ОЖИДАЕМО (mcp-server в host-сети,
#      известное исключение WS-MCP-HOSTNET, отслеживается .boardData.md §10).
#
# Как читать вывод:
#   PASS — ожидаемая недостижимость подтверждена (вектор закрыт);
#   FAIL — НЕОЖИДАННАЯ достижимость (утечка ingress): разбирать немедленно;
#   WARN — известное отслеживаемое исключение (MCP host-net): exit НЕ валит;
#   INFO — нейтральное наблюдение (например, целевой контейнер не запущен —
#          проверка вырождается в тривиальный PASS, это указывается явно).
# Exit: 0 — утечек нет (WARN не считается); 1 — есть хотя бы один FAIL.
#
# Read-only: ничего не пишет в репо/данные; docker run --rm без volume/сети проекта.
# Air-gap: образы для контейнерных проверок берутся ТОЛЬКО локальные (docker images),
# из интернета ничего не тянется. Переопределение: INGRESS_PROBE_IMAGE=<image>.
set -u

FAILS=0

note_fail() { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }

# ── образ для контейнерных проверок (только локальный, см. шапку) ─────────────
PROBE_IMAGE="${INGRESS_PROBE_IMAGE:-}"
PROBE_MODE=""   # py | wget
if [ -z "$PROBE_IMAGE" ]; then
  if docker image inspect kb-console:prod >/dev/null 2>&1; then
    PROBE_IMAGE=kb-console:prod; PROBE_MODE=py
  elif docker image inspect mcp-knowledge-mcp-server:latest >/dev/null 2>&1; then
    PROBE_IMAGE=mcp-knowledge-mcp-server:latest; PROBE_MODE=py
  elif docker image inspect redis:7-alpine >/dev/null 2>&1; then
    PROBE_IMAGE=redis:7-alpine; PROBE_MODE=wget
  fi
else
  PROBE_MODE=py   # явный образ => рассчитываем на python3 внутри
fi
if [ -z "$PROBE_IMAGE" ]; then
  note_fail "нет локального образа для контейнерных проверок (docker images пуст?)"
  PROBE_IMAGE=""
fi

echo "== ingress probe (Ф2 #4): internal-only; probe-image='${PROBE_IMAGE:-<none>}' mode='${PROBE_MODE}' =="

# ── 1. LiteLLM :4000 с хоста — ожидается НЕДОСТИЖИМ ───────────────────────────
if curl -sS -m 3 -o /dev/null http://127.0.0.1:4000/health/liveliness 2>/dev/null; then
  note_fail "LiteLLM :4000 ДОСТИЖИМ с хоста (http://127.0.0.1:4000) — порт не публикуется (I1/I6)"
  ss -ltnp 2>/dev/null | grep ':4000 ' || true
else
  echo "PASS  LiteLLM :4000 недостижим с хоста (127.0.0.1:4000)"
fi

# ── 2. LiteLLM :4000 из чужой сети (default bridge) — ожидается НЕДОСТИЖИМ ────
if [ -n "$PROBE_IMAGE" ]; then
  LITELLM_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    mcp-knowledge-litellm 2>/dev/null || true)"
  if [ -z "${LITELLM_IP:-}" ]; then
    echo "INFO  litellm-контейнер не запущен/IP недоступен — cross-network проверка тривиально PASS"
    echo "PASS  LiteLLM :4000 недостижим из чужой сети (цель отсутствует)"
  else
    if [ "$PROBE_MODE" = py ]; then
      docker run --rm --network bridge --entrypoint python3 "$PROBE_IMAGE" -c \
        "import socket,sys; s=socket.socket(); s.settimeout(3); rc=s.connect_ex(('$LITELLM_IP',4000)); s.close(); sys.exit(0 if rc==0 else 1)" \
        >/dev/null 2>&1
    else
      docker run --rm --network bridge --entrypoint wget "$PROBE_IMAGE" \
        -q -T 3 -t 1 -O /dev/null "http://$LITELLM_IP:4000/" >/dev/null 2>&1
    fi
    if [ $? -eq 0 ]; then
      note_fail "LiteLLM $LITELLM_IP:4000 ДОСТИЖИМ из чужой сети (default bridge) — межсетевая изоляция нарушена"
    else
      echo "PASS  LiteLLM :4000 ($LITELLM_IP) недостижим из чужой сети (default bridge)"
    fi
  fi
fi

# ── 3. ws-redis :6379 с хоста — ожидается НЕДОСТИЖИМ ──────────────────────────
# TCP-проба (/dev/tcp): redis не говорит по HTTP, curl дал бы ложный отказ.
if timeout 3 bash -c '</dev/tcp/127.0.0.1/6379' 2>/dev/null; then
  note_fail "ws-redis :6379 ДОСТИЖИМ с хоста (127.0.0.1:6379) — порт не публикуется (I6); слушает НЕ наш контейнер?"
  ss -ltnp 2>/dev/null | grep ':6379 ' || true
else
  echo "PASS  ws-redis :6379 недостижим с хоста (127.0.0.1:6379)"
fi
if ! docker inspect -f '{{.State.Running}}' mcp-knowledge-ws-redis 2>/dev/null | grep -q true; then
  echo "INFO  контейнер mcp-knowledge-ws-redis сейчас не запущен — проверка 3 вырождена (порт свободен)"
fi

# ── 4. MCP :8000 с хоста — достижимость = WARN (известное исключение) ─────────
if curl -sS -m 3 -o /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
  echo "WARN  MCP :8000 достижим с хоста (0.0.0.0:8000) — ИЗВЕСТНО: host-сеть mcp-server (WS-MCP-HOSTNET, tracked §10); exit не валится"
else
  echo "INFO  MCP :8000 сейчас недостижим с хоста (сервис остановлен?) — для ingress-гейта нейтрально (host-net см. §10)"
fi

echo "== итог: FAILS=$FAILS (WARN/INFO не считаются) =="
[ "$FAILS" -gt 0 ] && exit 1
exit 0
