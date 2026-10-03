# kb-converter — sidecar-канонизатор документных форматов → canonical PDF.
# bibliography Ф1 (план §4.2/§4.5): LibreOffice-headless (docx) + WeasyPrint (md/txt/html) + spine (epub).
FROM python:3.11-slim

# pypi.org недоступен из build-контейнеров на этом хосте (SSL handshake timeout);
# зеркало Tencent проверено 2026-08-06 (паттерн mcp_server/Dockerfile).
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

WORKDIR /app

# libreoffice-writer (НЕ полный LO) для docx; системные зависимости WeasyPrint ≥53
# (pango/cairo/gdk-pixbuf/fontconfig); шрифты DejaVu/Liberation/Noto для детерминизма.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 fontconfig \
    fonts-dejavu-core fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --no-build-isolation --index-url ${PIP_INDEX_URL} -r requirements.txt

COPY app.py .

# Детерминизм (план §4.4): фикс. окружение + HOME=/tmp (LO first-run профиль в tmpfs).
ENV CONVERTER_BIND=0.0.0.0:8660
ENV HOME=/tmp
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV SAL_USE_VCLPLUGIN=svp
# Zip-bomb лимит (план §4.6): кумулятивный счётчик распаковки zip-энтрис.
ENV CONVERTER_MAX_UNPACKED_BYTES=1073741824

EXPOSE 8660

CMD ["python", "app.py"]
