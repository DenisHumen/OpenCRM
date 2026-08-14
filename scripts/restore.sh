#!/bin/sh
# Восстановление OpenCRM из бэкапа.
# Использование: restore.sh <db-YYYY-MM-DD.sql> <storage-YYYY-MM-DD.tar.gz>
# ВНИМАНИЕ: перезаписывает текущую базу и storage. Останавливайте приложение перед запуском.
#
# **Копия проверяется ДО того, как тронуть базу.** Восстановление — это
# единственный путь, которым чинят потерю данных, и молчаливый отказ на нём
# стоит дороже любого другого: заливают копию тогда, когда прежней базы уже
# нет. Поэтому «не та копия» и «копия оборвана» — это отказ до первой правки, а
# не бодрое «restore done.» над половиной таблиц.
set -eu

DB_BACKUP="$1"
STORAGE_BACKUP="$2"
DB_URL="${OPENCRM_DB_URL:-}"
STORAGE_DIR="${OPENCRM_STORAGE_DIR:-/app/storage}"

[ -f "$DB_BACKUP" ] || { echo "no db backup: $DB_BACKUP"; exit 1; }
[ -f "$STORAGE_BACKUP" ] || { echo "no storage backup: $STORAGE_BACKUP"; exit 1; }

# Годна ли копия — спрашиваем ДО того, как тронуть базу.
#
# Оборванный дамп — обычный текстовый файл: он открывается, читается и выглядит
# совершенно целым. Если обрыв пришёлся на границу оператора (а на кончившемся
# месте это ровно так и бывает), клиент `mysql` заливает такой огрызок БЕЗ
# ЕДИНОЙ ЖАЛОБЫ и выходит с нулём. Поймано живьём на стенде: копию, обрезанную
# пополам, залили в испорченную базу — скрипт напечатал «restore done.», а
# таблиц users, warehouses и tasks в базе не было вовсе. Узнать об этом было
# неоткуда: `scripts/verify_backup.py` об этой же копии говорил «НЕГОДНА», но
# восстановление его не спрашивало.
#
# Признак негодности один — отсутствие хвоста, который дампер дописывает
# последним действием. Хвостов два, потому что дамперов два: `-- Dump completed`
# у mysqldump (scripts/backup.sh) и метка у scripts/snapshot_db.py — вернуть
# просят и предмиграционный снимок тоже.
KONETS_DUMPA="-- Dump completed"
KONETS_SNIMKA="-- opencrm snapshot complete"

# Вид копии — по расширению; отдельной переменной, чтобы не спутать этот разбор
# с разбором ниже, по которому ходит сторож в tests/test_backup.py.
VID_KOPII=${DB_BACKUP##*.}
if [ "$VID_KOPII" != "sql" ]; then
    # Файл от прежней установки (db-ГГГГ-ММ-ДД.db, база SQLite) лежит в том же
    # каталоге и выбирается по ошибке. Заливать его некуда, и молчать об этом
    # нельзя: раньше такой файл проходил мимо ветки `*.sql` и скрипт бодро
    # печатал «restore done.», не тронув базу вовсе.
    echo "непонятная копия базы: $DB_BACKUP" >&2
    echo "Восстанавливать нечем — копии базы называются db-ГГГГ-ММ-ДД.sql." >&2
    exit 1
fi

if ! tail -c 4096 "$DB_BACKUP" | grep -q -e "$KONETS_DUMPA" -e "$KONETS_SNIMKA"; then
    echo "копия $DB_BACKUP оборвана: хвоста дампера в ней нет." >&2
    if [ "${OPENCRM_FORCE_RESTORE:-0}" = "1" ]; then
        echo "OPENCRM_FORCE_RESTORE=1 — заливаю как есть, под вашу ответственность." >&2
    else
        echo "Заливать её нельзя: вернётся половина таблиц, а сказано будет" >&2
        echo "«готово». Возьмите копию поновее — или, если половина данных всё же" >&2
        echo "лучше, чем ничего, запустите с OPENCRM_FORCE_RESTORE=1." >&2
        exit 1
    fi
fi

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
                    # Это полная база в одном файле — с хэшами паролей и
                    # шифротекстами почтовых ящиков, ровно как обычная копия.
                    # Умаска здесь 022, и файл ложился 0644, то есть читаемым
                    # любым пользователем машины (проверено на стенде). Маску
                    # целиком не трогаем: под неё попадёт и распаковка storage,
                    # а те файлы отдаёт nginx из-под своего пользователя.
                    chmod 600 "$_before"
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
    esac
fi

mkdir -p "$STORAGE_DIR"
tar -xzf "$STORAGE_BACKUP" -C "$STORAGE_DIR"

# Ключ шифрования из копии не подставляем автоматически: он живёт в
# config/.env, а тот в контейнер не смонтирован, да и молча менять ключ
# работающей системы нельзя — этим можно сделать нечитаемым то, что сейчас
# читается. Говорим о нём словами, и это единственное, что тут уместно.
SECRET_BACKUP="$(dirname "$DB_BACKUP")/secret-$(basename "$DB_BACKUP" | sed 's/^db-//; s/\.sql$//').env"
if [ -f "$SECRET_BACKUP" ]; then
    echo ""
    echo "ВАЖНО: рядом лежит ключ от этой копии — $SECRET_BACKUP"
    echo "Если восстанавливаете на ДРУГОЙ машине, перенесите OPENCRM_SECRET_KEY"
    echo "оттуда в config/.env ДО первого запуска: иначе пароли почтовых ящиков"
    echo "не расшифруются никогда."
fi

if [ "${OPENCRM_SKIP_DB:-0}" != "1" ]; then
    echo "restore done."
else
    echo "restore done (storage)."
fi
echo "Start the app and verify /healthz and a couple of showcase links."
