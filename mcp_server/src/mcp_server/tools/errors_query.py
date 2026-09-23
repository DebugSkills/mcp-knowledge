# ruff: noqa: BLE001
"""errors_query — read-only запрос к sink «Error → Rule» (code-2026-09-22-006).

Admin-only: WRITE_TOOLS + ЯВНОЕ исключение из EDITOR_TOOLS (auth.py).
Семантически read-only: write-вызовов нет, маунт sink в контейнер :ro.

Отклонение от канона E7 (scoped token → admin-only) — решение оператора
2026-09-22 (спека §0): компенсации — FS-:ro, stdout-audit без значений (§5),
маскирование на записи + оборонительное на отдаче, лимиты/капы,
структурная проверка sink (P1-2), routine-P3-класс аудита (P1-1б).

P1-3: ВЕСЬ fs-доступ — в sync-хелперах через run_in_executor (конвенция
browse.py:52, fragments.py:115, crud.py:421-439). CC ≤ 10 на функцию.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp_server.config import settings

logger = logging.getLogger("mcp_knowledge.tools.errors_query")

# ── Капы/лимиты (P2-3) ─────────────────────────────────────
RAW_SCAN_CAP_BYTES = 64 * 1024 * 1024  # суммарный объём raw-файлов ≤ 64 МБ
RAW_SCAN_MAX_DAYS = 30                 # окно raw-скана ≤ 30d
MESSAGE_MAX_CHARS = 500                # message в примерах ≤ 500 симв.
PERIOD_DAYS = {"24h": 1, "7d": 7, "14d": 14, "30d": 30}
PRIO_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# ── Оборонительное маскирование на отдаче (P2-5, DRY-дубликат) ──
# SYNC: errors_collect.py:99-106 mask_secrets — дубликат ОБЯЗАТЕЛЕН: scripts/
# не копируется в образ (Dockerfile: только src/+tests/), импорт невозможен.
# Дрейф ловит tests/test_masking_parity.py (паритет наборов + поведения).
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmcp_[a-z]{1,3}_[A-Za-z0-9]+\b"), "<secret>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<secret>"),
    (re.compile(r"(?i)Authorization:\s*.*"), "Authorization: <secret>"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <secret>"),
    (re.compile(r"(?i)\b(password|token|api[_-]?key|secret)\s*[=:]\s*\S+"), r"\1=<secret>"),
]

_SINK_HINT_MISSING = (
    "каталог существует, но структуры sink нет "
    "(events/raw/ + aggregates/signatures.json): проверь compose-маунт :ro "
    "и ERRORS_SINK_DIR (Docker авто-создаёт пустой bind-каталог)"
)
_SINK_HINT_PERMISSION = (
    "sink не читается (PermissionError): проверь права/владельца "
    "маунта :ro и пользователя контейнера"
)


def mask_output(text: str) -> str:
    """Маскирование на отдаче (глубина 2: на записи уже замаскировано)."""
    for rx, repl in _SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text


def _truncate(text: str) -> str:
    if len(text) <= MESSAGE_MAX_CHARS:
        return text
    return text[:MESSAGE_MAX_CHARS] + "…[truncated]"


# ── Фильтры (валидация входа) ──────────────────────────────

def _parse_filters(params: dict[str, Any]) -> dict[str, Any] | str:
    """Валидация и разбор фильтров. Строка = ошибка invalid_params."""
    since = params.get("since")
    period = params.get("period")
    if since and period:
        return "invalid_params: 'since' and 'period' are mutually exclusive"
    cutoff: datetime | None = None
    window_days: int | None = None
    if period:
        if period not in PERIOD_DAYS:
            return f"invalid_params: 'period' must be one of {sorted(PERIOD_DAYS)}"
        window_days = PERIOD_DAYS[period]
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    elif since:
        try:
            s = str(since).replace("Z", "+00:00")
            cutoff = datetime.fromisoformat(s)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            window_days = max(0, (datetime.now(timezone.utc) - cutoff).days)
        except ValueError:
            return "invalid_params: 'since' must be ISO-8601 date-time"
    priority = params.get("priority") or []
    if any(p not in PRIO_RANK for p in priority):
        return "invalid_params: 'priority' items must be P0..P3"
    return {
        "view": params.get("view", "aggregates"),
        "priority": list(priority),
        "source": params.get("source"),
        "signature": params.get("signature"),
        "query": (params.get("query") or "").lower() or None,
        "include_audit": bool(params.get("include_audit", False)),
        "status": params.get("status"),
        "cutoff": cutoff,
        "window_days": window_days,
        "limit": max(1, min(100, int(params.get("limit", 20)))),
        "examples_limit": max(1, min(10, int(params.get("examples_limit", 3)))),
    }


# ── Sink: структурная проверка (P1-2) ──────────────────────

def _probe_sink(sink: Path) -> tuple[bool, str | None, str | None]:
    """sink_available = структура (events/raw/ + aggregates/signatures.json),
    не «каталог существует»: Docker авто-создаёт пустой bind-каталог."""
    raw_dir = sink / "events" / "raw"
    agg_path = sink / "aggregates" / "signatures.json"
    try:
        raw_ok = raw_dir.is_dir()
        agg_ok = agg_path.is_file()
        if agg_ok:
            with open(agg_path, encoding="utf-8") as f:  # читаемость
                f.read(1)
    except PermissionError:
        return False, "permission_denied", _SINK_HINT_PERMISSION
    except OSError as exc:
        return False, f"sink_unavailable ({exc.__class__.__name__})", _SINK_HINT_MISSING
    if not (raw_ok and agg_ok):
        return False, "sink_unavailable", _SINK_HINT_MISSING
    return True, None, None


def _load_aggregates(sink: Path) -> tuple[dict[str, Any], list[str]]:
    """signatures.json → dict; повреждённый JSON → ({}, warning), не exception."""
    warnings: list[str] = []
    try:
        with open(sink / "aggregates" / "signatures.json", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, ["aggregates: unexpected type, skipped"]
        return data, warnings
    except json.JSONDecodeError as exc:
        return {}, [f"aggregates: broken JSON skipped ({exc.__class__.__name__})"]
    except OSError as exc:
        return {}, [f"aggregates: unreadable ({exc.__class__.__name__})"]


# ── Агрегаты: фильтр/тренд/рендер ──────────────────────────

def _trend(agg: dict[str, Any]) -> str | None:
    """up/down/flat/null — семантика errors_collect.py:733 (OQ ⑥)."""
    c7, prev = agg.get("count_7d", 0), agg.get("count_prev_7d", 0)
    if not prev:
        return None
    if c7 > prev:
        return "up"
    if c7 < prev:
        return "down"
    return "flat"


def _filter_aggregates(
    aggs: dict[str, Any], flt: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """→ отсортированный [(signature, agg)] по фильтрам (last_example НЕ отдаём)."""
    out: list[tuple[str, dict[str, Any]]] = []
    for sig, agg in aggs.items():
        parts = sig.split("|", 2)
        marker = parts[1] if len(parts) > 1 else ""
        if not flt["include_audit"] and marker == "ERRORS_QUERY":
            continue  # P1-1г: собственный аудит не засоряет диагностику
        if flt["priority"] and agg.get("priority") not in flt["priority"]:
            continue
        if flt["source"] and flt["source"] not in agg.get("sources", []):
            continue
        if flt["signature"] and sig != flt["signature"]:
            continue
        if flt["status"] and agg.get("status") != flt["status"]:
            continue
        if flt["query"] and flt["query"] not in sig.lower():
            continue  # query — только по normalized_message (OQ ⑤): она в ключе сигнатуры
        if flt["cutoff"] is not None:
            try:
                last = datetime.fromisoformat(str(agg.get("last_seen", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if last < flt["cutoff"]:
                continue
        out.append((sig, agg))
    # стабильные сортировки: сначала last_seen desc, затем priority asc
    out.sort(key=lambda sa: str(sa[1].get("last_seen", "")), reverse=True)
    out.sort(key=lambda sa: PRIO_RANK.get(sa[1].get("priority"), 9))
    return out


def _suppressed_7d(agg: dict[str, Any]) -> int:
    """Вычисляемое на рендере из suppressed_daily (008 P2-7 — единообразно с
    count_7d errors_collect.py:724-725; не хранится — одна правда при обрезке 21d)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    return sum(n for d, n in (agg.get("suppressed_daily") or {}).items()
               if str(d) >= cutoff)


