#!/usr/bin/env python3
"""errors_collect.py — host-коллектор цикла Error→Rule (code-2026-09-22-003, Ф1).

Канон: .knowledge/docs/observability/self-improvement-loop.md (E1–E7); спека
.boardData.md §7 «error-rule-self-improvement» (вариант В2).

Принципы (ЗАМОРОЖЕНЫ спекой):
  • capture-first: sink пишет ВСЁ (маркеры/WARN/ERROR/CRITICAL/tracebacks/4xx-5xx
    access/lifecycle/пороги/health) — фильтрация только на показе; 2xx/3xx access
    НЕ пишутся (иначе healthcheck-пробы захлебнут sink).
  • сигнатура E2 считается ПРИ ЗАПИСИ: source + "|" + (marker || error_code)
    + "|" + deep_normalize(маскированное message).
  • приоритет E3 — агрегатный (в aggregates/signatures.json): P0 = hint ∈
    {traceback, critical, 5xx, oom, restart, cron_nonzero, health_degraded,
    hang, disk_critical} (независимо от числа; expected=true плановые restart
    НЕ дают P0 — P2-8); P1 = ≥2 акторов ИЛИ рост неделя-к-неделе; P3 = baseline
    (401/403/404/429 без актора, health-снапшоты, [AUTH]-класс); P2 = остальное.
  • маскирование секретов НА ЗАПИСИ (E7); атомарность state-файлов tmp+os.replace
    (P2-3); graceful-деградация: недоступный источник = skip + stderr, exit 0
    (P2-5); дедуп host-порогов: событие при ПЕРЕСЕЧЕНИИ + снапшот ≤1/час (P2-4).

Sink ($DATA_ROOT/logs/errors/, вне клона и вне контейнеров — M1):
  events/raw/YYYY-MM-DD.jsonl   raw-события (TTL 90d, prune)
  aggregates/signatures.json    долгоживущие сигнатуры (живут ДОЛЬШЕ raw)
  collector_state.json          last_ts/offset/breached-состояния (дедуп-окна)

Запуск: cron */5 (deploy.yml/errors.yml, user root, лог → /var/log/mcp-errors-collect.log).
Собственные ошибки — в stderr (НЕ в sink — анти-рекурсия); собственный лог
ротируется (последние 2000 строк при >10 МБ — P2-6). Python ≥3.9, ТОЛЬКО stdlib.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Явный PATH для cron Debian (docker/nvidia-smi живут в /usr/bin; «явность защищает»).
os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
# DATA_ROOT — тот же контракт, что backup.sh:14 (env-override, дефолт = dev-layout).
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(PROJECT_DIR / "data")))

# ── Схема/нормализация (E2, заморожено; тесты в tests/test_errors_lib.py) ──

MARKER_RE = re.compile(r"\[([A-Z][A-Z_]+)\]")
LEVEL_BRACKET_RE = re.compile(r"\[(INFO|DEBUG|WARNING|ERROR|CRITICAL)\]")
LEVEL_PREFIX_RE = re.compile(r"^(INFO|DEBUG|WARNING|ERROR|CRITICAL):")
ACCESS_RE = re.compile(r'"(\S+) (\S+) HTTP/[\d.]+" (\d{3})')
KEY_HASH_RE = re.compile(r"\bkey=([0-9a-f]{4,64})\b")
CRON_LINE_RE = re.compile(r"\[CRON\] job=(\S+) exit=(\d+) dur=(\S+) ts=(\S+)")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HEX16_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
PATH_RE = re.compile(r"(?<![\w./-])/(?:[\w.-]+/)*[\w.-]+")
NUM_RE = re.compile(r"\b\d+\b")
WS_RE = re.compile(r"\s+")

# P0-признаки (E3-словарь, заморожен; см. docstring модуля)
P0_HINTS = frozenset(
    {"traceback", "critical", "5xx", "oom", "restart", "cron_nonzero",
     "health_degraded", "hang", "disk_critical"}
)
# baseline P3: 4xx без актора (словарь 4xx заморожен каноном §4)
BASELINE_4XX = frozenset({401, 403, 404, 429})
DAILY_KEEP_DAYS = 21  # окно расчёта роста неделя-к-неделе + запас
ENDPOINTS_KEEP = 20  # 009: cap ключей endpoints в агрегате (хвост → __others__)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw: str) -> datetime:
    """RFC3339 (docker даёт наносекунды — обрезаем до микро) → aware datetime UTC."""
    raw = raw.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$", raw)
    if not m:
        raise ValueError(f"bad ts: {raw!r}")
    frac = (m.group(2) or "")[:6]
    iso = m.group(1) + ("." + frac if frac else "")
    off = m.group(3)
    if not off or off == "Z":
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    else:
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        dt = (datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
              - sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0)))
    return dt.astimezone(timezone.utc)


def mask_secrets(text: str) -> str:
    """Маскирование НА ЗАПИСИ (E7/P2-10): ключи/hex/Authorization/password/token/Bearer."""
    text = re.sub(r"\bmcp_[a-z]{1,3}_[A-Za-z0-9]+\b", "<secret>", text)
    text = re.sub(r"\b[0-9a-fA-F]{32,}\b", "<secret>", text)
    text = re.sub(r"(?i)Authorization:\s*.*", "Authorization: <secret>", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <secret>", text)
    text = re.sub(r"(?i)\b(password|token|api[_-]?key|secret)\s*[=:]\s*\S+", r"\1=<secret>", text)
    return text


def deep_normalize(text: str) -> str:
    """Нормализация сигнатуры (E2, порядок фиксирован): key-hash → uuid → hex≥16
    → пути → числа → пробелы. key=<hex> схлопывается ПЕРВЫМ: актор не входит в
    сигнатуру (иначе разные акторы = разные сигнатуры = P1 «≥2 акторов» не
    сработает; канон §9: нормализация идентификаторов дала 171→47 сигнатур)."""
    text = re.sub(r"\bkey=[0-9a-f]{4,64}\b", "key=<key>", text, flags=re.IGNORECASE)
    text = UUID_RE.sub("<uuid>", text)
    text = HEX16_RE.sub("<hex>", text)
    text = PATH_RE.sub("<path>", text)
    text = NUM_RE.sub("<n>", text)
    return WS_RE.sub(" ", text).strip()


def make_signature(source: str, marker, error_code, message: str) -> str:
    key = marker or error_code or "-"
    return f"{source}|{key}|{deep_normalize(message)}"


def extract_endpoint(message: str):
    """endpoint (009 §7.1): route-шаблон request-target access-строки.

    Вычисляется ТОЛЬКО при матче ACCESS_RE (кавычечный uvicorn-формат
    `"METHOD target HTTP/x.x" NNN` — access-логи mcp-server); [REQ]-строки
    kb-console (без статуса/кавычек) не матчатся → None всегда (зона
    покрытия P1-2, расширение ACCESS_RE — вне трассы 009). Вход — УЖЕ
    замаскированная строка (порядок mask→trunc→extract, §7.3-1): секреты
    конструктивно не могут попасть в поле. Query отброшен (до ?);
    абсолютный URL → отброс scheme/host/port, только path; id-сегменты
    (pure-digits / UUID / hex≥16) → <id>; корень / → /; обрезка 200."""
    m = ACCESS_RE.search(message)
    if not m:
        return None
    target = m.group(2).split("?", 1)[0]  # query отброшен (до ?)
    i = target.find("://")  # абсолютный URL → только path
    if i != -1:
        rest = target[i + 3:]
        target = rest[rest.find("/"):] if "/" in rest else "/"
    segs = [s for s in target.split("/") if s]
    norm = ["<id>" if s.isdigit() or UUID_RE.fullmatch(s) or HEX16_RE.fullmatch(s)
            else s for s in segs]
    return ("/" + "/".join(norm))[:200] if segs else "/"


def classify_actor(message: str, source: str) -> str:
    """actor_id v1 (OQ-3): key_hash из [MCP]-строк; cron:<job>; host; console:<user>."""
    m = KEY_HASH_RE.search(message)
    if m:
        return m.group(1)
    m = re.search(r"\buser=(\S+)", message)
    if m:
        return f"console:{m.group(1)}"
    if source == "cron_log":
        m = re.search(r"\[CRON\] job=(\S+)", message)
        if m:
            return f"cron:{m.group(1)}"
    return None


# ── Sink / state (атомарность P2-3: tmp + os.replace) ──

def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def make_event(ts, source, message, *, container=None, stream=None, level=None,
               marker=None, error_code=None, status=None, priority_hint=None,
               actor_id=None, exit_code=None, expected=False,
               suppressed_count=0, sampled=False, endpoint=None):
    message = mask_secrets(str(message))[:2000]
    return {
        "ts": ts, "source": source, "container": container, "stream": stream,
        "level": level, "marker": marker, "error_code": error_code,
        "status": status, "message": message,
        "normalized_message": deep_normalize(message)[:600],
        "signature": make_signature(source, marker, error_code, message),
        "priority_hint": priority_hint, "actor_id": actor_id,
        "trace_id": None, "exit_code": exit_code, "expected": expected,
        # 008: аддитивные поля гварда — перенос подавленных в разрешённое
        "suppressed_count": suppressed_count, "sampled": sampled,
        # 009 §7.3-1: диагностический endpoint — только из ИТОГОВОЙ строки
        # (замаскированной и усечённой); kw-only для явного управления
        "endpoint": endpoint if endpoint is not None else extract_endpoint(message),
    }


def append_events(sink: Path, events, collected_at: str) -> None:
    """Raw JSONL: daily-файлы 0640 + raw_ref (безопасно при пустом списке)."""
    if not events:
        return
    raw_dir = sink / "events" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for ev in events:
        day = ev["ts"][:10]
        by_day.setdefault(day, []).append(ev)
    for day, evs in sorted(by_day.items()):
        path = raw_dir / f"{day}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for i, ev in enumerate(evs, 1):
                ev["raw_ref"] = f"raw:{day}#line?{collected_at}"
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.chmod(path, 0o640)


# ── Источник (a,g,h): docker logs 4 контейнеров ──

def parse_log_line(line: str):
    """'2026-09-22T..Z <rest>' → (ts_iso, rest); rest без ts-префикса → (None, line)."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$", line)
    if m:
        return m.group(1), m.group(2)
    return None, line


