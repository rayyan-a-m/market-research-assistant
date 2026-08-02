FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Install browsers to a shared path instead of $HOME/.cache, so the image
    # can drop root without the runtime user losing track of Chromium.
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

# System deps for crawl4ai's headless Chromium (Stage 1 fetcher)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium + its OS-level deps (crawl4ai's engine). Must run as root for the
# apt side of --with-deps, which is why the user switch comes after.
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

COPY app ./app

# Run as an unprivileged user: a fetcher that renders attacker-controlled pages
# is the one process in this system you least want owning the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# ENV should be "production" on Azure Container Apps to disable /docs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
