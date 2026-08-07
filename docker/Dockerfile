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

# Признак «это развёртывание, а не запуск на ноутбуке». Зашит в образ, поэтому
# снаружи его не потерять: любой контейнер из этого образа — боевой или
# репетиционный, но не разработка.
#
# Нужен ровно для одного отказа. `config/.env` подключён как `env_file` с
# `required: false` (иначе первый запуск до установки не поднялся бы вовсе), и
# пропади он — compose стартует контейнер молча, а приложение поднимается на
# ЗНАЧЕНИЯХ ПО УМОЛЧАНИЮ: dev-режим, всем известный секретный ключ, подделка
# cookie PIN-доступа. Снаружи это выглядит как работающий сайт.
#
# С этим флагом приложение отличает «человек запустил uvicorn у себя» от
# «конфиг не доехал в контейнер» и во втором случае не поднимается.
ENV OPENCRM_DEPLOYED=1

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
# Этап тестов наследует `OPENCRM_DEPLOYED=1` от `app`, а тесты развёртыванием
# не являются: набор снимает флаг и сам (tests/conftest.py), но снять его здесь
# честнее — иначе любой запуск в этом образе мимо conftest вёл бы себя как
# боевой.
ENV OPENCRM_DEPLOYED=0
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
# git — ради тестов preflight'а: обновление однажды встало намертво на настоящих
# особенностях git (чужой владелец каталога, бит исполнения как «правка»), и
# подделкой команд такое не ловится. Без git здесь эти проверки пропускались бы
# ровно на воротах деплоя, то есть там, где однажды и прорвало. В образ `app`
# слой не едет — этап отдельный.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install pytest && python -m pytest
