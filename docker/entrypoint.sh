#!/bin/sh
set -e

# страховочная копия SQLite перед миграциями (если файл уже есть)
DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
if [ -f "$DB_FILE" ]; then
    sqlite3 "$DB_FILE" ".backup '$DB_FILE.pre-migrate'"
fi

python -m alembic upgrade head

# --no-proxy-headers: uvicorn НЕ переписывает client по X-Forwarded-For. Определение
# IP клиента (rate-limit подбора PIN, хэш IP просмотров) целиком за приложением
# (core client_ip + OPENCRM_TRUSTED_PROXY_HOPS) — иначе заголовок клиента подделывается.
exec python -m uvicorn web.main:app --host 0.0.0.0 --port 8000 \
    --no-proxy-headers --workers "${OPENCRM_WORKERS:-1}"
