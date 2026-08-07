#!/bin/sh
# Ежедневный бэкап OpenCRM: база, файлы, ключ шифрования — и проверка того,
# что получилось.
#
# Хранение: 7 ежедневных + 4 еженедельных (воскресные).
# Cron (на хосте):  0 3 * * *  /path/to/OpenCRM/scripts/backup.sh
# Для Docker-томов запускать внутри контейнера app либо примонтировав тома.
#
# **Копия включает ключ шифрования, и это осознанно.** Без OPENCRM_SECRET_KEY
# восстановленная база наполовину мертва: пароли почтовых ящиков зашифрованы им
# и не выводятся из данных (core/security/secretbox.py). Ключ, оставшийся только
# в config/.env на сгоревшем сервере, — это потеря навсегда, а не неудобство.
# Файл кладётся с правами 600 и ровно с тем, чего нельзя восстановить иначе.
#
# **Копия проверяется сразу после снятия.** Копии, которые никто не проверял, —
# это не копии, а надежда: `.backup`, оборванный на полном диске, оставляет
# файл, который выглядит как база и читается до первой битой страницы. Узнавать
# об этом в день, когда база понадобилась, — слишком поздно.
set -eu

DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
STORAGE_DIR="${OPENCRM_STORAGE_DIR:-/app/storage}"
BACKUP_DIR="${OPENCRM_BACKUP_DIR:-/app/data/backups}"

STAMP="$(date +%Y-%m-%d)"
DOW="$(date +%u)"   # 7 = воскресенье
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# 1) SQLite: .backup даёт консистентную копию на горячую
sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/daily/db-$STAMP.db'"

# 2) storage: полный архив (файлы неизменяемые, дельта не критична для MVP)
tar -czf "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" -C "$STORAGE_DIR" .

# 3) ключ шифрования и соль хэша IP — то, чего нет в базе и что не выводится
#    из неё ничем. Соль нужна, чтобы после восстановления просмотры витрины
#    считались теми же людьми, а не начались с чистого листа.
SECRET_FILE="$BACKUP_DIR/daily/secret-$STAMP.env"
umask 077
{
    echo "# Ключи OpenCRM на $STAMP. Без них восстановление неполное:"
    echo "# пароли почтовых ящиков зашифрованы OPENCRM_SECRET_KEY и без него"
    echo "# не расшифровываются НИКОГДА."
    echo "OPENCRM_SECRET_KEY=${OPENCRM_SECRET_KEY:-}"
    echo "OPENCRM_IP_HASH_SALT=${OPENCRM_IP_HASH_SALT:-}"
} > "$SECRET_FILE"
chmod 600 "$SECRET_FILE"

# 4) воскресный бэкап дублируем в weekly
if [ "$DOW" = "7" ]; then
    cp "$BACKUP_DIR/daily/db-$STAMP.db" "$BACKUP_DIR/weekly/"
    cp "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" "$BACKUP_DIR/weekly/"
    cp "$SECRET_FILE" "$BACKUP_DIR/weekly/"
fi

# 5) ротация: daily > 7 дней, weekly > 28 дней
find "$BACKUP_DIR/daily" -type f -mtime +7 -delete
find "$BACKUP_DIR/weekly" -type f -mtime +28 -delete

# 6) проверка того, что получилось. Отказ здесь — отказ всего скрипта: копия,
#    о негодности которой не сказали, хуже отсутствия копии, потому что на неё
#    рассчитывают.
python -m scripts.verify_backup \
    "$BACKUP_DIR/daily/db-$STAMP.db" \
    "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" \
    "$SECRET_FILE"

# 7) (опционально) выгрузка наружу — раскомментировать и настроить.
#    Копия на том же диске, что и база, спасает от ошибки человека, но не от
#    смерти диска. Ключ шифруется отдельно и уезжает вместе с базой.
# age -r "$OPENCRM_BACKUP_PUBKEY" -o "/tmp/db-$STAMP.db.age" "$BACKUP_DIR/daily/db-$STAMP.db"
# rclone copy "/tmp/db-$STAMP.db.age" remote:opencrm-backups/

echo "backup done: $STAMP"