def _render_aggregate(sig: str, agg: dict[str, Any]) -> dict[str, Any]:
    """Агрегат без last_example и actors[] (Critic ③, P2-2): только счётчики+meta.

    008 (§7.5): +suppressed_total / suppressed_7d (вычисляемое) / burst /
    burst_ts — аддитивно через .get: старые агрегаты без полей не ломают выдачу (R6).

    009 (§7.3-3): +endpoints — топ-эндпоинты сигнатуры (count desc, key asc),
    аддитивно через .get: legacy-агрегат без поля → {} (R6).
    """
    return {
        "signature": sig,
        "priority": agg.get("priority"),
        "class": agg.get("class"),
        "status": agg.get("status"),
        "count_7d": agg.get("count_7d", 0),
        "count_prev_7d": agg.get("count_prev_7d", 0),
        "count_total": agg.get("count_total", 0),
        "trend": _trend(agg),
        "actors_count": len(agg.get("actors") or []),
        "sources": agg.get("sources", []),
        "first_seen": agg.get("first_seen"),
        "last_seen": agg.get("last_seen"),
        "fixed_at": agg.get("fixed_at"),
        "suppressed_total": agg.get("suppressed_total", 0),
        "suppressed_7d": _suppressed_7d(agg),
        "burst": bool(agg.get("burst")),
        "burst_ts": agg.get("burst_ts"),
        "endpoints": dict(sorted((agg.get("endpoints") or {}).items(),
                                 key=lambda kv: (-kv[1], kv[0]))[:20]),
    }


