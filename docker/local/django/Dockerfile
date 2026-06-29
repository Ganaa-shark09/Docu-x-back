FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    netcat-openbsd \
    gettext \
    tesseract-ocr \
    poppler-utils \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt /app/requirements/base.txt
COPY requirements/local.txt /app/requirements/local.txt

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /app/requirements/local.txt

COPY . /app

RUN chmod +x /app/scripts/entrypoint.sh
