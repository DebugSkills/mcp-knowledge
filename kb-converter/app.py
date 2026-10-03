"""kb-converter — sidecar-канонизатор документных форматов → canonical PDF.

bibliography Ф1 (план §4): детерминированный canonical-PDF рендер.
Форматы (движок): docx → LibreOffice-headless; md/txt/html → WeasyPrint; epub → WeasyPrint-spine.

Безопасность (план §4.5):
- Egress-deny: WeasyPrint url_fetcher разрешает ТОЛЬКО file:// под каталогом задачи (realpath);
  http/https/ftp → отказ (внешний ресурс пропускается, конвертация продолжается).
- Zip-bomb: кумулятивный потоковый счётчик распаковки (CONVERTER_MAX_UNPACKED_BYTES).
- LibreOffice: профиль с Link/Update/Mode=never; макросы не исполняются (headless).

Детерминизм (план §4.4): pikepdf пост-нормализация (strip /ID, /Metadata, Producer) —
последний шаг конвейера (после LO-вывода и pypdf-merge).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="kb-converter")

MAX_UNPACKED = int(os.environ.get("CONVERTER_MAX_UNPACKED_BYTES", "1073741824"))
MAX_PAGES = int(os.environ.get("CONVERTER_MAX_PAGES", "2000"))

# Фикс. CSS для WeasyPrint (A4, поля 2 см, DejaVu Serif 11pt) — детерминизм §4.4.
_BASE_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: 'DejaVu Serif', serif; font-size: 11pt; line-height: 1.4; }
pre { font-family: 'DejaVu Sans Mono', monospace; font-size: 9pt; white-space: pre-wrap; }
"""


class ConversionError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message or code)


