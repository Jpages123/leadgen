FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system deps for WeasyPrint (PDF), psycopg, Playwright, and mockup build pipeline
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq-dev \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    git \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22 + pnpm + wrangler for mockup build pipeline
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g pnpm@9 wrangler

# Two-stage COPY: pip-install only re-runs when pyproject changes.
COPY pyproject.toml .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir ".[dev]"

# Playwright Chromium must match the Python lib version EXACTLY.
# Both are pinned together: pyproject.toml has playwright==1.59.0 (as of
# 2026-07-02). Bump both together. The runtime hook in
# docker-compose.yml worker/beat commands additionally calls
# `python -m playwright install chromium` on every container start, so
# even post-build pip upgrades still self-heal.
RUN python -m playwright install chromium --with-deps

COPY . .

# Default: run the FastAPI app
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
