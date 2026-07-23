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

cp "$DB_BACKUP" "$DB_FILE"

mkdir -p "$STORAGE_DIR"
tar -xzf "$STORAGE_BACKUP" -C "$STORAGE_DIR"

echo "restore done. Previous DB kept at $DB_FILE.before-restore-$STAMP"
echo "Start the app and verify /healthz and a couple of showcase links."