# ── Raw-скан примеров: newest-first, пре-фильтр, ранний выход, кап ──

def _scan_raw_examples(
    sink: Path,
    matched: list[tuple[str, dict[str, Any]]],
    flt: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Примеры для сигнатур из matched (newest-first, P2-new-2).

    Строчный пре-фильтр до json.loads; ранний выход — все сигнатуры набрали
    examples_limit; кап 64 МБ → raw_scan_truncated (P2-new-1) + warning.
    """
    targets = {sig for sig, _ in matched}
    need = len(targets) * flt["examples_limit"] if targets else 0
    meta = {"raw_files_scanned": 0, "raw_bytes_scanned": 0,
            "raw_scan_truncated": False, "warnings": []}
    if not need:
        return [], meta
    # окно: ≤ 30d (P2-3); без period/since — последние 24h (схема: default)
    days = flt["window_days"] if flt["window_days"] is not None else 1
    days = min(days, RAW_SCAN_MAX_DAYS)
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    buckets: dict[str, list[dict[str, Any]]] = {sig: [] for sig in targets}
    raw_dir = sink / "events" / "raw"
    try:
        files = sorted((f for f in raw_dir.glob("*.jsonl") if f.stem >= cutoff_date),
                       key=lambda f: f.stem, reverse=True)  # newest-first
    except OSError as exc:
        meta["warnings"].append(f"raw scan aborted: {exc.__class__.__name__}")
        return [], meta
    for path in files:
        if meta["raw_scan_truncated"] or all(
            len(buckets[s]) >= flt["examples_limit"] for s in buckets
        ):
            break  # ранний выход: примеры набраны / кап достигнут
        try:
            size = path.stat().st_size
            meta["raw_bytes_scanned"] += size
            meta["raw_files_scanned"] += 1
            if meta["raw_bytes_scanned"] > RAW_SCAN_CAP_BYTES:
                meta["raw_scan_truncated"] = True
                meta["warnings"].append(
                    "raw scan truncated by 64MB cap: examples beyond the cap "
                    "were NOT checked")
                break
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()  # newest-last в файле → идём с конца
            for line in reversed(lines):
                if flt["query"] and flt["query"] not in line.lower():
                    continue  # строчный пре-фильтр до json.loads (P2-new-2)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sig = ev.get("signature")
                if sig not in buckets or len(buckets[sig]) >= flt["examples_limit"]:
                    continue
                if not flt["include_audit"] and ev.get("marker") == "ERRORS_QUERY":
                    continue
                if flt["source"] and ev.get("source") != flt["source"]:
                    continue
                buckets[sig].append({
                    "ts": ev.get("ts"),
                    "source": ev.get("source"),
                    "container": ev.get("container"),
                    "level": ev.get("level"),
                    "marker": ev.get("marker"),
                    "actor_id": ev.get("actor_id"),  # namespace коллектора (P2-2)
                    "message": _truncate(mask_output(str(ev.get("message", "")))),
                    "suppressed_count": ev.get("suppressed_count"),  # 008: гвард-перенос
                    "sampled": ev.get("sampled"),
                })
                if all(len(buckets[s]) >= flt["examples_limit"] for s in buckets):
                    break  # ранний выход внутри файла
        except OSError as exc:
            meta["warnings"].append(f"raw file skipped: {path.name} ({exc.__class__.__name__})")
    examples = [ev for sig in targets for ev in buckets[sig]]
    return examples, meta


# ── Audit обращений (P1-1а: формат БЕЗ значений) ───────────

def _audit(view: str, flt: dict[str, Any], query_raw: str, sig_given: bool,
           results: int, dur_ms: float, key_hash: str) -> None:
    """stdout-маркер → docker logs → errors_collect (routine-P3, §5).

    Без query-текста (P1-1а): только q_len + q_hash=sha256[:8] — корреляция
    повторов без раскрытия текста запроса агента.
    """
    logger.info(
        "[ERRORS_QUERY] view=%s prio=%s src=%s period=%s q_len=%d q_hash=%s "
        "sig=%s results=%d dur=%.1fms key=%s",
        view,
        ",".join(flt["priority"]) if flt["priority"] else "-",
        flt["source"] or "-",
        _period_label(flt),
        len(query_raw),
        hashlib.sha256(query_raw.encode()).hexdigest()[:8] if query_raw else "-",
        sig_given,
        results,
        dur_ms,
        key_hash or "none",
    )


def _period_label(flt: dict[str, Any]) -> str:
    if flt["window_days"] is not None:
        return f"{flt['window_days']}d"
    if flt["cutoff"] is not None:
        return "since"
    return "-"


# ── Оркестрация (sync-ядро; async-обёртка — P1-3) ──────────

def _run(params: dict[str, Any], key_hash: str) -> dict[str, Any]:
    t0 = time.monotonic()
    sink = Path(settings.ERRORS_SINK_DIR)
    base: dict[str, Any] = {"sink_available": True, "view": params.get("view", "aggregates")}
    err = _parse_filters(params)
    if isinstance(err, str):
        return {"error": err}
    flt: dict[str, Any] = err  # type: ignore[assignment]
    available, reason, hint = _probe_sink(sink)
    if not available:
        # graceful (P2-6): 200 + sink_available=false + причина — агент
        # сообщает оператору, не гадает «ошибок нет»
        return {"sink_available": False, "error": reason, "hint": hint,
                "meta": {"sink_dir": str(sink)}}
    aggs, warnings = _load_aggregates(sink)
    matched = _filter_aggregates(aggs, flt)
    matched_limited = matched[: flt["limit"]]
    result: dict[str, Any] = {
        **base,
        "filters_applied": {
            "priority": flt["priority"] or None,
            "source": flt["source"],
            "signature": flt["signature"],
            "query": bool(flt["query"]),
            "include_audit": flt["include_audit"],
            "status": flt["status"],
            "period": params.get("period"),
            "since": params.get("since"),
            "limit": flt["limit"],
            "examples_limit": flt["examples_limit"],
        },
        "aggregates": [],
        "examples": [],
        "meta": {
            "sink_dir": str(sink),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "signatures_matched": len(matched_limited),
            "signatures_truncated": len(matched) > flt["limit"],
            "examples_returned": 0,
            "raw_files_scanned": 0,
            "raw_bytes_scanned": 0,
            "raw_scan_truncated": False,
            "warnings": warnings,
        },
    }
    if flt["view"] in ("aggregates", "both"):
        result["aggregates"] = [_render_aggregate(s, a) for s, a in matched_limited]
    if flt["view"] in ("examples", "both"):
        examples, raw_meta = _scan_raw_examples(sink, matched_limited, flt)
        result["examples"] = examples
        result["meta"]["examples_returned"] = len(examples)
        result["meta"]["raw_files_scanned"] = raw_meta["raw_files_scanned"]
        result["meta"]["raw_bytes_scanned"] = raw_meta["raw_bytes_scanned"]
        result["meta"]["raw_scan_truncated"] = raw_meta["raw_scan_truncated"]
        result["meta"]["warnings"] += raw_meta["warnings"]
    if not matched:
        result["meta"]["hint"] = (
            "коллектор ещё не писал в окно фильтра / фильтр пуст: "
            "попробуй view=aggregates без period, затем расширь окно")
    dur_ms = (time.monotonic() - t0) * 1000
    result["meta"]["duration_ms"] = round(dur_ms, 1)
    _audit(str(params.get("view", "aggregates")), flt,
           str(params.get("query") or ""), bool(flt["signature"]),
           len(matched_limited), dur_ms, key_hash)
    return result


async def errors_query(params: dict[str, Any], app_state: Any) -> dict[str, Any]:
    """MCP handler: read-only запрос к sink ошибок (admin-only, §4 спеки)."""
    auth = params.get("_auth") if isinstance(params, dict) else None
    key_hash = getattr(auth, "key_hash", "") or ""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run, params, key_hash)
