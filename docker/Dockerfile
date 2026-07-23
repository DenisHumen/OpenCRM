# --- этап 1: сборка фронтенда CRM ---
FROM node:22-alpine AS frontend
WORKDIR /build
COPY web/frontend/crm/package.json web/frontend/crm/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/frontend/crm/ ./
RUN npm run build

# --- этап 2: приложение ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# ffmpeg — постеры и метаданные видео
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY config/ config/
COPY core/ core/
COPY database/ database/
COPY web/ web/
COPY scripts/ scripts/
RUN pip install .

COPY --from=frontend /build/dist web/frontend/crm/dist

RUN useradd -m opencrm && mkdir -p /app/data /app/storage && chown -R opencrm /app/data /app/storage
USER opencrm

EXPOSE 8000
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