def _tool_version() -> str:
    try:
        out = subprocess.run(["soffice", "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "libreoffice-unknown"


def _params_hash() -> str:
    return hashlib.sha256(_BASE_CSS.encode()).hexdigest()[:16]


# ── Egress-deny url_fetcher (план §4.5) ─────────────────────

def _make_url_fetcher(task_dir: Path) -> Callable:
    """WeasyPrint fetcher: ТОЛЬКО file:// под task_dir (realpath-контейнмент)."""
    from weasyprint import default_url_fetcher

    task_root = task_dir.resolve()

    def fetcher(url, timeout=10, ssl_context=None):
        if url.startswith(("http://", "https://", "ftp://")):
            return {"string": b"", "mime_type": "text/plain", "encoding": "utf-8"}
        if url.startswith("file://"):
            import urllib.parse
            p = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).resolve()
            if task_root in p.parents or p == task_root:
                return default_url_fetcher(url, timeout, ssl_context)
            return {"string": b"", "mime_type": "text/plain", "encoding": "utf-8"}
        return default_url_fetcher(url, timeout, ssl_context)

    return fetcher


# ── Zip-bomb guard (план §4.6) ──────────────────────────────

def _safe_extract_zip(data: bytes, task_dir: Path) -> None:
    """Кумулятивный потоковый счётчик распаковки zip-энтрис."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if total > MAX_UNPACKED:
                raise ConversionError("unpacked_size_limit", f"unpacked > {MAX_UNPACKED} bytes")
        zf.extractall(task_dir)


# ── pikepdf пост-нормализация (план §4.4) ───────────────────

def _normalize_pdf(pdf_bytes: bytes) -> bytes:
    """Strip /ID, /Metadata (XMP), Producer/CreationDate — детерминизм."""
    import pikepdf

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/ID" in pdf.trailer:
            del pdf.trailer["/ID"]
        if "/Metadata" in pdf.trailer.get("/Root", {}):
            del pdf.trailer["/Root"]["/Metadata"]
        meta = pdf.docinfo
        for key in ("/Producer", "/CreationDate", "/ModDate"):
            try:
                del meta[key]
            except Exception:  # noqa: BLE001
                pass
        out = io.BytesIO()
        pdf.save(out, static_id=True)
        return out.getvalue()


# ── Движки ──────────────────────────────────────────────────

def _convert_docx(data: bytes, task_dir: Path) -> bytes:
    src = task_dir / "input.docx"
    src.write_bytes(data)
    out_dir = task_dir / "out"
    out_dir.mkdir(exist_ok=True)
    env = {
        "HOME": str(task_dir),
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "SAL_USE_VCLPLUGIN": "svp",
    }
    proc = subprocess.run(
        ["soffice", "--headless", "--norestore", "--convert-to", "pdf",
         "--outdir", str(out_dir), str(src)],
        capture_output=True, timeout=120, env={**os.environ, **env},
    )
    if proc.returncode != 0:
        raise ConversionError("conversion_failed", proc.stderr.decode(errors="replace")[:300])
    pdf_path = out_dir / "input.pdf"
    if not pdf_path.exists():
        raise ConversionError("conversion_failed", "soffice produced no PDF")
    return _normalize_pdf(pdf_path.read_bytes())


def _weasyprint_html(html: str, task_dir: Path, base_url: str | None = None) -> bytes:
    from weasyprint import HTML

    return HTML(string=html, base_url=base_url,
                url_fetcher=_make_url_fetcher(task_dir)).write_pdf(
        stylesheets=None,
    )


def _convert_md(data: bytes, task_dir: Path) -> bytes:
    import markdown
    html = markdown.markdown(data.decode("utf-8", errors="replace"))
    return _normalize_pdf(_weasyprint_html(_wrap_html(html), task_dir))


def _convert_txt(data: bytes, task_dir: Path) -> bytes:
    import html as _html
    text = _html.escape(data.decode("utf-8", errors="replace"))
    return _normalize_pdf(_weasyprint_html(_wrap_html(f"<pre>{text}</pre>"), task_dir))


def _convert_html(data: bytes, task_dir: Path) -> bytes:
    return _normalize_pdf(_weasyprint_html(_wrap_html(data.decode("utf-8", errors="replace")), task_dir))


def _convert_epub(data: bytes, task_dir: Path) -> bytes:
    _safe_extract_zip(data, task_dir)
    # spine: упрощённый порядок XHTML по имени (реальный spine — из container.xml).
    xhtml_files = sorted(task_dir.rglob("*.xhtml")) or sorted(task_dir.rglob("*.html"))
    if not xhtml_files:
        raise ConversionError("conversion_failed", "epub: no XHTML spine")
    from pypdf import PdfMerger

    merger = PdfMerger()
    for xf in xhtml_files:
        html = xf.read_text(encoding="utf-8", errors="replace")
        pdf = _weasyprint_html(html, task_dir, base_url=str(xf.parent))
        merger.append(io.BytesIO(pdf))
    out = io.BytesIO()
    merger.write(out)
    merger.close()
    return _normalize_pdf(out.getvalue())


def _wrap_html(body: str) -> str:
    return f"<html><head><style>{_BASE_CSS}</style></head><body>{body}</body></html>"


ENGINES: dict[str, Callable] = {
    "docx": _convert_docx,
    "md": _convert_md,
    "txt": _convert_txt,
    "html": _convert_html,
    "epub": _convert_epub,
}

TOOLS = {
    "docx": "libreoffice-headless",
    "md": "weasyprint",
    "txt": "weasyprint",
    "html": "weasyprint",
    "epub": "weasyprint-spine",
}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/convert")
async def convert(request: Request):
    fmt = request.query_params.get("format", "")
    engine = ENGINES.get(fmt)
    if engine is None:
        return JSONResponse(status_code=400, content={"error": f"unsupported format: {fmt}", "code": "unsupported_format"})
    data = await request.body()
    if not data:
        return JSONResponse(status_code=400, content={"error": "empty body", "code": "empty_body"})

    task_dir = Path(tempfile.mkdtemp(prefix="conv_", dir="/tmp"))
    try:
        pdf = engine(data, task_dir)
    except ConversionError as exc:
        return JSONResponse(status_code=422, content={"error": exc.message, "code": exc.code})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)[:300], "code": "conversion_failed"})
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)

    headers = {
        "X-Tool": TOOLS[fmt],
        "X-Tool-Version": _tool_version() if fmt == "docx" else _weasyprint_version(),
        "X-Params-Hash": _params_hash(),
    }
    return Response(content=pdf, media_type="application/pdf", headers=headers)


def _weasyprint_version() -> str:
    try:
        import weasyprint
        return getattr(weasyprint, "__version__", "weasyprint")
    except Exception:  # noqa: BLE001
        return "weasyprint"


if __name__ == "__main__":
    bind = os.environ.get("CONVERTER_BIND", "0.0.0.0:8660")
    host, _, port = bind.partition(":")
    uvicorn.run(app, host=host, port=int(port))
