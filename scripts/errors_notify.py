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

Durable-доставка (code-2026-09-30-039, scope C — хост aikb ходит в TG только
через внешний прокси, канал/прокси может моргать):
  • in-run ретраи на каждый чанк: notify["retry_attempts"] (default 3) попыток,
    backoff notify["retry_backoff_sec"] (default "2,5,10" — список через запятую,
    лишние элементы игнорируются). Ретраятся ТОЛЬКО транспортные/серверные
    ошибки (URLError/OSError/timeout/5xx/429); 4xx кроме 429 — не ретраются;
  • durable spool при финальном фейле чанка: reports/tg-pending/
    <UTC-ts>-<pid>-<n>.json, mode 0600 (атомарная запись tmp+os.replace).
    Содержимое — created_at/host/chat_id/text/attempts/last_error(замаскированный).
    Токен/proxy-креды в файл НЕ пишутся (читаются из notify.json в момент flush).
    Лимит notify["tg_pending_max"] (default 200): переполнение → прунинг самых
    старых + строка в tg-errors.log;
  • auto-flush в начале send_telegram: доставляются pending от старых к новым,
    ≤ notify["tg_flush_max"] (default 5) за вызов; успех → файл удалён; неуспех →
    файл остаётся, attempts/last_error обновляются (замаскировано); битый JSON →
    карантин tg-pending/.corrupt/ + строка в лог. notify_ready() == false →
    «TG: skip», spool не трогается (как сейчас);
  • взаимное исключение (039 iter2): неблокирующий fcntl.flock на
    reports/.tg-flush.lock вокруг ВСЕГО тела отправки (flush + чанки + spool).
    Занят → «TG: skip (flush lock busy)» + rc 0 (никогда rc≠0 — cron_wrap дал бы
    ложный P0). Исключает дубли доставки при наложении cron-прогонов */5 и
    «воскрешение» файла write-back'ом после unlink другого процесса;
  • CLI: python3 scripts/errors_notify.py --flush [--json] [--sink PATH].
    rc=0 при «нет pending / нет настроек / доставлено / лок занят», rc≠0 только
    при внутренней ошибке. --json — машинные счётчики sent/spooled/pending/
    flushed/failed/skipped.

notify.json (0600, вне git): {"bot_token", "chat_id", "proxy"(опц.),
"no_proxy"(опц.), "host"(опц.), "retry_attempts"(опц.), "retry_backoff_sec"(опц.),
"tg_pending_max"(опц.), "tg_flush_max"(опц.)}; источник — errors_notify_import.sh
из /etc/backup-status.env или ansible errors-notify.json.j2 (прод). Python ≥3.9,
stdlib-only. Читает секрет ТОЛЬКО этот скрипт.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (  # sibling-импорт по прецеденту errors_report.py:31
    DATA_ROOT,
    load_json,
)

TG_CHUNK = 4096
SKIP_MSG = "TG: skip (notify.json отсутствует/пуст — рендер ansible errors.yml setup)"
HOST_ENV = "MCP_ERRORS_HOST"
FLUSH_LOCK_NAME = ".tg-flush.lock"

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF = "2,5,10"
DEFAULT_TG_PENDING_MAX = 200
DEFAULT_TG_FLUSH_MAX = 5


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
    return _load_notify(sink) is not None


def _mask(reason, *secrets):
    """Токен и proxy-креды НИКОГДА не попадают в тексты (R5/AC-proxy-1)."""
    for s in secrets:
        if s:
            reason = re.sub(re.escape(str(s)), "<token>" if s is secrets[0] else "<proxy>", reason)
    return reason


# ── durable-доставка: конфиг, лок, спул, флаш ──

def _load_notify(sink):
    notify = load_json(Path(sink) / "notify.json", None)
    if isinstance(notify, dict) and notify.get("bot_token") and notify.get("chat_id"):
        return notify
    return None


def _int_cfg(notify, key, default):
    try:
        raw = notify.get(key)
        if raw is None or raw == "":
            return default
        return int(raw)
    except (ValueError, TypeError):
        return default


def _parse_backoff(raw):
    """Строка "2,5,10" → [2.0, 5.0, 10.0]; лишние элементы игнорируются по месту."""
    if isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = str(raw).split(",")
    out = []
    for p in parts:
        try:
            out.append(float(str(p).strip()))
        except (ValueError, TypeError):
            continue
    return out or [0.0]


def _retry_cfg(notify):
    attempts = max(1, _int_cfg(notify, "retry_attempts", DEFAULT_RETRY_ATTEMPTS))
    backoffs = _parse_backoff(notify.get("retry_backoff_sec", DEFAULT_RETRY_BACKOFF))
    return attempts, backoffs


