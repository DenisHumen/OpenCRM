#!/bin/sh
set -e

# Страховочная копия перед миграциями — та, к которой возвращаются, если
# миграция испортила данные.
#
# **Копия именуется ревизией, с которой уходим, и не перезаписывается.** Раньше
# файл был один (`.pre-migrate`) и обновлялся на КАЖДОМ старте контейнера —
# включая перезапуски, перезагрузку машины и цикл рестартов после падения.
# Схема такая: миграция что-то испортила, контейнер перезапустился (сам или
# руками), и копия, снятая ДО миграции, затёрлась состоянием ПОСЛЕ неё. К
# моменту, когда беду замечали, возвращаться было уже не к чему.
#
# Теперь копия снимается только когда есть куда мигрировать, называется по
# текущей ревизии и, если такая уже есть, не трогается: повторный старт на той
# же ревизии ничего не переписывает.
DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
if [ -f "$DB_FILE" ]; then
    # Ревизию читаем прямо из базы, а не через `alembic current`: тот поднимает
    # всё окружение приложения ради одной строки, а здесь важна скорость старта.
    CURRENT="$(sqlite3 "$DB_FILE" "SELECT version_num FROM alembic_version" 2>/dev/null || echo none)"
    [ -n "$CURRENT" ] || CURRENT=none
    SNAPSHOT="$DB_FILE.pre-migrate-$CURRENT"
    if [ -f "$SNAPSHOT" ]; then
        echo "[opencrm] копия перед миграциями уже есть: $SNAPSHOT"
    else
        sqlite3 "$DB_FILE" ".backup '$SNAPSHOT'"
        echo "[opencrm] снята копия перед миграциями: $SNAPSHOT"
    fi
    # Держим последние пять: копии тяжёлые, а нужны только недавние — к
    # позапрошлогодней ревизии никто не возвращается.
    ls -1t "$DB_FILE".pre-migrate-* 2>/dev/null | tail -n +6 | xargs -r rm -f
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
