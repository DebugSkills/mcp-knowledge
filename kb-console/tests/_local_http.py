"""Локальные HTTP-проверки тестов kb-console без env-прокси (trace code-2026-09-25-021).

Проблема: httpx по умолчанию читает окружение (trust_env=True), поэтому запрос
к localhost уходит на внешний HTTP(S)_PROXY; при снятом NO_PROXY это даёт
ConnectError [Errno 111] — тесты падают/флапают в зависимости от env, а в
air-gap контуре не работают вовсе.

Решение: (1) при импорте гарантируем localhost/127.0.0.1/::1 в NO_PROXY —
это видят и подпроцессы (они копируют os.environ); (2) local_get() явно
задаёт trust_env=False для прямых локальных GET.
"""
from __future__ import annotations

import os

import httpx

_LOCAL_HOSTS = "localhost,127.0.0.1,::1"

_existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
_merged = ",".join(p for p in (_existing, _LOCAL_HOSTS) if p)
os.environ["NO_PROXY"] = _merged
os.environ["no_proxy"] = _merged


def local_get(url: str, **kwargs) -> httpx.Response:
    """GET к локальному сервису с trust_env=False (env-прокси игнорируется)."""
    kwargs.setdefault("trust_env", False)
    return httpx.get(url, **kwargs)
