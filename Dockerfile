FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/var/cache/document-forensics

WORKDIR /opt/application

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[pptx,ocr,test]"

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /workspace "${XDG_CACHE_HOME}" \
    && chown -R app:app /opt/application /workspace "${XDG_CACHE_HOME}"

USER app
WORKDIR /workspace

CMD ["pptx-forensics", "--help"]
