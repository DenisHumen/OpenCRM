#!/bin/sh
set -e

# страховочная копия SQLite перед миграциями (если файл уже есть)
DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
if [ -f "$DB_FILE" ]; then
    sqlite3 "$DB_FILE" ".backup '$DB_FILE.pre-migrate'"
fi

# Несколько рабочих процессов на SQLite не работают: писатель у неё один на всю
# базу, и два процесса не поднимаются вовсе — падают на создании схемы и посеве
# умолчаний с «database is locked». Проверено живьём. Приложение говорит об этом
# и само, но здесь значение известно точно, а сказать лучше до миграций, чем
# после трёх минут ожидания.
WORKERS="${OPENCRM_WORKERS:-1}"
DB_URL="${OPENCRM_DB_URL:-sqlite}"
if [ "$WORKERS" -gt 1 ] 2>/dev/null; then
    case "$DB_URL" in
        sqlite*)
            echo "OPENCRM_WORKERS=$WORKERS с базой SQLite не работает: SQLite допускает" >&2
            echo "одного писателя, и процессы не поднимутся. Оставьте OPENCRM_WORKERS=1" >&2
            echo "или переезжайте на MySQL (scripts/migrate_to_mysql.py)." >&2
            exit 1
            ;;
    esac
fi

python -m alembic upgrade head

# --no-proxy-headers: uvicorn НЕ переписывает client по X-Forwarded-For. Определение
# IP клиента (rate-limit подбора PIN, хэш IP просмотров) целиком за приложением
# (core client_ip + OPENCRM_TRUSTED_PROXY_HOPS) — иначе заголовок клиента подделывается.
exec python -m uvicorn web.main:app --host 0.0.0.0 --port 8000 \
    --no-proxy-headers --workers "$WORKERS"
