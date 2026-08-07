#!/bin/sh
# Восстановление OpenCRM из бэкапа.
# Использование: restore.sh <db-YYYY-MM-DD.db> <storage-YYYY-MM-DD.tar.gz>
# ВНИМАНИЕ: перезаписывает текущую базу и storage. Останавливайте приложение перед запуском.
set -eu

DB_BACKUP="$1"
STORAGE_BACKUP="$2"
DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
STORAGE_DIR="${OPENCRM_STORAGE_DIR:-/app/storage}"

[ -f "$DB_BACKUP" ] || { echo "no db backup: $DB_BACKUP"; exit 1; }
[ -f "$STORAGE_BACKUP" ] || { echo "no storage backup: $STORAGE_BACKUP"; exit 1; }

# текущее состояние — в сторону, а не в /dev/null
STAMP="$(date +%Y%m%d-%H%M%S)"
[ -f "$DB_FILE" ] && mv "$DB_FILE" "$DB_FILE.before-restore-$STAMP"

# База работает в journal_mode=WAL (database/session.py), и рядом остаются
# -wal/-shm от прежнего файла. Копию `.backup` они не описывают, но SQLite
# при открытии попробует доиграть их поверх — журнал прежней базы надо убрать.
rm -f "$DB_FILE-wal" "$DB_FILE-shm"

cp "$DB_BACKUP" "$DB_FILE"

mkdir -p "$STORAGE_DIR"
tar -xzf "$STORAGE_BACKUP" -C "$STORAGE_DIR"

# Ключ шифрования из копии не подставляем автоматически: он живёт в
# config/.env, а тот в контейнер не смонтирован, да и молча менять ключ
# работающей системы нельзя — этим можно сделать нечитаемым то, что сейчас
# читается. Говорим о нём словами, и это единственное, что тут уместно.
SECRET_BACKUP="$(dirname "$DB_BACKUP")/secret-$(basename "$DB_BACKUP" | sed 's/^db-//; s/\.db$//').env"
if [ -f "$SECRET_BACKUP" ]; then
    echo ""
    echo "ВАЖНО: рядом лежит ключ от этой копии — $SECRET_BACKUP"
    echo "Если восстанавливаете на ДРУГОЙ машине, перенесите OPENCRM_SECRET_KEY"
    echo "оттуда в config/.env ДО первого запуска: иначе пароли почтовых ящиков"
    echo "не расшифруются никогда."
fi

echo "restore done. Previous DB kept at $DB_FILE.before-restore-$STAMP"
echo "Start the app and verify /healthz and a couple of showcase links."
