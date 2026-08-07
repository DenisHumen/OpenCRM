#!/bin/sh
# Восстановление OpenCRM из бэкапа.
# Использование: restore.sh <db-YYYY-MM-DD.db|db-YYYY-MM-DD.sql> <storage-YYYY-MM-DD.tar.gz>
# ВНИМАНИЕ: перезаписывает текущую базу и storage. Останавливайте приложение перед запуском.
#
# Копия базы бывает двух видов, и вид определяется расширением:
#   .db   — файл SQLite, кладётся на место прежнего;
#   .sql  — дамп MySQL, заливается клиентом mysql.
set -eu

DB_BACKUP="$1"
STORAGE_BACKUP="$2"
DB_URL="${OPENCRM_DB_URL:-sqlite}"
DB_FILE="${OPENCRM_DB_FILE:-/app/data/opencrm.db}"
STORAGE_DIR="${OPENCRM_STORAGE_DIR:-/app/storage}"

[ -f "$DB_BACKUP" ] || { echo "no db backup: $DB_BACKUP"; exit 1; }
[ -f "$STORAGE_BACKUP" ] || { echo "no storage backup: $STORAGE_BACKUP"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"

# Базу можно пропустить: на MySQL дамп заливает `./opencrm.sh restore` заходом в
# контейнер базы — клиент mysql лежит в её образе, а в образе приложения его нет.
# Сюда скрипт при этом всё равно заходит: storage, предупреждение про ключ и
# отчёт о сделанном одни на оба случая, и разъезжаться им незачем.
if [ "${OPENCRM_SKIP_DB:-0}" = "1" ]; then
    echo "базу не трогаю: дамп залит снаружи (OPENCRM_SKIP_DB=1)"
else
    case "$DB_BACKUP" in
        *.sql)
            case "$DB_URL" in
                mysql*) ;;
                *) echo "это дамп MySQL, а OPENCRM_DB_URL ведёт на SQLite"; exit 1 ;;
            esac
            command -v mysql >/dev/null 2>&1 || {
                echo "Дамп MySQL нечем заливать: клиента mysql здесь нет." >&2
                echo "Восстанавливайте через ./opencrm.sh restore — он заливает дамп" >&2
                echo "в контейнере базы, где клиент есть." >&2
                exit 1
            }

            # Разбор URL — тот же, что и в scripts/backup.sh, и по той же
            # причине: скрипт восстановления обязан работать там, где кроме
            # оболочки может не оказаться ничего.
            _bez_shemy=${DB_URL#*://}
            _uchetka=${_bez_shemy%%@*}
            _hvost=${_bez_shemy#*@}
            _user=${_uchetka%%:*}
            case "$_uchetka" in
                *:*) _pass=${_uchetka#*:} ;;
                *)   _pass="" ;;
            esac
            _adres=${_hvost%%/*}
            _baza=${_hvost#*/}
            _baza=${_baza%%\?*}
            _host=${_adres%%:*}
            case "$_adres" in
                *:*) _port=${_adres#*:} ;;
                *)   _port=3306 ;;
            esac

            # Пароль — в файл с правами 600, а не в командную строку: аргументы
            # процесса видит через `ps` любой пользователь машины.
            _cnf=$(mktemp)
            chmod 600 "$_cnf"
            printf '[client]\nuser=%s\npassword="%s"\nhost=%s\nport=%s\n' \
                "$_user" "$_pass" "$_host" "$_port" > "$_cnf"

            # Текущее состояние — в сторону, а не в /dev/null. Для MySQL это не
            # переименование файла, а дамп: без него откатывать неудачное
            # восстановление будет некуда.
            if command -v mysqldump >/dev/null 2>&1; then
                _before="$(dirname "$DB_BACKUP")/../db-before-restore-$STAMP.sql"
                if mysqldump --defaults-extra-file="$_cnf" --single-transaction \
                    --routines --triggers --no-tablespaces \
                    --default-character-set=utf8mb4 "$_baza" > "$_before"; then
                    echo "прежняя база сохранена: $_before"
                else
                    rm -f "$_cnf" "$_before"
                    echo "не удалось сохранить прежнюю базу — ничего не менял" >&2
                    exit 1
                fi
            fi

            if ! mysql --defaults-extra-file="$_cnf" \
                --default-character-set=utf8mb4 "$_baza" < "$DB_BACKUP"; then
                rm -f "$_cnf"
                echo "дамп не залился — смотрите вывод выше" >&2
                exit 1
            fi
            rm -f "$_cnf"
            ;;
        *)
            # текущее состояние — в сторону, а не в /dev/null.
            #
            # Явный if, а не `[ ] && mv`: цепочка здесь стоит последней в ветке
            # case, её код возврата становится кодом всей ветки, и под `set -e`
            # такая конструкция ронять скрипт на ровном месте уже умела (см. тот
            # же урок в opencrm.sh:guard_root).
            if [ -f "$DB_FILE" ]; then
                mv "$DB_FILE" "$DB_FILE.before-restore-$STAMP"
            fi

            # База работает в journal_mode=WAL (database/session.py), и рядом остаются
            # -wal/-shm от прежнего файла. Копию `.backup` они не описывают, но SQLite
            # при открытии попробует доиграть их поверх — журнал прежней базы надо убрать.
            rm -f "$DB_FILE-wal" "$DB_FILE-shm"

            cp "$DB_BACKUP" "$DB_FILE"
            ;;
    esac
fi

mkdir -p "$STORAGE_DIR"
tar -xzf "$STORAGE_BACKUP" -C "$STORAGE_DIR"

# Ключ шифрования из копии не подставляем автоматически: он живёт в
# config/.env, а тот в контейнер не смонтирован, да и молча менять ключ
# работающей системы нельзя — этим можно сделать нечитаемым то, что сейчас
# читается. Говорим о нём словами, и это единственное, что тут уместно.
SECRET_BACKUP="$(dirname "$DB_BACKUP")/secret-$(basename "$DB_BACKUP" | sed 's/^db-//; s/\.db$//; s/\.sql$//').env"
if [ -f "$SECRET_BACKUP" ]; then
    echo ""
    echo "ВАЖНО: рядом лежит ключ от этой копии — $SECRET_BACKUP"
    echo "Если восстанавливаете на ДРУГОЙ машине, перенесите OPENCRM_SECRET_KEY"
    echo "оттуда в config/.env ДО первого запуска: иначе пароли почтовых ящиков"
    echo "не расшифруются никогда."
fi

if [ "${OPENCRM_SKIP_DB:-0}" != "1" ]; then
    case "$DB_BACKUP" in
        *.sql) echo "restore done (MySQL)." ;;
        *)     echo "restore done. Previous DB kept at $DB_FILE.before-restore-$STAMP" ;;
    esac
else
    echo "restore done (storage)."
fi
echo "Start the app and verify /healthz and a couple of showcase links."