def extract_marker(rest: str):
    for m in MARKER_RE.finditer(rest):
        if m.group(1) not in ("INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL", "HTTP"):
            return m.group(1)
    return None


# ── D1 (Ф5): класс «ожидаемый/рутинный» — capture-first НЕ меняется (всё пишем
# в raw), меняется только маркировка события expected=True и приоритет агрегата.

MCP_OK_MS_RE = re.compile(r"\[MCP\] tool=\S+ ok .*?\b(\d+(?:\.\d+)?) ms")

# 006 (P2-new-3): слово-детект сбойных audit-строк. Word-boundary обязателен:
# наивный "error" in low ловит сам маркер ERRORS_QUERY («errors_query» содержит
# «error»), \berror\b — нет («s» — word-char). failed/failure — тоже отделяем.
AUDIT_FAIL_RE = re.compile(r"\b(error|errors|failed|failure|fail|exception)\b")


def classify_routine(rest: str, level, marker, status, slow_ms: float):
    """→ (expected, hint): routine-строки INFO-уровня без признаков ошибки.

    routine: '[MCP] tool=… ok <ms>' при ms<slow_ms; '[MCP] tool=… start';
    access '[REQ] …' 2xx/без явного 4xx-5xx.
    НЕ routine (expected=False): ERROR/CRITICAL/traceback, любой 5xx, '[MCP]'
    с error, WARNING. slow: '[MCP] … ok <ms≥slow_ms>' → hint='slow' (P1).
    """
    low = rest.lower()
    if level in ("ERROR", "CRITICAL") or status is not None and status >= 500:
        return False, None
    if "traceback" in low or "exception" in low or ("error" in low and marker == "MCP"):
        return False, None
    if level == "WARNING":
        return False, None  # WARNING не рутинен (кроме health-проб — свой источник)
    if marker == "MCP":
        m = MCP_OK_MS_RE.search(rest)
        if m and level in (None, "INFO"):
            ms = float(m.group(1))
            if ms >= slow_ms:
                return False, "slow"
            return True, None
        if re.search(r"\[MCP\] tool=\S+ start", rest) and level in (None, "INFO"):
            return True, None
        return False, None
    if marker == "REQ" and (status is None or status < 400):
        return True, None
    if (marker == "ERRORS_QUERY" and level in (None, "INFO")
            and not AUDIT_FAIL_RE.search(low)):
        # 006 (P1-1б): audit-маркер тула errors_query — routine → P3-baseline;
        # иначе default P2, а при ≥2 admin-ключах/росте — P1 (портит noise_ratio).
        # P2-new-3: сбойные строки (error/failed/exception как СЛОВА) НЕ глотаем.
        return True, None
    return False, None


