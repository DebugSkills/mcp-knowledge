#!/usr/bin/env python3
"""Ф1-канарейка K+1 для LLM-шлюза LiteLLM (arch-2026-10-05-ai-workspace, инвариант I1).

Проба: удержать K стримящих запросов полки, затем K+1-й обязан получить
HTTP 429 c error.type="throttling_error" — это 429 САМОГО шлюза (router поднял
max_parallel_requests ДО провайдера, без очереди). 0x429 без пробы — не доказательство.

Fail-closed W==1: max_parallel_requests — cap PER-WORKER; при W>1 фактический
потолок = K*W и проба ничего не доказывает → ALARM и отказ ДО любых HTTP-проб.

Режимы запуска:
  host:    python3 scripts/gateway_canary.py --base-url http://… (нужен доступ к порту)
  in-ctr:  docker exec -i mcp-knowledge-litellm python3 - \
               --base-url http://127.0.0.1:4000 < scripts/gateway_canary.py
           (docker exec недоступен изнутри → W-ассерт сам падает в /proc-режим)

Exit codes: 0 = PASS (429 throttling_error, W==1) · 3 = W!=1 (fail-closed) ·
            4 = барьер не достигнут · 5 = K+1 не 429/не throttling_error · 1 = ошибка.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field

EXIT_ERROR = 1
EXIT_W_ASSERT = 3
EXIT_BARRIER = 4
EXIT_NO_429 = 5

# «Runtime-serving» процесс: litellm-сервер / uvicorn / gunicorn-воркер.
# Исключаются: сам сканер (ps/sh), gunicorn-мастер (родитель — не serving).
# Факт формата (проверен на пине 1.104.0, chainguard): pid1 =
#   «python3 …/litellm --config … --port 4000 --num_workers 1» — единственный serving-процесс.
_SERVING_RE = re.compile(r"(uvicorn|gunicorn|litellm)", re.IGNORECASE)
_NUM_WORKERS_RE = re.compile(r"--num_workers[= ](\d+)")


def _log(msg: str) -> None:
    print(f"[canary] {msg}", flush=True)


def _proc_cmdlines() -> list[str]:
    """Командные строки локальных процессов (in-container режим; /proc не лжёт)."""
    out: list[str] = []
    for d in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(d, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        cmd = raw.replace("\x00", " ").strip()
        if cmd:
            out.append(cmd)
    return out


def _ps_cmdlines(container: str) -> list[str] | None:
    """`docker exec <ctr> ps -eo args` (host-режим); None → exec недоступен."""
    try:
        res = subprocess.run(
            ["docker", "exec", container, "ps", "-eo", "args"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        _log(f"docker exec недоступен ({exc.__class__.__name__}) → /proc-режим")
        return None
    if res.returncode != 0:
        _log(f"docker exec rc={res.returncode}: {res.stderr.strip()[:200]} → /proc-режим")
        return None
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def count_serving_workers(cmdlines: Sequence[str]) -> tuple[int, list[str]]:
    """Число runtime-serving воркеров + совпавшие командные строки (прозрачность)."""
    matched: list[str] = []
    for cmd in cmdlines:
        if cmd.startswith(("ps ", "sh -c ")) or " ps -eo " in f" {cmd} ":
            continue
        if "gunicorn: master" in cmd:
            continue
        if _SERVING_RE.search(cmd):
            matched.append(cmd[:160])
    return len(matched), matched


def declared_num_workers(cmdlines: Sequence[str]) -> int | None:
    """Значение --num_workers из cmdline сервера (перекрётстная проверка W)."""
    for cmd in cmdlines:
        if _SERVING_RE.search(cmd) and not cmd.startswith(("ps ", "sh -c ")):
            m = _NUM_WORKERS_RE.search(cmd)
            if m:
                return int(m.group(1))
    return None


@dataclass
class Holder:
    """Удержатель слота: стрим-запрос с прочитанным заголовком ответа + первым чанком."""

    idx: int
    sock: socket.socket
    status: int = 0
    first_chunk: bytes = b""
    error: str | None = None
    released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if not self.released:
            self.released = True
            try:
                self.sock.close()
            except OSError:
                pass


def _open_stream(
    base_url: str, model: str, master_key: str, timeout: float
) -> tuple[socket.socket, int, bytes]:
    """POST /v1/chat/completions (stream=true) на сыром сокете → (sock, status, первый чанк)."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body = json.dumps(
        {
            "model": model,
            "stream": True,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Считай медленно от 1 до 50."}],
        }
    ).encode()
    req = (
        f"POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Bearer {master_key}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode() + body

    sock = socket.create_connection((host, port), timeout=timeout)
    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)  # type: ignore[assignment]
    sock.sendall(req)
    head = b""
    deadline = time.monotonic() + timeout
    while b"\r\n\r\n" not in head and time.monotonic() < deadline:
        part = sock.recv(4096)
        if not part:
            break
        head += part
    raw_head = head.decode("utf-8", "replace")
    status = int(raw_head.split(" ", 2)[1]) if " " in raw_head else 0
    body_start = head.split(b"\r\n\r\n", 1)
    chunk = body_start[1] if len(body_start) == 2 else b""
    return sock, status, chunk


