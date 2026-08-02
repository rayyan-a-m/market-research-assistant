FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for crawl4ai's headless Chromium (Stage 1 fetcher)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium + OS deps for Playwright (crawl4ai's engine)
RUN python -m playwright install --with-deps chromium

COPY app ./app

EXPOSE 8000

# ENV should be "production" on Azure Container Apps to disable /docs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