def parse_docker_log_events(container: str, lines, last_ts: str, slow_ms: float = 60000):
    """Capture-first отбор: маркеры, WARN/ERROR/CRITICAL, traceback-блоки, access 4xx/5xx.

    Traceback-блок: строка 'Traceback (...)' + последующие строки С отступом;
    закрывается первой строкой без отступа (строка исключения) — она входит в блок.
    D1 (Ф5): routine-строки ([MCP] ok fast / [MCP] start / [REQ] 2xx) помечаются
    expected=True — сбор не меняется (всё пишем в raw), меняется приоритет агрегата.
    """
    events = []
    parsed = []
    for raw in lines:
        ts, rest = parse_log_line(raw)
        if ts is not None:
            parsed.append((ts, rest))
    i, n = 0, len(parsed)
    while i < n:
        ts, rest = parsed[i]
        if rest.startswith("Traceback ("):
            block = [rest]
            i += 1
            while i < n:
                nxt = parsed[i][1]
                block.append(nxt)
                i += 1
                if not nxt.startswith((" ", "\t")):
                    break  # строка исключения (без отступа) закрывает блок
            events.append(make_event(
                ts, "docker_logs", "\n".join(block), container=container, stream="stderr",
                level="ERROR", priority_hint="traceback",
            ))
            continue
        if last_ts and ts <= last_ts:
            i += 1
            continue  # дедуп-окно: принимаем строки строго новее last_ts
        level = None
        m = LEVEL_BRACKET_RE.search(rest) or LEVEL_PREFIX_RE.match(rest)
        if m:
            level = m.group(1)
        marker = extract_marker(rest)
        acc = ACCESS_RE.search(rest)
        status = int(acc.group(3)) if acc else None
        want = marker or level in ("WARNING", "ERROR", "CRITICAL") or (status is not None and status >= 400)
        if want:
            hint = None
            if level == "CRITICAL":
                hint = "critical"
            elif status is not None and status >= 500:
                hint = "5xx"
            expected, routine_hint = classify_routine(rest, level, marker, status, slow_ms)
            if routine_hint:
                hint = routine_hint  # slow перекрывает routine (аномалия → P1)
            events.append(make_event(
                ts, "docker_logs", rest, container=container, stream=None, level=level,
                marker=marker, error_code=str(status) if status else None, status=status,
                priority_hint=hint, actor_id=classify_actor(rest, "docker_logs"),
                expected=expected,
            ))
        i += 1
    return events


