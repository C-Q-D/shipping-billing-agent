# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src/ ./src/
COPY docs/workflow/contracts/ ./docs/workflow/contracts/
COPY examples/ ./examples/
COPY tests/fixtures/ ./tests/fixtures/

RUN uv sync --frozen --no-dev

COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN useradd --create-home --shell /bin/bash appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app/data

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    ALLOW_REMOTE_BIND=true \
    APP_DATA_DIR=/app/data \
    SQLITE_DB_PATH=/app/data/shipping_billing.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["shipping-billing-agent", "serve", "--host", "0.0.0.0", "--port", "8000", "--ocr-mode", "REMOTE_WITH_LOCAL_FALLBACK"]
