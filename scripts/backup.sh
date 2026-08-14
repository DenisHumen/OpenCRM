#!/bin/sh
# Ежедневный бэкап OpenCRM: база, файлы, ключ шифрования — и проверка того,
# что получилось.
#
# Хранение: 7 ежедневных + 4 еженедельных (воскресные).
# Cron (на хосте):  0 3 * * *  /path/to/OpenCRM/scripts/backup.sh
# Для Docker-томов запускать внутри контейнера app либо примонтировав тома.
#
# **База копируется дампом `mysqldump --single-transaction`** — с одного
# согласованного снимка и без блокировки работы.
#
# **Копия включает ключ шифрования, и это осознанно.** Без OPENCRM_SECRET_KEY
# восстановленная база наполовину мертва: пароли почтовых ящиков зашифрованы им
# и не выводятся из данных (core/security/secretbox.py). Ключ, оставшийся только
# в config/.env на сгоревшем сервере, — это потеря навсегда, а не неудобство.
# Файл кладётся с правами 600 и ровно с тем, чего нельзя восстановить иначе.
#
# **Копия проверяется сразу после снятия.** Копии, которые никто не проверял, —
# это не копии, а надежда: `.backup`, оборванный на полном диске, оставляет
# файл, который выглядит как база и читается до первой битой страницы. Оборванный
# дамп MySQL выглядит ещё безобиднее — это обычный текстовый файл, и «негодность»
# у него не видна ничем, кроме отсутствующего хвоста. Узнавать об этом в день,
# когда база понадобилась, — слишком поздно.
set -eu

DB_URL="${OPENCRM_DB_URL:-}"
STORAGE_DIR="${OPENCRM_STORAGE_DIR:-/app/storage}"
BACKUP_DIR="${OPENCRM_BACKUP_DIR:-/app/data/backups}"

STAMP="$(date +%Y-%m-%d)"
DOW="$(date +%u)"   # 7 = воскресенье
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# Дамп MySQL в указанный файл.
#
# Клиента `mysqldump` в образе приложения нет: он лежит в образе базы. Поэтому
# `./opencrm.sh backup` снимает дамп заходом в контейнер базы и передаёт сюда
# уже готовый файл через OPENCRM_DB_DUMP — а имя по дате, ротацию и проверку
# годности по-прежнему делает этот скрипт, в одном месте для обеих баз. Там, где
# клиент есть (cron на хосте, стенд, машина администратора), скрипт справляется
# сам и ничего снаружи не требует.
snyat_dump() {
    _cel=$1

    if [ -n "${OPENCRM_DB_DUMP:-}" ]; then
        # Пустой файл — это оборванное снятие, а не копия. Пропустить его сюда
        # значит получить «копию» размером в ноль, о негодности которой скажет
        # только проверка, а до неё ещё дойти надо.
        [ -s "$OPENCRM_DB_DUMP" ] || {
            echo "дамп $OPENCRM_DB_DUMP пуст или отсутствует — копия не снята" >&2
            exit 1
        }
        mv "$OPENCRM_DB_DUMP" "$_cel"
        return 0
    fi

    command -v mysqldump >/dev/null 2>&1 || {
        echo "OPENCRM_DB_URL ведёт на MySQL, а клиента mysqldump здесь нет." >&2
        echo "Запускайте копирование через ./opencrm.sh backup — он снимает дамп" >&2
        echo "в контейнере базы, где клиент есть, и передаёт его сюда." >&2
        exit 1
    }

    # Разбор URL вида mysql+pymysql://user:pass@host:port/base?charset=utf8mb4
    # средствами самой оболочки: ни python, ни sed здесь не нужны, а лишняя
    # зависимость в скрипте, который зовут из cron, — лишний повод не сработать.
    #
    # Пароли для этой установки генерирует opencrm.sh в алфавите A-Za-z0-9
    # (gen_secret) именно затем, чтобы разбор был возможен: `@`, `:` и `/` в
    # пароле разрезали бы URL не там, а знак процента потребовал бы раскодировки.
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

    # Пароль уходит в файл, а не в командную строку: аргументы процесса видит
    # любой пользователь машины через `ps`, и `-pПАРОЛЬ` раздал бы доступ к базе
    # всем, кто оказался рядом в момент снятия копии. Файл создаётся с правами
    # 600 и удаляется сразу, а `--defaults-extra-file` обязан идти первым
    # аргументом — иначе mysqldump его не заметит.
    _cnf=$(mktemp)
    chmod 600 "$_cnf"
    printf '[client]\nuser=%s\npassword="%s"\nhost=%s\nport=%s\n' \
        "$_user" "$_pass" "$_host" "$_port" > "$_cnf"

    # --single-transaction — то, ради чего всё это: дамп снимается с одного
    # согласованного снимка данных и НЕ блокирует работу. Без него mysqldump
    # берёт блокировку чтения на все таблицы, и сайт на время копии встаёт.
    #
    # Пайпа здесь намеренно нет (`mysqldump ... > файл`, а не `... | gzip > ...`):
    # в POSIX sh код возврата из середины пайпа не достать, и упавший на полпути
    # mysqldump выглядел бы как успешное снятие копии.
    if ! mysqldump --defaults-extra-file="$_cnf" \
        --single-transaction --routines --triggers --no-tablespaces \
        --default-character-set=utf8mb4 "$_baza" > "$_cel"; then
        rm -f "$_cnf"
        echo "mysqldump не справился — копия не снята" >&2
        exit 1
    fi
    rm -f "$_cnf"
}

# 1) база — дампом. База одна, и ветка одна.
DB_COPY="$BACKUP_DIR/daily/db-$STAMP.sql"
snyat_dump "$DB_COPY"

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
    cp "$DB_COPY" "$BACKUP_DIR/weekly/"
    cp "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" "$BACKUP_DIR/weekly/"
    cp "$SECRET_FILE" "$BACKUP_DIR/weekly/"
fi

# 5) ротация: daily > 7 дней, weekly > 28 дней
find "$BACKUP_DIR/daily" -type f -mtime +7 -delete
find "$BACKUP_DIR/weekly" -type f -mtime +28 -delete

# 6) проверка того, что получилось. Отказ здесь — отказ всего скрипта: копия,
#    о негодности которой не сказали, хуже отсутствия копии, потому что на неё
#    рассчитывают. Проверка сама разбирается, файл перед ней или дамп.
python -m scripts.verify_backup \
    "$DB_COPY" \
    "$BACKUP_DIR/daily/storage-$STAMP.tar.gz" \
    "$SECRET_FILE"

# 7) (опционально) выгрузка наружу — раскомментировать и настроить.
#    Копия на том же диске, что и база, спасает от ошибки человека, но не от
#    смерти диска. Ключ шифруется отдельно и уезжает вместе с базой.
# age -r "$OPENCRM_BACKUP_PUBKEY" -o "/tmp/$(basename "$DB_COPY").age" "$DB_COPY"
# rclone copy "/tmp/$(basename "$DB_COPY").age" remote:opencrm-backups/

echo "backup done: $STAMP"
