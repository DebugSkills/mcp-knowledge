#!/usr/bin/env python3
"""errors_notify.py — общий TG-sender цикла Error→Rule (code-2026-09-24-016, В2-блок б).

Вынесен из errors_report.py send_telegram (:382-417 iter-до-016) — контракт
бит-в-бит: чанки ≤4096 без разрыва строк · сбой чанка → reports/tg-errors.log
+ продолжение · exit-семантика 0 (best-effort) · токен НИКОГДА не печатается.

Аддитивно (дельты 2/3 спеки §7.2(б)):
  • прокси-слой — непустой notify["proxy"] → urllib.request.ProxyHandler(
    {"https": proxy, "http": proxy}) + build_opener().open(req); пустой/null →
    прежний прямой urllib.request.urlopen (direct-fallback). no_proxy (опц.)
    зарезервирован для будущих локальных адресов (api.telegram.org — единый
    внешний хост, в ProxyHandler не передаётся);
  • host-тег — КАЖДЫЙ отправляемый чанк начинается с 🤖[mcp-errors@<host>];
    host = MCP_ERRORS_HOST (env) → notify["host"] → socket.gethostname()
    (короткое имя); различает два хоста в общем ops-чате (AC-host-1/2);
  • маскировка расширена — proxy-креды → <proxy> рядом с <token> (R5).

notify.json (0600, вне git): {"bot_token", "chat_id", "proxy"(опц.),
"no_proxy"(опц.), "host"(опц.)}; источник — errors_notify_import.sh из
/etc/backup-status.env или ansible errors-notify.json.j2 (прод). Python ≥3.9,
stdlib-only. Читает секрет ТОЛЬКО этот скрипт.
"""

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import load_json  # sibling-импорт по прецеденту errors_report.py:31

TG_CHUNK = 4096
SKIP_MSG = "TG: skip (notify.json отсутствует/пуст — рендер ansible errors.yml setup)"
HOST_ENV = "MCP_ERRORS_HOST"


def resolve_host(notify=None, override=None):
    """Приоритет: CLI/env-override > notify["host"] > факт ОС (короткое имя)."""
    if override:
        return str(override)
    env = os.environ.get(HOST_ENV)
    if env:
        return env
    if isinstance(notify, dict) and notify.get("host"):
        return str(notify["host"])
    return socket.gethostname().split(".")[0]


def host_tag(host):
    return f"🤖[mcp-errors@{host}]"


def build_chunks(text, tag):
    """Чанки ≤TG_CHUNK без разрыва строк (бит-в-бит :393-401) + тег первой
    строкой КАЖДОГО чанка (дельта 3); габарит тега — в запасе длины."""
    headroom = max(20, len(tag) + 1)
    lines, chunks, cur = text.splitlines(), [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > TG_CHUNK - headroom:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [f"{tag}\n{c}" for c in chunks]


def tg_log_path(sink):
    return Path(sink) / "reports" / "tg-errors.log"


def log_tg_error(sink, line):
    path = tg_log_path(sink)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")


def notify_ready(sink):
    """notify.json валиден для отправки (bot_token+chat_id непусты)."""
    notify = load_json(Path(sink) / "notify.json", None)
    return isinstance(notify, dict) and bool(notify.get("bot_token")) and bool(notify.get("chat_id"))


def _mask(reason, *secrets):
    """Токен и proxy-креды НИКОГДА не попадают в тексты (R5/AC-proxy-1)."""
    for s in secrets:
        if s:
            reason = re.sub(re.escape(str(s)), "<token>" if s is secrets[0] else "<proxy>", reason)
    return reason


def send_telegram(sink, text, chat_override=None, host_override=None):
    """Best-effort отправка: → int (число доставленных чанков; 0 = skip/фейл).

    Поведение бит-в-бит с errors_report.send_telegram (:382-417) поskip-логике,
    чанкам и continue-on-fail; аддитивно — прокси-слой и host-тег каждого чанка.
    """
    notify = load_json(Path(sink) / "notify.json", None)
    if not isinstance(notify, dict) or not notify.get("bot_token") or not notify.get("chat_id"):
        print(SKIP_MSG)
        log_tg_error(sink, SKIP_MSG)
        return 0
    token = notify["bot_token"]
    chat_id = chat_override or notify["chat_id"]
    proxy = notify.get("proxy") or None
    tag = host_tag(resolve_host(notify, host_override))
    chunks = build_chunks(text, tag)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    opener = None
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    sent = 0
    for i, chunk in enumerate(chunks, 1):
        try:
            data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            if opener is not None:
                resp_ctx = opener.open(req, timeout=20)
            else:
                resp_ctx = urllib.request.urlopen(req, timeout=20)
            with resp_ctx as resp:
                if resp.status == 200:
                    sent += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = _mask(str(exc), token, proxy)
            print(f"TG: ошибка доставки чанка {i}: {reason}")
            log_tg_error(sink, f"chunk {i}/{len(chunks)}: {reason}")
    print(f"TG: отправлено ({sent}/{len(chunks)} чанков)" if sent
          else "TG: не отправлено (см. tg-errors.log)")
    return sent
