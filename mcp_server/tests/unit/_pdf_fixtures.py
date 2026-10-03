"""Общая PDF-фикстура для bibliography-тестов (Ф2, trace code-2026-10-02-bibliography).

``_build_minimal_pdf`` изначально жил в test_pdf_locator_extractor (Ф2a);
вынесен сюда (Ф2b4.E), чтобы устранить хрупкий кросс-импорт
``from tests.unit.test_pdf_locator_extractor import _build_minimal_pdf``
в test_pdf_producer_spans — тестовые модули не зависят друг от друга,
фикстура-хелпер импортируется из безтестового модуля ``_pdf_fixtures``
(pytest его не коллеклектит: не ``test_*.py``).

Сборка: минимальный ручной билдер байтов с программно вычисляемыми
byte-offsets и корректным xref — без reportlab / pikepdf / weasyprint
и без сети (air-gap). Единственная зависимость — pdfplumber, которым
парсит и сам экстрактор (round-trip через тот же парсер).
"""

from __future__ import annotations


def _build_minimal_pdf(pages: list[list[str]]) -> bytes:
    """Собрать минимальный валидный PDF байтами: N страниц, текст = строки Tj.

    Подход (задокументирован сознательно, см. докстринг модуля): объекты
    PDF (Catalog/Pages/Page/Contents/Font) сериализуются вручную, смещение
    каждого объекта и позиция xref вычисляются программно при накоплении
    bytearray — xref всегда корректен, парсеру не нужны восстановительные
    эвристики. Кириллицу в текст не кладём: BaseFont /Helvetica + latin-1
    content stream (ru-локаль проверяется на Locator.display, не в PDF).
    """
    n = len(pages)
    page_nums = [3 + i * 2 for i in range(n)]
    content_nums = [4 + i * 2 for i in range(n)]
    font_num = 3 + n * 2

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{p} 0 R" for p in page_nums)
            + f"] /Count {n} >>"
        ).encode(),
    }
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 12 Tf", "72 720 Td"]
        for line in lines:
            safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            ops.append(f"({safe}) Tj")
            ops.append("0 -14 Td")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects[page_nums[i]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {content_nums[i]} 0 R >>"
        ).encode()
        objects[content_nums[i]] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    max_obj = max(objects)
    out += f"xref\n0 {max_obj + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, max_obj + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)
