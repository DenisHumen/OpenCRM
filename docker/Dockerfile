# --- этап 1: сборка фронтенда CRM ---
FROM node:22-alpine AS frontend
WORKDIR /build
COPY web/frontend/crm/package.json web/frontend/crm/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/frontend/crm/ ./
RUN npm run build

# --- этап 2: приложение ---
FROM python:3.12-slim AS app
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

# --- этап 3: тесты (в боевой образ не попадает) ---
# Автообновление гоняет их на новом коде до того, как тронет живой сайт:
#     docker build --target tests -f docker/Dockerfile .
# Красные тесты — ненулевой код возврата сборки, деплой не начинается.
# Отдельным этапом, а не в этапе `app`, чтобы pytest и сами тесты не ехали в
# продакшен; поэтому же compose собирает строго `target: app`.
FROM app AS tests
USER root
# deploy/ в боевой образ не едет (его код работает на хосте), но тесты
# автообновления живут в общем наборе — без него прогон падал бы на импорте.
COPY deploy/ deploy/
COPY tests/ tests/
# Обвязка развёртывания тоже проверяется тестами (tests/test_deploy_config.py):
# самые дорогие поломки здесь были не в коде, а в конфигах — nginx с забытым
# резолвером и секреты, запечённые в слой образа. Без этих файлов проверки
# молча пропускались бы ровно там, где они нужнее всего, — на воротах деплоя.
COPY docker/ docker/
COPY .dockerignore ./
RUN pip install pytest && python -m pytest
