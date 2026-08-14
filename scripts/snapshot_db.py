"""Копия базы MySQL перед миграциями — та, к которой возвращаются.

Нужна ради правила номер один: обновление, не дождавшееся `/healthz`,
откатывает и код, и базу (`deploy/updater.py`). Найдено на стенде: снимок в
`docker/entrypoint.sh` стоял внутри проверки «файл базы существует», то есть
снимался только для файловой базы, а на MySQL не снималось НИЧЕГО. В журнале
запуска это выглядело так: старая копия файла и следом миграции по MySQL.

--------------------------------------------------------------------------
Почему свой дампер, а не `mysqldump` в образе
--------------------------------------------------------------------------

Соблазн понятен: `mysqldump` написан не нами и проверен миллионом установок.
Против него — четыре довода, и первый из них решающий.

**1. Клиента в образе приложения нет и заводить его некуда.** Образ собран из
`python:3.12-slim`, а `mysqldump` живёт в образе базы. Проект уже принял это
решение однажды: `scripts/backup.sh` снимает дамп заходом В КОНТЕЙНЕР БАЗЫ
(`./opencrm.sh backup`). Точка входа так не может — она работает ИЗНУТРИ
контейнера приложения, сокета docker у неё нет и быть не должно.

**2. Ревизию всё равно читать Python'ом.** Имя копии строится по ревизии
alembic, а прочитать `alembic_version` из точки входа нечем: клиента базы в
образе приложения нет (довод 1). То есть Python на этом шаге появляется в любом
случае, и вопрос лишь в том, звать ли из него ещё и внешний клиент.

**3. Размер образа и время сборки.** `default-mysql-client` в debian — это
mariadb-client с зависимостями, десятки мегабайт и лишняя минута сборки на
каждом обновлении. Платить их ради того, что здесь делается сотней строк на уже
обязательном `pymysql`, не за что.

**4. Клиент MariaDB против сервера MySQL — третья пара версий в системе.**
Совместимость их дампов держится на договорённостях и ломается в мелочах, а
проверять это пришлось бы на каждой новой базовой версии образа.

Цена решения названа честно: экранирование значений здесь наше. Оно делегировано
`pymysql` — той самой функции, которой драйвер экранирует параметры КАЖДОГО
запроса приложения. Если бы она врала, врало бы всё приложение, а не дамп.

--------------------------------------------------------------------------
Что даёт годность копии
--------------------------------------------------------------------------

Дамп пишется во временный файл и переименовывается только целиком: оборванный
на полпути (кончилось место, убили контейнер) не должен выглядеть готовой
копией. Последней строкой идёт метка с числом таблиц и строк — по её отсутствию
негодность видна без чтения всего файла, и её же проверяет сам скрипт сразу
после записи. Ровно этого не хватало резервным копиям, пока их никто не читал.

--------------------------------------------------------------------------
Как этим пользоваться
--------------------------------------------------------------------------

    python -m scripts.snapshot_db revision        # ревизия alembic или none
    python -m scripts.snapshot_db dump ФАЙЛ       # снять копию

Восстановление — тем же способом, каким снимаются обычные копии MySQL, то есть
заходом в контейнер базы, где клиент есть:

    docker compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        mysql -uroot opencrm' < ФАЙЛ
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from config.settings import get_settings  # noqa: E402

#: По сколько строк собираем в один `INSERT`.
#:
#: Пятьсот широких строк — сотни килобайт, то есть заведомо меньше
#: `max_allowed_packet` (64 МБ по умолчанию) с запасом на самое длинное письмо в
#: `mail_messages`.
RAZMER_PACHKI = 500

#: Метка конца. По ней и только по ней копия считается снятой целиком.
METKA = "-- opencrm snapshot complete"


def _engine():
    """Движок по адресу из настроек. Отдельной строкой — адрес секретный."""
    return create_engine(get_settings().db_url, pool_pre_ping=True)


def revizia(engine) -> str:
    """Ревизия alembic в базе. `none` — таблицы нет вовсе (база пустая).

    Именно ревизия, а не дата, стоит в имени копии: копий на одну ревизию
    достаточно одной, и повторный старт контейнера на той же ревизии не должен
    заводить вторую. Прежде копия была одна и обновлялась на КАЖДОМ старте —
    включая тот перезапуск, который случается уже после неудачной миграции, — и
    затиралась состоянием ПОСЛЕ неё.
    """
    with engine.connect() as c:
        try:
            znachenie = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:  # noqa: BLE001 — нет таблицы, нет прав, пустая база
            return "none"
    return znachenie or "none"


def _tablicy(c) -> list[str]:
    """Имена таблиц ЖИВОЙ базы, а не моделей.

    Копия снимается с того, что есть, а не с того, что должно быть: код в этот
    момент уже новый, а база ещё старая — в том и смысл копии. Представления
    (`VIEW`) пропускаем: своих в проекте нет, а чужое в нашей схеме — не то, что
    стоит воспроизводить дампом.
    """
    stroki = c.execute(text("SHOW FULL TABLES")).all()
    return sorted(r[0] for r in stroki if r[1] == "BASE TABLE")


def _literal(escape, znachenie) -> str:
    """Значение как литерал SQL. Экранирование — драйвера, не наше.

    `escape` — метод живого соединения pymysql, тот самый, которым драйвер
    подставляет параметры в каждый запрос приложения. Двоичные строки он
    оборачивает в `_binary'...'`, `None` превращает в `NULL`, кавычки и обратные
    слэши экранирует по правилам сервера, к которому подключён (важно при
    `NO_BACKSLASH_ESCAPES`).
    """
    if znachenie is None:
        return "NULL"
    return escape(znachenie)


def snyat(engine, put: Path) -> tuple[int, int]:
    """Снять дамп в файл. Возвращает (таблиц, строк).

    Снимок согласованный: `START TRANSACTION WITH CONSISTENT SNAPSHOT` даёт
    InnoDB одну точку во времени на весь дамп и НЕ блокирует пишущих. Ровно то
    же делает `mysqldump --single-transaction`, и по той же причине: копия,
    останавливающая сайт на время своего снятия, — это простой на каждом
    обновлении.
    """
    raw = engine.raw_connection()
    try:
        escape = raw.driver_connection.escape
        with engine.connect() as c:
            c.execute(text("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            c.execute(text("START TRANSACTION WITH CONSISTENT SNAPSHOT"))
            imena = _tablicy(c)
            vsego_strok = 0
            with put.open("w", encoding="utf-8", newline="\n") as f:
                f.write(_shapka(engine))
                for imya in imena:
                    vsego_strok += _odna_tablica(c, f, imya, escape)
                f.write("\nSET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n")
                f.write(f"{METKA}: таблиц {len(imena)}, строк {vsego_strok}\n")
    finally:
        raw.close()
    return len(imena), vsego_strok


def _shapka(engine) -> str:
    return (
        "-- OpenCRM: копия базы перед миграциями.\n"
        f"-- база {engine.url.database}, снято {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}\n"
        "--\n"
        "-- Восстановление (клиент живёт в контейнере базы, не приложения):\n"
        "--   docker compose exec -T db sh -c 'MYSQL_PWD=\"$MYSQL_ROOT_PASSWORD\" \\\n"
        f"--       mysql -uroot {engine.url.database}' < ЭТОТ_ФАЙЛ\n"
        "SET NAMES utf8mb4;\n"
        # Внешние ключи выключаются на время заливки: порядок таблиц в дампе
        # алфавитный, а не по зависимостям, и первая же ссылка вперёд иначе
        # уронила бы восстановление. Уникальность — ради скорости.
        "SET FOREIGN_KEY_CHECKS=0;\n"
        "SET UNIQUE_CHECKS=0;\n"
    )


def _odna_tablica(c, f, imya: str, escape) -> int:
    kavychki = f"`{imya}`"
    ddl = c.execute(text(f"SHOW CREATE TABLE {kavychki}")).one()[1]
    f.write(f"\n--\n-- {imya}\n--\nDROP TABLE IF EXISTS {kavychki};\n{ddl};\n")

    # Поток, а не выборка целиком: таблица движений склада за пару лет — сотни
    # тысяч строк, и прочитанная в список она занимает гигабайты.
    kursor = c.execution_options(stream_results=True).execute(text(f"SELECT * FROM {kavychki}"))
    stolbcy = ", ".join(f"`{k}`" for k in kursor.keys())
    vsego = 0
    while True:
        pachka = kursor.fetchmany(RAZMER_PACHKI)
        if not pachka:
            break
        znacheniya = ",\n".join(
            "(" + ",".join(_literal(escape, v) for v in stroka) + ")" for stroka in pachka
        )
        f.write(f"INSERT INTO {kavychki} ({stolbcy}) VALUES\n{znacheniya};\n")
        vsego += len(pachka)
    return vsego


def celaya(put: Path) -> bool:
    """Дочитана ли копия до метки конца.

    Проверяется сразу после снятия и по хвосту, а не по размеру: оборванный
    дамп — это обычный текстовый файл, и негодность у него не видна ничем,
    кроме отсутствующего хвоста.
    """
    if not put.is_file() or put.stat().st_size == 0:
        return False
    with put.open("rb") as f:
        f.seek(max(0, put.stat().st_size - 4096))
        hvost = f.read().decode("utf-8", "replace")
    return METKA in hvost


def main() -> int:
    parser = argparse.ArgumentParser(description="Копия базы MySQL перед миграциями")
    podkomandy = parser.add_subparsers(dest="komanda", required=True)
    podkomandy.add_parser("revision", help="ревизия alembic в базе (или none)")
    podkomandy.add_parser(
        "ping",
        help="отвечает ли сервер базы: код 0 — да, 1 — нет. Ничего не читает и "
        "не пишет",
    )
    dump = podkomandy.add_parser("dump", help="снять копию в файл")
    dump.add_argument("fayl")
    args = parser.parse_args()

    engine = _engine()
    # `ping` существует ради одного: отличить «сервер ещё не слушает порт» от
    # «с базой беда». Точка входа без этого различения принимала первое за
    # второе и убивала контейнер — см. `zhdat_bazu` в docker/entrypoint.sh.
    #
    # Молча и без подробностей: зовётся он в цикле ожидания, и десяток
    # трассировок в журнале старта только мешали бы разбирать настоящую беду.
    if args.komanda == "ping":
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — любая причина здесь значит «ещё нет»
            return 1
        return 0

    if args.komanda == "revision":
        print(revizia(engine))
        return 0

    put = Path(args.fayl)
    tablic, strok = snyat(engine, put)
    if not celaya(put):
        print(f"копия {put} снята не до конца — метки конца в ней нет", file=sys.stderr)
        return 1
    print(f"снято: таблиц {tablic}, строк {strok}, файл {put} ({put.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
