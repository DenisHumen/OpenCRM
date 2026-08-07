"""Перенос данных SQLite → MySQL.

Порядок действий целиком (см. docs/03-database.md):

  1. Поднять MySQL с пустой базой в кодировке utf8mb4.
  2. Построить в ней схему миграциями — теми же самыми, что и в SQLite:
       OPENCRM_DB_URL=mysql+pymysql://user:pass@host/opencrm python -m alembic upgrade head
  3. Перенести данные этим скриптом.
  4. Переключить OPENCRM_DB_URL приложения на MySQL и перезапустить.

Файлы в storage/ не трогаются: пути в базе относительные.

--------------------------------------------------------------------------
Чем этот скрипт отличается от наивного «прочитать всё и вставить»
--------------------------------------------------------------------------

**Исходная база открывается только на чтение и не меняется ничем.** Это не
предосторожность, а условие отката: пока SQLite-файл цел, неудавшийся переезд
стоит одной строки в конфиге — вернуть `OPENCRM_DB_URL` на sqlite и
перезапуститься. Всё остальное в этом скрипте построено так, чтобы это свойство
сохранялось при любом исходе.

**Целевые таблицы очищаются перед вставкой.** Миграции не только строят схему,
они ещё и СЕЮТ данные: этапы воронки, роль root, три десятка её прав, склад
«Основной». То есть к моменту переноса целевая база уже не пуста, и вставка
поверх падает на нарушении уникальности — причём не на первой таблице, а на
третьей-четвёртой, когда часть данных уже перенесена. Источник здесь
единственная власть: что было в SQLite, то и должно оказаться в MySQL, включая
изменённые названия этапов и снятые галочки прав.

**Строки идут пакетами, а не всей таблицей сразу.** Таблица движений склада за
пару лет работы — это сотни тысяч строк; прочитанная в список словарей, она
занимает гигабайты, а одним `executemany` не пролезает в `max_allowed_packet`.

**После переноса всё сверяется.** Совпадение числа строк и сумм по каждому
целочисленному столбцу. Целые числа в этом проекте — это деньги (в минорных
единицах) и количества (в тысячных), то есть ровно то, расхождение чего нельзя
заметить глазами. Молчаливая потеря строки при переносе — худшее, что здесь
может случиться, и сверка нужна именно против неё.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Integer, create_engine, func, insert, inspect, select, text  # noqa: E402

from database import models  # noqa: E402,F401 — регистрирует таблицы в metadata
from database import types  # noqa: E402,F401 — правила переносимости типов
from database.session import Base  # noqa: E402

#: По сколько строк вставляем за раз. 500 строк даже широкой таблицы — это
#: сотни килобайт, что заведомо меньше `max_allowed_packet` (по умолчанию 64 МБ)
#: и при этом достаточно крупно, чтобы не платить за каждый круг по сети.
RAZMER_PACHKI = 500


def _revizia(engine) -> str | None:
    """Ревизия alembic в базе. `None` — таблицы нет вовсе."""
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _svodka(engine, table) -> dict:
    """Отпечаток таблицы: сколько строк и суммы по целым столбцам.

    Суммы берём по целочисленным столбцам, потому что целые здесь — это деньги
    и количества. Строковые поля не суммируются: сверять их пришлось бы
    посимвольно, а движки по-разному отдают регистр и пробелы, и сверка давала
    бы ложные тревоги вместо пользы.
    """
    stolbcy = [c for c in table.c if isinstance(c.type, Integer)]
    zapros = select(func.count()).select_from(table)
    with engine.connect() as c:
        svodka = {"строк": c.execute(zapros).scalar_one()}
        for stolbec in stolbcy:
            svodka[stolbec.name] = c.execute(select(func.sum(stolbec))).scalar()
    return svodka


def sverit(do, target, tablicy) -> list[str]:
    """Сличает отпечатки «до» с тем, что оказалось в цели.

    Вынесено отдельной функцией нарочно: сверка — единственное, что стоит между
    молчаливой потерей строки и её обнаружением через месяц, поэтому она должна
    проверяться сама по себе, а не только заодно с удачным переносом. Проверка,
    которая срабатывает лишь тогда, когда и так всё хорошо, — не проверка.
    """
    rashozhdenia = []
    for table in tablicy:
        posle = _svodka(target, table)
        for kluch, bylo in do[table.name].items():
            stalo = posle[kluch]
            if bylo != stalo:
                rashozhdenia.append(f"{table.name}.{kluch}: было {bylo}, стало {stalo}")
    return rashozhdenia


def perenesti(source, target, *, razmer_pachki=RAZMER_PACHKI, tolko_proverka=False):
    """Переносит данные и возвращает список расхождений (пустой — всё сошлось)."""
    tablicy = list(Base.metadata.sorted_tables)

    # 1. Схемы обязаны совпадать. Разные ревизии — разный набор колонок, и
    #    перенос либо упадёт на полпути, либо (хуже) молча не довезёт то, чего
    #    в целевой схеме ещё нет.
    rev_ist, rev_cel = _revizia(source), _revizia(target)
    if rev_ist != rev_cel:
        raise SystemExit(
            f"Схемы разошлись: в источнике ревизия {rev_ist}, в цели {rev_cel}.\n"
            "Прогоните на цели те же миграции:\n"
            "    OPENCRM_DB_URL=<цель> python -m alembic upgrade head"
        )
    print(f"Схемы сошлись, ревизия {rev_ist}")

    # 2. Кодировка цели. utf8 (без mb4) в MySQL — это три байта на символ, и
    #    эмодзи в заметке клиента обрывают вставку на полуслове.
    if target.dialect.name == "mysql":
        with target.connect() as c:
            nabor = c.execute(text("SELECT @@character_set_database")).scalar()
        if nabor != "utf8mb4":
            raise SystemExit(
                f"Кодировка целевой базы {nabor}, а нужна utf8mb4: иначе эмодзи и"
                " часть символов не перенесутся.\n"
                "    ALTER DATABASE <имя> CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            )
        print("Кодировка цели utf8mb4")

    do = {t.name: _svodka(source, t) for t in tablicy}
    vsego = sum(s["строк"] for s in do.values())
    print(f"В источнике {vsego} строк в {len(tablicy)} таблицах")

    if tolko_proverka:
        print("\n--- только проверка, ничего не переношу ---")
        return []

    # 3. Чистим цель — в обратном порядке зависимостей, иначе внешние ключи не
    #    дадут удалить то, на что ссылаются.
    with target.begin() as dst:
        for table in reversed(tablicy):
            udaleno = dst.execute(table.delete()).rowcount
            if udaleno:
                print(f"  очищено {table.name}: было {udaleno} строк")

    # 4. Переносим пакетами, в порядке зависимостей.
    print("")
    for table in tablicy:
        vsego_v_tablice = do[table.name]["строк"]
        if not vsego_v_tablice:
            print(f"{table.name}: пусто")
            continue
        pereneseno = 0
        with source.connect() as src:
            # `stream_results` не тянет всю выборку в память разом; для SQLite
            # это ничего не стоит, а для больших таблиц решает всё.
            kursor = src.execution_options(stream_results=True).execute(select(table))
            while True:
                pachka = kursor.fetchmany(razmer_pachki)
                if not pachka:
                    break
                with target.begin() as dst:
                    dst.execute(insert(table), [dict(r._mapping) for r in pachka])
                pereneseno += len(pachka)
                if vsego_v_tablice > razmer_pachki * 4:
                    print(f"  {table.name}: {pereneseno}/{vsego_v_tablice}", flush=True)
        print(f"{table.name}: перенесено {pereneseno}")

    # 5. Сверяем. Это главная часть: потерянную при переносе строку иначе
    #    обнаружат через месяц и не свяжут с переездом.
    print("\n--- сверка ---")
    rashozhdenia = sverit(do, target, tablicy)
    if rashozhdenia:
        print("НЕ СОШЛОСЬ:")
        for stroka in rashozhdenia:
            print(f"  {stroka}")
    else:
        print(f"Сошлось всё: {vsego} строк, суммы по целым столбцам совпали")
    return rashozhdenia


def main() -> None:
    parser = argparse.ArgumentParser(description="Перенос данных SQLite → MySQL")
    parser.add_argument("--source", required=True, help="URL исходной БД (SQLite)")
    parser.add_argument("--target", required=True, help="URL целевой БД (MySQL)")
    parser.add_argument(
        "--batch", type=int, default=RAZMER_PACHKI, help=f"строк за раз (по умолчанию {RAZMER_PACHKI})"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="только сверить схемы и посчитать строки, ничего не переносить",
    )
    args = parser.parse_args()

    # Источник открываем ТОЛЬКО НА ЧТЕНИЕ. Пока SQLite-файл цел, неудавшийся
    # переезд стоит одной строки в конфиге; испорченный — стоит восстановления
    # из копии, а это уже потерянное время и нервы.
    istochnik = args.source
    if istochnik.startswith("sqlite:///") and ":memory:" not in istochnik:
        put = istochnik.removeprefix("sqlite:///")
        istochnik = f"sqlite:///file:{put}?mode=ro&uri=true"

    source = create_engine(istochnik)
    target = create_engine(args.target)

    rashozhdenia = perenesti(
        source, target, razmer_pachki=args.batch, tolko_proverka=args.check_only
    )

    if rashozhdenia:
        print(
            "\nПеренос НЕ удался. Исходная база не тронута — верните"
            " OPENCRM_DB_URL на sqlite и перезапустите приложение,"
            " всё продолжит работать как раньше."
        )
        raise SystemExit(1)

    if not args.check_only:
        print(
            "\nГотово. Переключите OPENCRM_DB_URL на MySQL и перезапустите.\n"
            "Проверьте вход, пару карточек клиентов и публичную ссылку.\n"
            "SQLite-файл не тронут: если что-то не так, верните URL обратно."
        )


if __name__ == "__main__":
    main()