def start_holder(idx: int, base_url: str, model: str, key: str, timeout: float) -> Holder:
    """Открыть стрим и оставить сокет живым (слот занят, пока сокет открыт)."""
    h = Holder(idx=idx, sock=socket.socket())
    try:
        sock, status, chunk = _open_stream(base_url, model, key, timeout)
        h.sock, h.status, h.first_chunk = sock, status, chunk
        if status != 200:
            h.error = f"holder#{idx}: HTTP {status}: {chunk[:300].decode('utf-8', 'replace')}"
            h.release()
        elif not chunk:
            h.error = f"holder#{idx}: 200 без тела (стрим не начался)"
            h.release()
    except OSError as exc:
        h.error = f"holder#{idx}: {exc.__class__.__name__}: {exc}"
        h.release()
    return h


def metrics_in_flight(base_url: str, key: str, deployment: str) -> tuple[str, float] | None:
    """In-flight gauge из /metrics; None — такая метрика версией не экспонируется."""
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/metrics", headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        _log(f"/metrics недоступен ({exc.__class__.__name__}) → барьер client-side")
        return None
    best: tuple[str, float] | None = None
    for ln in text.splitlines():
        if ln.startswith("#") or " " not in ln:
            continue
        name, _, val = ln.rpartition(" ")
        m = re.match(r"^([a-zA-Z_:]*in[_-]flight[a-zA-Z_:]*)", name)
        if not m:
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if best is None or deployment in name:
            best = (name, v)
    return best


def probe_k_plus_one(base_url: str, model: str, key: str, timeout: float) -> tuple[int, str]:
    """K+1-й запрос (не стрим — меньше шума в диагностике). → (status, body)."""
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"transport: {exc.__class__.__name__}: {exc}"


def classify_429(status: int, raw: str) -> str:
    """Атрибуция 429: gateway (наш throttle) vs upstream (провайдер) — по телу."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = {}
    err = obj.get("error", {}) if isinstance(obj, dict) else {}
    etype = str(err.get("type") or obj.get("type") or "")
    msg = str(err.get("message") or obj.get("detail") or raw)
    if etype == "throttling_error" or "max_parallel_request" in msg or "Rejected by" in msg:
        return "gateway-throttle"
    if etype in {"rate_limit_exceeded", "requests_per_minute_limit"} or "Rate limit" in msg:
        return "upstream-rate-limit"
    if "deepseek" in msg.lower() or "ollama" in msg.lower():
        return "upstream-other"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="Ф1 K+1 canary для LiteLLM (fail-closed W==1)")
    ap.add_argument("--k", type=int, default=1, help="слоты полки (= max_parallel_requests)")
    ap.add_argument("--model", default="local", help="deployment (по умолчанию local)")
    ap.add_argument("--base-url", default="http://127.0.0.1:4000")
    ap.add_argument("--master-key", default="", help="Bearer; по умолчанию $LITELLM_MASTER_KEY")
    ap.add_argument("--container", default="mcp-knowledge-litellm", help="W-ассерт (host-режим)")
    ap.add_argument("--connect-timeout", type=float, default=30.0)
    args = ap.parse_args()

    key = args.master_key or os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        _log("ALARM: master key пуст (--master-key / $LITELLM_MASTER_KEY)")
        return EXIT_ERROR

    # ── (a) fail-closed W==1 — ДО любых проб ──
    _log(f"W-ассерт: контейнер={args.container} (docker exec → fallback /proc)")
    cmdlines = _ps_cmdlines(args.container)
    if cmdlines is None:
        cmdlines = _proc_cmdlines()
    w, matched = count_serving_workers(cmdlines)
    for ln in matched:
        _log(f"  serving-proc: {ln}")
    declared = declared_num_workers(cmdlines)
    _log(f"W (runtime-serving) = {w}; --num_workers в cmdline = {declared}")
    if w != 1 or (declared is not None and declared != 1):
        _log(f"ALARM: W={w} != 1 (declared={declared}) — неподдерживаемая конфигурация "
             "(факт. cap = K*W); проба отменена")
        return EXIT_W_ASSERT

    # ── (b) удержать K слотов стримом + барьер in-flight==K ──
    _log(f"удерживаю K={args.k} стримящих запросов (model={args.model})…")
    holders: list[Holder] = []
    for i in range(args.k):
        h = start_holder(i, args.base_url, args.model, key, args.connect_timeout)
        holders.append(h)
        if h.error:
            _log(f"ALARM: {h.error}")
            for x in holders:
                x.release()
            return EXIT_BARRIER
        _log(f"  holder#{i}: 200, стрим идёт")
        time.sleep(0.3)  # разгон router-счётчика

    infl = metrics_in_flight(args.base_url, key, args.model)
    barrier = "client-side"
    if infl:
        _log(f"барьер /metrics: {infl[0]} = {infl[1]:.0f} (ожидалось {args.k})")
        barrier = "metrics"
        if int(infl[1]) != args.k:
            _log("ALARM: in-flight != K по метрике")
            for x in holders:
                x.release()
            return EXIT_BARRIER
    else:
        _log(f"барьер client-side: все K={args.k} держателей держат 200+стрим (in-flight=K)")

    # ── (c) K+1 → ожидаем 429 throttling_error (gateway, не upstream) ──
    _log("проба K+1…")
    status, raw = probe_k_plus_one(args.base_url, args.model, key, args.connect_timeout)
    _log(f"K+1: HTTP {status}")
    _log(f"K+1 body: {raw[:500]}")
    verdict = classify_429(status, raw)
    for x in holders:
        x.release()
    if status == 429 and verdict == "gateway-throttle":
        _log(f"PASS: 429 {verdict} — шлюз отдал переполнение слотов ДО провайдера (W={w}, барьер={barrier})")
        return 0
    _log(f"ALARM: verdict={verdict} (ожидался gateway-throttle 429 throttling_error)")
    return EXIT_NO_429


if __name__ == "__main__":
    sys.exit(main())