def detect_hangs(events, window_min: int = 30):
    """M2(i): незакрытые '[MCP] tool=X start' за окно → синтетическое hang-событие (P0-кандидат).

    Прецедент: инцидент 2026-08-06 (reindex висел минуты без следа).
    Сообщение НЕ содержит возраста — сигнатура стабильна, дедуп по state.
    """
    starts = {}
    closed = set()
    for ev in events:
        if ev.get("container") != "mcp-knowledge-server" or "tool=" not in ev.get("message", ""):
            continue
        m = re.search(r"\[MCP\] tool=(\S+) (start|ok)", ev["message"])
        if not m:
            continue
        tool, phase = m.group(1), m.group(2)
        if phase == "start":
            starts.setdefault(tool, []).append(ev["ts"])
        else:
            closed.add(tool)
    hang_events = []
    for tool, ts_list in starts.items():
        if tool in closed:
            continue
        for ts in ts_list:
            try:
                age_min = (datetime.now(timezone.utc) - parse_ts(ts)).total_seconds() / 60
            except ValueError:
                continue
            if window_min <= age_min <= window_min * 4:  # окно 30–120 мин
                hang_events.append(make_event(
                    now_iso(), "docker_logs",
                    f"[MCP] tool={tool} start без ok >{window_min} мин (подозрение на зависание)",
                    container="mcp-knowledge-server", level="ERROR", marker="MCP",
                    priority_hint="hang",
                ))
    return hang_events


