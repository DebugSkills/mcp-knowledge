"""Canonicalizer — extension point + sidecar-клиент (bibliography Ф1, план §4).

Реестр движков canonical-PDF (формат → движок):
  docx → libreoffice-headless · md/txt/html → weasyprint · epub → weasyprint-spine.
media/код/таблицы/url — вне PDF-оси (outside_pdf_axis).

Канонизация делегируется sidecar'у `kb-converter` (loopback :8660) через HTTP.
Fail-closed: недоступен/таймаут/non-zero → CanonicalizationError(reason, ...);
original уже в сторе — отказ конвертера не теряет данные (план §4.1 шаг 1-2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("mcp_knowledge.content.canonicalizer")

# ── Engine registry (extension point по образцу content/registry.py) ──
# Новый документный формат = новый движок + 1 строка здесь.
CANONICALIZER_ENGINES: dict[str, str] = {
    "docx": "libreoffice-headless",
    "md": "weasyprint",
    "txt": "weasyprint",
    "html": "weasyprint",
    "epub": "weasyprint-spine",
}

# Таймауты по формату (план §4.6).
_TIMEOUTS: dict[str, float] = {
    "docx": 120.0,
    "md": 60.0,
    "txt": 60.0,
    "html": 60.0,
    "epub": 180.0,
}


class CanonicalizationError(Exception):
    """Отказ канонизации — reason-код (fail-closed, план §3.4 reason-классификация)."""

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


def is_document_format(format_value: str | None) -> bool:
    """Документный формат с canonical-PDF осью (иначе — вне PDF-оси)."""
    return format_value in CANONICALIZER_ENGINES


def engine_for(format_value: str) -> str:
    """Движок для документного формата (KeyError для non-document)."""
    return CANONICALIZER_ENGINES[format_value]


async def canonicalize(data: bytes, format_value: str, *, converter_url: str | None = None) -> dict[str, Any]:
    """HTTP-вызов sidecar → {pdf_bytes, tool, tool_version, params_hash}.

    Контракт sidecar (kb-converter):
      POST {url}/convert?format=<fmt>  (body = raw bytes, Content-Type: application/octet-stream)
      → 200: binary PDF + заголовки X-Tool / X-Tool-Version / X-Params-Hash
      → 4xx/5xx: JSON {error, code}

    Raises:
        CanonicalizationError: reason ∈ {converter_unavailable, conversion_failed,
        conversion_timeout, input_too_large, output_too_large, unpacked_size_limit}.
    """
    url = converter_url or settings.CONVERTER_URL
    if not url:
        raise CanonicalizationError("converter_unavailable", "CONVERTER_URL is not set")

    if len(data) > settings.CONVERTER_MAX_INPUT_BYTES:
        raise CanonicalizationError("input_too_large", f"{len(data)} bytes > max input")

    timeout = _TIMEOUTS.get(format_value, 60.0)
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: _convert_sync(url, format_value, data, timeout),
        )
    except CanonicalizationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CanonicalizationError("converter_unavailable", str(exc)) from exc


def _convert_sync(url: str, format_value: str, data: bytes, timeout: float) -> dict[str, Any]:
    """Синхронный HTTP-вызов (исполняется в executor, не блокирует event loop)."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url.rstrip('/')}/convert",
                params={"format": format_value},
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
    except httpx.TimeoutException as exc:
        raise CanonicalizationError("conversion_timeout", str(exc)) from exc
    except httpx.TransportError as exc:
        raise CanonicalizationError("converter_unavailable", str(exc)) from exc

    if resp.status_code != 200:
        try:
            body = resp.json()
            code = body.get("code", "conversion_failed")
            error = body.get("error", "")
        except Exception:  # noqa: BLE001
            code, error = "conversion_failed", resp.text[:200]
        raise CanonicalizationError(code, error)

    pdf_bytes = resp.content
    if len(pdf_bytes) > settings.CONVERTER_MAX_OUTPUT_BYTES:
        raise CanonicalizationError("output_too_large", f"{len(pdf_bytes)} bytes > max output")
    return {
        "pdf_bytes": pdf_bytes,
        "tool": resp.headers.get("X-Tool", engine_for(format_value)),
        "tool_version": resp.headers.get("X-Tool-Version"),
        "params_hash": resp.headers.get("X-Params-Hash"),
    }