def _backoff_delay(backoffs, idx):
    if not backoffs:
        return 0.0
    return float(backoffs[idx]) if idx < len(backoffs) else float(backoffs[-1])


def _retryable(exc):
    """Ретраить только транспортные/серверные ошибки; 4xx кроме 429 — нет."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, OSError))


def _build_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    return None


def _send_one(opener, token, chat_id, chunk, attempts, backoffs):
    """Отправить ОДИН чанк с ретраями → (ok, last_exc|None, attempts_made)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    last = None
    made = 0
    for attempt in range(1, attempts + 1):
        made = attempt
        try:
            if opener is not None:
                resp_ctx = opener.open(req, timeout=20)
            else:
                resp_ctx = urllib.request.urlopen(req, timeout=20)
            with resp_ctx as resp:
                if resp.status == 200:
                    return True, None, made
                last = urllib.error.URLError(f"HTTP {resp.status}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        if not _retryable(last):
            break
        if attempt < attempts:
            delay = _backoff_delay(backoffs, attempt - 1)
            if delay:
                time.sleep(delay)
    return False, last, made


def _pending_dir(sink):
    return Path(sink) / "reports" / "tg-pending"


def _pending_files(sink):
    d = _pending_dir(sink)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def _pending_count(sink):
    return len(_pending_files(sink))


@contextlib.contextmanager
def _flush_lock(sink):
    """Неблокирующий EX-flock на reports/.tg-flush.lock (контекст-менеджер).

    yields True если лок взят, False если занят (никогда rc≠0 — cron_wrap дал
    бы ложный P0). Прецедент errors_prune.py:168-176. flock на
    open-file-description: два open() одного файла (даже в одном процессе)
    конкурируют — тест на гонку возможен без многопроцессности. Лок снимается
    при закрытии fh по выходу из with.
    """
    reports = Path(sink) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    with open(reports / FLUSH_LOCK_NAME, "w") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


_spool_seq = 0


def _spool_name():
    global _spool_seq
    _spool_seq += 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{ts}-{os.getpid()}-{_spool_seq}.json"


def _remove_pending(path):
    try:
        path.unlink()
    except OSError:
        pass


def _write_pending(path, payload):
    """Атомарная запись spool/pending-файла (tmp + os.replace, mode 0600)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _quarantine_pending(sink, path):
    """Битый spool-файл → tg-pending/.corrupt/ (не занимает слот) + строка в лог."""
    corrupt = _pending_dir(sink) / ".corrupt"
    corrupt.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, corrupt / path.name)
    except OSError:
        _remove_pending(path)
    log_tg_error(sink, f"tg-pending: corrupt spool quarantined: {path.name}")


def _spool(sink, host, chat_id, chunk, attempts, last_error, pending_max):
    d = _pending_dir(sink)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "chat_id": chat_id,
        "text": chunk,
        "attempts": int(attempts),
        "last_error": last_error,
    }
    _write_pending(d / _spool_name(), payload)  # атомарно (tmp + os.replace, 0600)
    _prune_pending(sink, pending_max)


def _prune_pending(sink, pending_max):
    files = _pending_files(sink)
    excess = len(files) - pending_max
    if excess <= 0:
        return
    for f in files[:excess]:
        _remove_pending(f)
    log_tg_error(sink, f"tg-pending overflow: pruned {excess} oldest (limit {pending_max})")


def _flush_pending(sink, notify, token, chat_id, proxy, flush_max):
    """Доставить pending от старых к новым, ≤ flush_max → (delivered, failed).

    failed — ТОЛЬКО реально проваленные попытки (файлы сверх flush_max не
    считаются); битый JSON → карантин, мусор без text → удаление.
    """
    files = _pending_files(sink)
    if not files:
        return 0, 0
    opener = _build_opener(proxy)
    attempts, backoffs = _retry_cfg(notify)
    delivered = failed = 0
    for f in files[:flush_max]:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _quarantine_pending(sink, f)
            continue
        except OSError:
            _quarantine_pending(sink, f)
            continue
        if not isinstance(payload, dict) or not payload.get("text"):
            _remove_pending(f)
            log_tg_error(sink, f"tg-pending: dropped malformed spool {f.name}")
            continue
        pchat = payload.get("chat_id") or chat_id
        ok, last, made = _send_one(opener, token, pchat, payload["text"], attempts, backoffs)
        if ok:
            _remove_pending(f)
            delivered += 1
        else:
            failed += 1
            reason = _mask(str(last) if last is not None else "unknown", token, proxy)
            payload["attempts"] = int(payload.get("attempts", 0) or 0) + made
            payload["last_error"] = reason
            _write_pending(f, payload)
    return delivered, failed


def send_telegram(sink, text, chat_override=None, host_override=None):
    """Best-effort отправка → int (число доставленных чанков: pending-флаш + текст).

    Поведение бит-в-бит с errors_report.send_telegram (:382-417) по skip-логике,
    чанкам и continue-on-fail; аддитивно — прокси-слой, host-тег, ретраи,
    durable-spool, auto-flush pending и взаимное исключение (flock).
    """
    notify = _load_notify(sink)
    if notify is None:
        print(SKIP_MSG)
        log_tg_error(sink, SKIP_MSG)
        return 0
    with _flush_lock(sink) as held:
        if not held:
            print("TG: skip (flush lock busy — другой прогон доставляет)")
            log_tg_error(sink, "TG: skip (flush lock busy)")
            return 0
        token = notify["bot_token"]
        chat_id = chat_override or notify["chat_id"]
        proxy = notify.get("proxy") or None
        host = resolve_host(notify, host_override)
        tag = host_tag(host)
        chunks = build_chunks(text, tag)
        attempts, backoffs = _retry_cfg(notify)
        flush_max = max(0, _int_cfg(notify, "tg_flush_max", DEFAULT_TG_FLUSH_MAX))
        pending_max = max(1, _int_cfg(notify, "tg_pending_max", DEFAULT_TG_PENDING_MAX))
        opener = _build_opener(proxy)

        # 1) durable spool flush (старые → новые, ≤ flush_max за вызов)
        flushed, _ = _flush_pending(sink, notify, token, chat_id, proxy, flush_max)

        # 2) текущие чанки с ретраями; финальный фейл → durable spool
        sent = spooled = 0
        for i, chunk in enumerate(chunks, 1):
            ok, last, made = _send_one(opener, token, chat_id, chunk, attempts, backoffs)
            if ok:
                sent += 1
                continue
            reason = _mask(str(last) if last is not None else "unknown", token, proxy)
            print(f"TG: ошибка доставки чанка {i}: {reason}")
            log_tg_error(sink, f"chunk {i}/{len(chunks)}: {reason}")
            _spool(sink, host, chat_id, chunk, made, reason, pending_max)
            spooled += 1

        pending_after = _pending_count(sink)
        tail = f", в очереди {pending_after}" if pending_after > 0 else ""
        if sent:
            print(f"TG: отправлено ({sent}/{len(chunks)} чанков){tail}")
        else:
            print(f"TG: не отправлено (см. tg-errors.log){tail}")
        return sent + flushed


def flush(sink):
    """CLI --flush: доставить pending без нового текста → dict счётчиков."""
    notify = _load_notify(sink)
    if notify is None:
        return {"sent": 0, "spooled": 0, "pending": _pending_count(sink),
                "flushed": 0, "failed": 0, "skipped": False}
    with _flush_lock(sink) as held:
        if not held:
            return {"sent": 0, "spooled": 0, "pending": _pending_count(sink),
                    "flushed": 0, "failed": 0, "skipped": True}
        token = notify["bot_token"]
        chat_id = notify["chat_id"]
        proxy = notify.get("proxy") or None
        flush_max = max(0, _int_cfg(notify, "tg_flush_max", DEFAULT_TG_FLUSH_MAX))
        delivered, failed = _flush_pending(sink, notify, token, chat_id, proxy, flush_max)
        return {"sent": 0, "spooled": 0, "pending": _pending_count(sink),
                "flushed": delivered, "failed": failed, "skipped": False}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Error→Rule: общий TG-sender — flush durable spool")
    ap.add_argument("--sink", default=None,
                    help="override каталога sink (по умолчанию $DATA_ROOT/logs/errors)")
    ap.add_argument("--flush", action="store_true",
                    help="доставить pending-spool (без отправки нового текста)")
    ap.add_argument("--json", action="store_true",
                    help="машинный вывод JSON со счётчиками")
    args = ap.parse_args(argv)
    if not args.flush:
        ap.print_help(sys.stderr)
        return 2
    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    try:
        stats = flush(sink)
    except Exception as exc:  # noqa: BLE001 — best-effort: rc≠0 только здесь
        notify = _load_notify(sink)
        reason = _mask(str(exc),
                       (notify or {}).get("bot_token"),
                       (notify or {}).get("proxy"))
        if args.json:
            print(json.dumps({"error": reason, "sent": 0, "spooled": 0,
                              "pending": 0, "flushed": 0, "failed": 0, "skipped": False},
                             ensure_ascii=False))
        else:
            print(f"TG flush: внутренняя ошибка: {reason}")
        return 1
    if args.json:
        print(json.dumps(stats, ensure_ascii=False))
    else:
        if stats.get("skipped"):
            print("TG flush: skip (flush lock busy — другой прогон доставляет)")
        else:
            print(f"TG flush: доставлено {stats['flushed']}, "
                  f"осталось в очереди {stats['pending']}, не удалось {stats['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