def collect_docker_logs(sink: Path, state: dict, cfg: dict):
    events = []
    dl_state = state.setdefault("docker_logs", {})
    for container in cfg.get("containers", []):
        last_ts = dl_state.get(container, "")
        since = last_ts
        if since:
            try:  # запас −60 с против джитера границ цикла
                since = (parse_ts(last_ts) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                since = ""
        try:
            proc = subprocess.run(
                ["docker", "logs", "--timestamps", *(["--since", since] if since else []), container],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[errors_collect] WARN: docker logs {container}: {exc} — skip", file=sys.stderr)
            continue
        if proc.returncode != 0:
            # P2-5: стек не поднят / docker недоступен — graceful skip, не ошибка
            print(f"[errors_collect] WARN: docker logs {container}: rc={proc.returncode} — skip",
                  file=sys.stderr)
            continue
        lines = (proc.stdout or "") + (proc.stderr or "")
        evs = parse_docker_log_events(container, lines.splitlines(), last_ts,
                                      float(cfg.get("slow_ms", 60000)))
        events.extend(evs)
        newest = max((e["ts"] for e in evs), default=None)
        if newest and (not last_ts or newest > last_ts):
            dl_state[container] = newest
    hang_events = detect_hangs(events, cfg.get("hang_window_min", 30))
    # дедуп hang-сигнатур (пока висит — не спамим каждый цикл); чистка старше 4×окна
    seen = state.setdefault("_hang_signatures_logged", {})
    now = datetime.now(timezone.utc)
    for sig in [s for s, ts in seen.items()
                if not ts or (now - parse_ts(ts)).total_seconds() > 4 * cfg.get("hang_window_min", 30) * 3600]:
        seen.pop(sig, None)
    hang_events = [h for h in hang_events if h["signature"] not in seen]
    for h in hang_events:
        seen[h["signature"]] = now_iso()
    events.extend(hang_events)
    return events


# ── Источник (b,c): cron-логи + [CRON]-строки wrapper'а ──

def collect_cron_logs(sink: Path, state: dict, cfg: dict):
    events = []
    cl_state = state.setdefault("cron_log", {})
    for logfile in cfg.get("cron_logs", []):
        path = Path(logfile)
        try:
            if not path.exists():
                continue
            stat = path.stat()
        except OSError as exc:
            print(f"[errors_collect] WARN: cron log {logfile}: {exc} — skip", file=sys.stderr)
            continue
        st = cl_state.get(logfile, {})
        # byte-offset дедуп + детект ротации (inode/size)
        if st and (st.get("inode") != stat.st_ino or stat.st_size < st.get("offset", 0)):
            st = {}
        offset = st.get("offset", 0)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if offset:
                    f.seek(offset)
                chunk = f.read()
                new_offset = f.tell()
        except OSError as exc:
            print(f"[errors_collect] WARN: cron log {logfile}: {exc} — skip", file=sys.stderr)
            continue
        cl_state[logfile] = {"inode": stat.st_ino, "offset": new_offset}
        if not chunk:
            continue
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            cron_m = CRON_LINE_RE.search(line)
            if cron_m:
                job, exit_code = cron_m.group(1), int(cron_m.group(2))
                events.append(make_event(
                    cron_m.group(4), "cron_log", line, level="INFO" if exit_code == 0 else "ERROR",
                    marker="CRON", exit_code=exit_code,
                    priority_hint=None if exit_code == 0 else "cron_nonzero",
                    actor_id=f"cron:{job}",
                ))
            elif re.search(r"\b(WARN|ERROR|CRITICAL|FAILED|Traceback)\b", line):
                level = "CRITICAL" if "CRITICAL" in line else ("ERROR" if "ERROR" in line else "WARNING")
                events.append(make_event(
                    now_iso(), "cron_log", line, level=level,
                    priority_hint="traceback" if "Traceback" in line else None,
                ))
    return events


# ── Источник (d): docker events (die/oom/restart/health_status) ──

def collect_docker_events(sink: Path, state: dict, cfg: dict):
    last_ts = state.get("docker_events", "")
    since = last_ts or (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now_iso()
    # D2 (Ф5): события ТОЛЬКО контейнеров нашего стека (cfg["containers"]);
    # чужие (donation_bot и пр.) игнорируются — фильтр в команде + защитный пост-фильтр
    scope = [c for c in cfg.get("containers", []) if c]
    cmd = ["docker", "events", "--since", since, "--until", until,
           "--filter", "type=container", "--format", "{{json .}}"]
    for name in scope:
        cmd += ["--filter", f"container={name}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[errors_collect] WARN: docker events: {exc} — skip", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"[errors_collect] WARN: docker events rc={proc.returncode} — skip", file=sys.stderr)
        return []
    events = []
    for line in (proc.stdout or "").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = raw.get("Action", "")
        if action not in ("die", "oom", "restart", "health_status"):
            continue
        attrs = (raw.get("Actor") or {}).get("Attributes") or {}
        container = attrs.get("name", raw.get("id", "")[:12])
        if scope and container not in scope:  # D2: пост-фильтр — чужие не проходят
            continue
        ts_raw = raw.get("Time") or raw.get("time")
        ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts_raw else until
        if last_ts and ts <= last_ts:
            continue
        hint, level, exit_code = None, "WARNING", None
        if action == "oom":
            hint, level = "oom", "ERROR"
        elif action == "restart":
            hint, level = "restart", "ERROR"
        elif action == "die":
            exit_code = int(attrs.get("exitCode", -1) or -1)
            if exit_code != 0:
                hint, level = "restart", "ERROR"  # die(non-zero) — P0-признак (restart-класс)
            else:
                level = "INFO"  # плановый stop (compose down) — capture-first без P0
        elif action == "health_status" and "unhealthy" in str(attrs.get("health") or attrs.get("old") or action):
            hint, level = "health_degraded", "ERROR"
        events.append(make_event(
            ts, "docker_events", f"docker event: {action} container={container}"
            + (f" exit={exit_code}" if exit_code is not None else ""),
            container=container, level=level, marker="LIFECYCLE",
            priority_hint=hint, exit_code=exit_code,
        ))
    if events:
        state["docker_events"] = max(e["ts"] for e in events)
    return events


# ── Источник (e): host-пороги (дедуп P2-4: событие при пересечении + снапшот ≤1/час) ──

def _host_thresholds(cfg: dict):
    th = cfg.get("thresholds", {})
    return (
        float(th.get("df_warn_pct", 85)), float(th.get("df_crit_pct", 95)),
        float(th.get("ram_avail_min_pct", 10)), float(th.get("load15_factor", 2)),
        float(th.get("vram_warn_pct", 95)),
    )


def collect_host(sink: Path, state: dict, cfg: dict, data_root: Path):
    events = []
    h_state = state.setdefault("host", {})
    df_warn, df_crit, ram_min, load_factor, vram_warn = _host_thresholds(cfg)
    checks = []  # (key, breached, is_critical, message, hint)

    try:
        out = subprocess.run(["df", "-P", str(data_root)], capture_output=True, text=True, timeout=15, check=False)
        parts = out.stdout.splitlines()[-1].split()
        pct = float(parts[4].rstrip("%"))
        checks.append(("df", pct >= df_warn, pct >= df_crit,
                       f"df(data_root)={pct:.0f}% (warn≥{df_warn:.0f}% crit≥{df_crit:.0f}%)",
                       "disk_critical" if pct >= df_crit else None))
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        print(f"[errors_collect] WARN: df: {exc} — skip", file=sys.stderr)

    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                k, v = line.split(":", 1)
                meminfo[k] = float(v.split()[0])
        avail_pct = 100 * meminfo["MemAvailable"] / meminfo["MemTotal"]
        checks.append(("ram", avail_pct < ram_min, avail_pct < ram_min / 2,
                       f"RAM avail={avail_pct:.1f}% (<{ram_min:.0f}%)", None))
    except (OSError, KeyError, ValueError) as exc:
        print(f"[errors_collect] WARN: /proc/meminfo: {exc} — skip", file=sys.stderr)

    try:
        with open("/proc/loadavg", encoding="ascii") as f:
            load15 = float(f.read().split()[2])
        nproc = os.cpu_count() or 1
        limit = load_factor * nproc
        checks.append(("load15", load15 > limit, load15 > 2 * limit,
                       f"load15={load15:.2f} > {limit:.1f} (2×nproc, nproc={nproc})", None))
    except (OSError, ValueError, IndexError) as exc:
        print(f"[errors_collect] WARN: /proc/loadavg: {exc} — skip", file=sys.stderr)

    if shutil.which("nvidia-smi"):  # P2-5: нет бинарника → skip
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=20, check=False)
            for row in out.stdout.strip().splitlines():
                used, total = (float(x) for x in row.split(","))
                vram_pct = 100 * used / total if total else 0
                checks.append((f"vram_{int(vram_pct)}", vram_pct >= vram_warn, vram_pct >= 99,
                               f"GPU VRAM={vram_pct:.0f}% ≥{vram_warn:.0f}%", None))
                break
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"[errors_collect] WARN: nvidia-smi: {exc} — skip", file=sys.stderr)

    now = datetime.now(timezone.utc)
    for key, breached, is_crit, message, hint in checks:
        st = h_state.setdefault(key, {"breached": False, "last_event_ts": "", "last_snapshot_ts": ""})
        if breached and not st["breached"]:
            events.append(make_event(now_iso(), "host", message, level="ERROR" if is_crit else "WARNING",
                                     marker="HOST", priority_hint=hint))
            st["breached"], st["last_event_ts"] = True, now_iso()
        elif breached and st["breached"]:
            last_snap = st.get("last_snapshot_ts") or st.get("last_event_ts")
            if not last_snap or (now - parse_ts(last_snap)).total_seconds() >= 3600:
                events.append(make_event(now_iso(), "host", message + " [snapshot≤1/h]",
                                         level="WARNING", marker="HOST", priority_hint=hint))
                st["last_snapshot_ts"] = now_iso()
        else:
            st["breached"] = False
    return events


# ── Источник (f): health-пробы ×4 (событие при ИЗМЕНЕНИИ + снапшот 1/час, P3) ──

def _health_state(url: str):
    """→ (ok, degraded_detail): degraded = reconcile.state=error / embedding.loaded=false / не-200."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"unreachable:{type(exc).__name__}"
    detail = []
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            rec = ((data.get("reconcile") or {}).get("state")
                   or (data.get("components") or {}).get("reconcile", {}).get("state"))
            if rec == "error":
                detail.append("reconcile.state=error")  # main.py:297
            emb = data.get("embedding") or (data.get("components") or {}).get("embedding") or {}
            if isinstance(emb, dict) and emb.get("loaded") is False:
                detail.append("embedding.loaded=false")  # health.py:154-160
    except (json.JSONDecodeError, AttributeError):
        pass
    return status == 200, "; ".join(detail) or (f"http_{status}" if status != 200 else "")


def collect_health(sink: Path, state: dict, cfg: dict):
    events = []
    hl_state = state.setdefault("health", {})
    now = datetime.now(timezone.utc)
    for url in cfg.get("health_urls", []):
        ok, degraded = _health_state(url)
        cur = "ok" if ok and not degraded else f"degraded:{degraded}"
        st = hl_state.setdefault(url, {"state": None, "last_snapshot_ts": ""})
        if cur != st["state"]:
            events.append(make_event(
                now_iso(), "health", f"health {url} → {cur}",
                level="WARNING" if cur != "ok" else "INFO", marker="HEALTH",
                priority_hint="health_degraded" if cur != "ok" else None,
            ))
            st["state"], st["last_snapshot_ts"] = cur, now_iso()
        else:
            last = st.get("last_snapshot_ts") or ""
            if not last or (now - parse_ts(last)).total_seconds() >= 3600:
                events.append(make_event(
                    now_iso(), "health", f"health {url} → {cur} [snapshot≤1/h]",
                    level="INFO", marker="HEALTH",  # P3-baseline: пробы не шумят
                ))
                st["last_snapshot_ts"] = now_iso()
    return events


# ── P2-8: плановые restart рядом с [CRON] job=prod-update/deploy → expected ──

def mark_expected_restarts(events, window_min: int = 15):
    deploy_crons = [
        e for e in events
        if e["source"] == "cron_log" and e.get("marker") == "CRON"
        and re.search(r"job=(prod-update|update|deploy)\b", e.get("message", ""))
    ]
    if not deploy_crons:
        return events
    for ev in events:
        if ev.get("priority_hint") not in ("restart", "oom"):
            continue
        try:
            ev_dt = parse_ts(ev["ts"])
        except ValueError:
            continue
        for cr in deploy_crons:
            try:
                if abs((ev_dt - parse_ts(cr["ts"])).total_seconds()) <= window_min * 60:
                    ev["expected"] = True
                    break
            except ValueError:
                continue
    return events


# ── Агрегат E3 (приоритет сигнатуры — агрегатный, замороженный словарь) ──

def update_aggregates(sink: Path, events, cfg: dict,
                      suppressed_delta=None, burst_delta=None):
    """Инкремент aggregates/signatures.json; resolved = last_seen старше окна E4 (7d).

    008 (P1-2): suppressed_delta/burst_delta — отдельные параметры; сигнатура
    с 0 разрешённых за цикл (операторская suppression / cap-шторм) продолжает
    накапливать suppressed_total/suppressed_daily/count_total через stub-агрегат
    (§13.3:188 «подавленное не терять»). count_total/daily = ПОЛНАЯ правда о
    частоте: len(evs) + Δ (P2-new-1 — иначе count_7d полностью-подавленной
    сигнатуры = 0). last_seen подавленной НЕ обновляется (тишина в raw = E4).
    Burst-пост-шаг — КАЖДЫЙ цикл после лестницы (лестница P0/P1 не понижает).
    """
    suppressed_delta = suppressed_delta or {}
    burst_delta = burst_delta or {}
    if not events and not suppressed_delta and not burst_delta:
        return
    agg_path = sink / "aggregates" / "signatures.json"
    aggregates = load_json(agg_path, {})
    e4_days = int(cfg.get("e4_window_days", 7))
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    # окно роста неделя-к-неделе требует 14d; старше 21d — сворачиваем
    # (события уже учтены в count_total при записи — только удаляем ключи)
    cutoff = (now - timedelta(days=DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    two_weeks_ago = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    by_sig = {}
    for ev in events:
        by_sig.setdefault(ev["signature"], []).append(ev)
    for sig in list(by_sig) + [s for s in suppressed_delta if s not in by_sig]:
        evs = by_sig.get(sig, [])
        delta = int(suppressed_delta.get(sig, 0))
        a = aggregates.get(sig) or {
            "priority": "P2", "class": "T",
            "first_seen": (evs[0]["ts"] if evs else now_iso()),
            "last_seen": (evs[0]["ts"] if evs else now_iso()),
            "count_total": 0, "daily": {},
            "actors": [], "sources": [], "last_example": None,
            "status": "active", "fixed_at": None,
        }
        if evs:
            hints = [(e.get("priority_hint"), e.get("expected", False)) for e in evs]
            p0 = any(h in P0_HINTS and not exp for h, exp in hints)
            slow = any(h == "slow" for h, _ in hints)
            actors = {e.get("actor_id") for e in evs if e.get("actor_id")}
            # D1 (Ф5): routine-сигнатура = все события expected и ни одного не-routine
            # за историю (инкрементальность: флаг залипает, если хоть раз была не-рутина)
            if any(not e.get("expected", False) for e in evs):
                a["has_non_routine"] = True
            routine_all = (not a.get("has_non_routine")
                           and all(e.get("expected", False) for e in evs))
            a["last_seen"] = max(e["ts"] for e in evs)
            a["actors"] = sorted(set(a.get("actors", [])) | actors)[:50]
            a["sources"] = sorted(set(a.get("sources", [])) | {e["source"] for e in evs})
            a["last_example"] = {"ts": evs[-1]["ts"], "message": evs[-1]["message"][:300]}
        # инкременты: события + suppressed-дельта — ПОЛНАЯ правда о частоте (P2-new-1)
        a["daily"][day] = a["daily"].get(day, 0) + len(evs) + delta
        for d in [d for d in a["daily"] if d < cutoff]:
            a["daily"].pop(d)
        if delta:
            sd = a.get("suppressed_daily") or {}
            sd[day] = sd.get(day, 0) + delta
            for d in [d for d in sd if d < cutoff]:  # P2-4: cutoff 21d единообразно
                sd.pop(d)
            a["suppressed_daily"] = sd
            a["suppressed_total"] = a.get("suppressed_total", 0) + delta
        a["count_total"] += len(evs) + delta
        count_7d = sum(n for d, n in a["daily"].items() if d >= week_ago)
        count_prev_7d = sum(n for d, n in a["daily"].items()
                            if two_weeks_ago <= d < week_ago)
        a["count_7d"], a["count_prev_7d"] = count_7d, count_prev_7d
        if evs:
            # приоритет E3 (заморожено): P0-hint(не expected) → slow → burst(008)
            # → routine → P1(≥2 акторов | рост) → P3-baseline → P2
            baseline = all(
                (e.get("status") in BASELINE_4XX and not e.get("actor_id"))
                or (e.get("marker") == "HEALTH") or ("[AUTH]" in e.get("message", ""))
                for e in evs
            )
            if p0:
                a["priority"], a["class"] = "P0", "T"
            elif slow:
                # D1: '[MCP] … ok <ms ≥ slow_ms>' — реальная аномалия (не рутина и не P2)
                a["priority"], a["class"], a["slow"] = "P1", "U", True
            elif any(h == "burst" for h, _ in hints):
                # 008: [GUARD]-маркер с ЯВНЫМ priority_hint="burst" → P1/T (P2-5)
                a["priority"], a["class"] = "P1", "T"
            elif routine_all and not p0:
                # D1: вся сигнатура — ожидаемая рутина ([MCP] ok fast / start,
                # [REQ] 2xx) → P3-baseline; «≥2 акторов ⇒ P1» к routine НЕ применяется
                a["priority"], a["class"] = "P3", "T"
            elif len(set(a["actors"])) >= 2 or (count_prev_7d > 0 and count_7d > count_prev_7d):
                a["priority"] = "P1"
                a["class"] = "U" if a["actors"] else "T"
            elif baseline:
                a["priority"], a["class"] = "P3", "T"
            else:
                a["priority"] = "P2"
                a["class"] = "U" if a["actors"] else "T"
        # 008 (P1-1): жертва burst-эскалации получает burst_ts + burst_count_5m
        b = burst_delta.get(sig)
        if b:
            a["burst"] = True
            a["burst_ts"] = b.get("burst_ts")
            a["burst_count_5m"] = b.get("burst_count_5m")
        # E4: resolved = тишина ≥ окна; рецидив — last_seen обновится, report пометит regressed
        if a["status"] == "active" and (now - parse_ts(a["last_seen"])).days >= e4_days:
            a["status"], a["fixed_at"] = "resolved", a["last_seen"]
        elif a["status"] == "resolved" and (now - parse_ts(a["last_seen"])).days < e4_days:
            a["status"] = "active"  # рецидив в окне наблюдения
            a["fixed_at"] = None
        aggregates[sig] = a
    # 008 (P1-1): burst-пост-шаг КАЖДЫЙ цикл после замороженной лестницы —
    # sticky-эскалация P2/P3→P1 в окне 7d от burst_ts (декей: старше 7d не влияет)
    for a in aggregates.values():
        if not (a.get("burst") and a.get("burst_ts")):
            continue
        try:
            age_s = (now - parse_ts(a["burst_ts"])).total_seconds()
        except ValueError:
            continue
        if age_s <= 7 * 86400 and a.get("priority") not in ("P0", "P1"):
            a["priority"] = "P1"
    atomic_write_json(agg_path, aggregates)


# ── Ротация собственного лога (P2-6): cron пишет сюда 288 запусков/день ──

def rotate_own_log(logfile: str, max_bytes: int = 10 * 1024 * 1024, keep_lines: int = 2000) -> None:
    try:
        path = Path(logfile)
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-keep_lines:]
        tmp = path.with_name(path.name + ".tail")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp, path)
        print(f"[errors_collect] own log rotated to last {keep_lines} lines", file=sys.stderr)
    except OSError as exc:
        print(f"[errors_collect] WARN: own log rotate: {exc}", file=sys.stderr)


# ── Конфиг (рендер ansible; дефолты inline — работает без config.json на dev) ──

DEFAULT_CONFIG = {
    "containers": ["mcp-knowledge-server", "kb-console", "mcp-qdrant-dev", "mcp-knowledge-ollama"],
    "cron_logs": ["/var/log/mcp-backup.log", "/var/log/mcp-quality.log"],
    "thresholds": {"df_warn_pct": 85, "df_crit_pct": 95, "ram_avail_min_pct": 10,
                   "load15_factor": 2, "vram_warn_pct": 95},
    "retention_days": 90, "hold_days": 14, "e4_window_days": 7,
    "prune": {"enabled": False},
    "health_urls": ["http://localhost:8000/health", "http://localhost:6333/healthz",
                    "http://localhost:11435/api/tags", "http://localhost:8085/"],
    "own_log": "/var/log/mcp-errors-collect.log",
    "hang_window_min": 30,
    "slow_ms": 60000,  # D1 (Ф5): '[MCP] … ok <ms>' при ms ≥ slow_ms → P1 (slow=true)
    # 008 «Шторм-гард» (kill-switch — по прецеденту prune.enabled): cap 5/60 с
    # на сигнатуру; burst ≥50/цикл или ×10 к среднему за 12 циклов (~1 ч);
    # TTL guard/burst-состояния в collector_state.json.
    "guard": {"enabled": True, "cap_per_minute": 5, "burst_abs": 50,
              "burst_ratio": 10, "burst_window_cycles": 12, "state_ttl_days": 7},
}


def load_config(sink: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep-copy дефолтов
    file_cfg = load_json(sink / "config.json", {})
    if isinstance(file_cfg, dict):
        for key, val in file_cfg.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="host-коллектор Error→Rule (один цикл на запуск)")
    ap.add_argument("--once", action="store_true", help="явный один цикл (поведение по умолчанию)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    cfg = load_config(sink)
    rotate_own_log(cfg.get("own_log", "/var/log/mcp-errors-collect.log"))
    (sink / "events" / "raw").mkdir(parents=True, exist_ok=True)
    (sink / "aggregates").mkdir(parents=True, exist_ok=True)

    state_path = sink / "collector_state.json"
    state = load_json(state_path, {})

    events = []
    for collector in (collect_docker_logs, collect_cron_logs, collect_docker_events):
        try:
            events.extend(collector(sink, state, cfg))
        except Exception as exc:  # noqa: BLE001 — источник упал ⇒ остальные живут (P2-5)
            print(f"[errors_collect] WARN: {collector.__name__}: {exc!r} — skip", file=sys.stderr)
    try:
        events.extend(collect_host(sink, state, cfg, DATA_ROOT))
    except Exception as exc:  # noqa: BLE001
        print(f"[errors_collect] WARN: collect_host: {exc!r} — skip", file=sys.stderr)
    try:
        events.extend(collect_health(sink, state, cfg))
    except Exception as exc:  # noqa: BLE001
        print(f"[errors_collect] WARN: collect_health: {exc!r} — skip", file=sys.stderr)

    events = mark_expected_restarts(events)
    collected_at = now_iso()
    # 008 «Шторм-гард»: write-side гвард между маркировкой и записью (§7.0-1 —
    # единственная вставка). Lazy-импорт: errors_guard берёт make_event отсюда.
    from errors_guard import apply_write_guard, load_suppression
    events, guard_markers, sup_delta, burst_delta = apply_write_guard(
        events, state, cfg, load_suppression(sink), collected_at)
    append_events(sink, events + guard_markers, collected_at)
    update_aggregates(sink, events + guard_markers, cfg,
                      suppressed_delta=sup_delta, burst_delta=burst_delta)
    atomic_write_json(state_path, state)
    print(f"[errors_collect] {collected_at}: captured={len(events)}"
          f"(+{len(guard_markers)} guard) suppressed={sum(sup_delta.values())} → {sink}")
    return 0  # graceful: сбор даже без стека не роняет cron (P2-5)


if __name__ == "__main__":
    sys.exit(main())
