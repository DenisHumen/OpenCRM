#!/bin/sh
# Ежедневный бэкап OpenCRM: консистентная копия SQLite + архив storage.
# Хранение: 7 ежедневных + 4 еженедельных (воскресные).
# Cron (на хосте):  0 3 * * *  /path/to/OpenCRM/scripts/backup.sh
# Для Docker-томов запускать внутри контейнера app либо примонтировав тома.
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

# 3) воскресный бэкап дублируем в weekly
if [ "$DOW" = "7" ]; then
    cp "$BACKUP_DIR/daily/db-$STAMP.db" "$BACKUP_DIR/weekly/"
    cp "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" "$BACKUP_DIR/weekly/"
fi

# 4) ротация: daily > 7 дней, weekly > 28 дней
find "$BACKUP_DIR/daily" -type f -mtime +7 -delete
find "$BACKUP_DIR/weekly" -type f -mtime +28 -delete

# 5) (опционально) выгрузка наружу — раскомментировать и настроить:
# age -r "$OPENCRM_BACKUP_PUBKEY" -o "/tmp/db-$STAMP.db.age" "$BACKUP_DIR/daily/db-$STAMP.db"
# rclone copy "/tmp/db-$STAMP.db.age" remote:opencrm-backups/

echo "backup done: $STAMP"
