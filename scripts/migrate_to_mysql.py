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

from sqlalchemy import Float, Integer, create_engine, func, insert, inspect, select, text  # noqa: E402

from database import models  # noqa: E402,F401 — регистрирует таблицы в metadata
from database import schema_check  # noqa: E402
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


def _rashodyatsya(bylo: dict, stalo: dict) -> bool:
    """Разошлись ли итоги одной таблицы. Отдельно — чтобы спросить это по имени."""
    return any(znachenie != stalo[kluch] for kluch, znachenie in bylo.items())


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


def _po_poryadku(table):
    """Выборка таблицы в устойчивом порядке — по первичному ключу, если он один.

    Без явного `ORDER BY` порядок строк внутри таблицы не определён ничем: он
    просто совпадал с порядком rowid у SQLite. У `finance_operations` есть
    ссылка на саму себя (`corrects_id`), то есть поправка ссылается на строку с
    меньшим id, — и вставка держалась ровно на этом совпадении. Смена плана
    выборки (индекс, версия SQLite, `stream_results`) уронила бы перенос на
    внешнем ключе, причём не сразу и не воспроизводимо.

    Составной или отсутствующий первичный ключ оставляем как есть: собирать
    порядок из нескольких колонок незачем — самоссылок в таких таблицах нет.
    """
    zapros = select(table)
    kluchi = list(table.primary_key.columns)
    if len(kluchi) == 1:
        return zapros.order_by(kluchi[0])
    return zapros


#: По сколько строк тянем при построчной сверке.
#:
#: Тысяча — это один круг по сети на тысячу строк и при этом десятки килобайт в
#: памяти. Обе стороны читаются потоком и идут навстречу друг другу, поэтому
#: память не зависит от размера таблицы вовсе: 2,7 млн строк проходят в том же
#: объёме, что и тысяча.
RAZMER_SVERKI = 1000

#: Сколько расхождений называем поимённо, прежде чем сказать «и ещё столько-то».
#:
#: Список на миллион строк не читает никто, а первых пяти хватает, чтобы понять
#: ЧТО случилось. Общее число при этом называется всегда — по нему видно
#: разницу между «одна строка» и «половина таблицы».
PREDEL_RASHOZHDENIY = 5


def _sravnimye(table):
    """Столбцы, которые можно сличать ПОБАЙТНО на двух разных движках.

    Из сравнения выпадает ровно один вид — `Float`, и не по лени. `Float` без
    указания точности компилируется в MySQL в `FLOAT`, то есть четыре байта
    против восьми у SQLite: 0,3333333333 возвращается как 0,33333334. Разница
    настоящая, но она есть у КАЖДОЙ такой колонки и на честном переносе тоже —
    сверка краснела бы всегда и ровно поэтому была бы выключена первым же
    действием. Ложная тревога хуже отсутствия тревог.

    Остальное сходится побайтно, и это свойство схемы, а не удача:
    `database/types.py` объявляет `DATETIME(6)` для всех колонок времени (иначе
    MySQL округляет доли секунды), `ExactString` — побайтное сравнение для
    токенов, `LongText` — `MEDIUMTEXT` вместо обрезающего `TEXT`. Строки при
    этом приходят с обоих движков одинаковыми: их не сравнивает база, их
    сравнивает Python.
    """
    return [c for c in table.c if not isinstance(c.type, Float)]


def _potok(engine, table, stolbcy, poryadok):
    """Строки таблицы потоком, в заданном порядке. Память не зависит от размера."""
    zapros = select(*stolbcy).order_by(*poryadok)
    with engine.connect() as c:
        kursor = c.execution_options(stream_results=True).execute(zapros)
        while True:
            pachka = kursor.fetchmany(RAZMER_SVERKI)
            if not pachka:
                return
            for stroka in pachka:
                yield tuple(stroka)


def _nazvat_stroku(table, stolbcy, stroka) -> str:
    """Как называть строку в отчёте: по первичному ключу, если он один."""
    kluchi = list(table.primary_key.columns)
    if len(kluchi) == 1:
        nomer = stroka[[c.name for c in stolbcy].index(kluchi[0].name)]
        return f"{kluchi[0].name}={nomer}"
    return "строка " + ", ".join(f"{c.name}={v!r}" for c, v in zip(stolbcy, stroka))[:120]


def sverit_postrochno(source, target, table) -> list[str]:
    """Сличает таблицу СТРОКА В СТРОКУ и называет расхождения поимённо.

    **Зачем это поверх сумм.** Агрегатная сверка (число строк плюс сумма по
    каждому целому столбцу) не видит порчу, которая сама себя компенсирует.
    Проверено делом: одной заявке `+1`, другой `-1` — число строк то же, сумма
    столбца та же, сверка зелёная, и сайт переключают на базу с двумя неверными
    суммами. Тот же провал у любой перестановки значений между строками.

    **Почему встречным ходом, а не двумя словарями.** Словарь «ключ → строка» на
    2,7 млн записей — это сотни мегабайт в процессе, который в этот момент и так
    держит два соединения к двум базам. Обе стороны читаются потоком в ОДНОМ
    порядке (по первичному ключу), и указатели идут навстречу друг другу: память
    постоянная, а расхождение называется в тот момент, когда встретилось.

    **Отсюда же и польза для второй беды.** Строка, заведённая в CRM уже после
    снятия копии, выглядит здесь как «есть в источнике, нет в цели» — и
    называется по первичному ключу, а не как «строк стало на одну меньше». По
    числу человек не поймёт, кого искать.
    """
    stolbcy = _sravnimye(table)
    kluchi = list(table.primary_key.columns)
    # Порядок обхода обязан быть одинаковым с обеих сторон и однозначным.
    # Единственный первичный ключ даёт и то, и другое; составной или
    # отсутствующий — берём все сравнимые столбцы, порядок от этого становится
    # произвольным, но одинаковым.
    poryadok = kluchi if len(kluchi) == 1 else stolbcy

    imena = [c.name for c in stolbcy]
    sleva = _potok(source, table, stolbcy, poryadok)
    sprava = _potok(target, table, stolbcy, poryadok)

    rashozhdenia: list[str] = []
    vsego = 0

    def skazat(chto: str) -> None:
        nonlocal vsego
        vsego += 1
        if len(rashozhdenia) < PREDEL_RASHOZHDENIY:
            rashozhdenia.append(chto)

    a = next(sleva, None)
    b = next(sprava, None)
    odin_kluch = len(kluchi) == 1
    while a is not None or b is not None:
        if b is None or (a is not None and odin_kluch and a[0] < b[0]):
            skazat(f"{table.name}: {_nazvat_stroku(table, stolbcy, a)} есть в источнике, в цели НЕТ")
            a = next(sleva, None)
        elif a is None or (odin_kluch and b[0] < a[0]):
            skazat(f"{table.name}: {_nazvat_stroku(table, stolbcy, b)} есть в цели, а в источнике нет")
            b = next(sprava, None)
        else:
            if a != b:
                otlichiya = [
                    f"{imya}: было {bylo!r}, стало {stalo!r}"
                    for imya, bylo, stalo in zip(imena, a, b)
                    if bylo != stalo
                ]
                skazat(
                    f"{table.name}: {_nazvat_stroku(table, stolbcy, a)} — "
                    + "; ".join(otlichiya)
                )
            a = next(sleva, None)
            b = next(sprava, None)

    if vsego > len(rashozhdenia):
        rashozhdenia.append(f"{table.name}: и ещё {vsego - len(rashozhdenia)} расхождений")
    return rashozhdenia


def sverit_shemu(engine) -> list[str]:
    """Сходится ли схема цели с моделями. Пустой список — сошлась.

    Это НЕ то же самое, что совпадение ревизий alembic. Ревизия говорит, какие
    шаги записаны выполненными; схема говорит, что получилось на самом деле.
    Расходятся они ровно в том случае, ради которого всё затевается: миграция
    прошла наполовину, отметилась и оставила таблицу без колонки.
    """
    otchyot = schema_check.check(engine)
    if otchyot.ok:
        return []
    return [otchyot.summary()]


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
    print(f"Ревизии сошлись: {rev_ist}")

    # 1б. Ревизии мало: она говорит, какие шаги ОТМЕЧЕНЫ выполненными, а не что
    #     из них получилось. Миграция, прошедшая наполовину и успевшая
    #     отметиться, оставляет таблицу без колонки при правильной ревизии —
    #     и перенос молча не довозит эту колонку. Поэтому схему цели сверяем с
    #     моделями тем же кодом, которым это делает приложение на старте.
    nesostykovki = sverit_shemu(target)
    if nesostykovki:
        raise SystemExit(
            "Схема цели не сходится с моделями:\n  " + "\n  ".join(nesostykovki) + "\n"
            "Прогоните на цели миграции заново:\n"
            "    OPENCRM_DB_URL=<цель> python -m alembic upgrade head"
        )
    print("Схема цели сходится с моделями")

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
    #
    # Отказ посередине оставляет цель в состоянии «посева уже нет, данных ещё
    # нет»: очистка закоммичена, часть пачек тоже. Само по себе это не беда —
    # повторный запуск снова чистит и переносит, — но МОЛЧАТЬ об этом нельзя.
    # Схема в такой базе сходится, `/healthz` зелёный, и переключённое на неё
    # приложение показало бы пустую CRM, ничем не отличимую от исправной.
    # Поэтому здесь ловим всё и говорим прямо, а решение (не переключаться,
    # остаться на SQLite) принимает вызывающий по коду возврата.
    print("")
    try:
        _perenesti_tablicy(source, target, tablicy, do, razmer_pachki)
    except Exception as exc:  # noqa: BLE001 — сказать вслух важнее, чем разобрать
        print(f"\nПЕРЕНОС ОБОРВАЛСЯ: {exc}")
        print(
            "Целевая база осталась НАПОЛОВИНУ ЗАПОЛНЕННОЙ и пользоваться ей нельзя:"
            " схема в ней сходится и снаружи она выглядит исправной пустой CRM.\n"
            "Исходная база не тронута — она открыта только на чтение. Ничего"
            " переключать не нужно, приложение по-прежнему работает на ней."
        )
        return [f"перенос оборвался: {exc}"]

    # 5. Сверяем. Это главная часть: потерянную при переносе строку иначе
    #    обнаружат через месяц и не свяжут с переездом.
    print("\n--- сверка ---")
    rashozhdenia = sverit(do, target, tablicy)
    if rashozhdenia:
        print("НЕ СОШЛОСЬ:")
        for stroka in rashozhdenia:
            print(f"  {stroka}")
    else:
        # Сказано ровно то, что проверено. Прежняя формулировка «сошлось всё»
        # обещала больше: здесь сличаются ИТОГИ — число строк и суммы, — а они
        # слепы к порче, которая сама себя компенсирует, и ничего не знают о
        # том, что дописали в живую базу, пока шёл перенос. Оба вопроса
        # закрывает отдельный заход `--verify`, и переезд признаётся удавшимся
        # по нему, а не по этой строке.
        print(
            f"Итоги переноса сошлись: {vsego} строк, суммы по целым столбцам совпали."
            "\nЭто ещё не годность базы: построчное сличение и сверка с ЖИВЫМ"
            " источником — отдельным заходом --verify."
        )
    return rashozhdenia


def _perenesti_tablicy(source, target, tablicy, do, razmer_pachki) -> None:
    for table in tablicy:
        vsego_v_tablice = do[table.name]["строк"]
        if not vsego_v_tablice:
            print(f"{table.name}: пусто")
            continue
        pereneseno = 0
        with source.connect() as src:
            # `stream_results` не тянет всю выборку в память разом; для SQLite
            # это ничего не стоит, а для больших таблиц решает всё.
            kursor = src.execution_options(stream_results=True).execute(_po_poryadku(table))
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


def proverit(source, target) -> list[str]:
    """Сверка без переноса: схема цели и сличение ИСТОЧНИКА с целью строка в строку.

    Отдельный проход, а не «заодно с переносом», и это главное в нём. Проверка,
    которая выполняется только вместе с удачным действием, проверяет само
    действие, а не его итог: перенос мог отработать, а приложение — дописать в
    источник строку за то время, пока он шёл. Здесь отпечаток источника
    снимается ЗАНОВО, уже после переноса, и сравнивается с целью.

    **Сверка идёт построчно, а не только по суммам, и это не педантизм.**
    Агрегат (число строк плюс сумма по каждому целому столбцу) слеп к порче,
    которая сама себя компенсирует: `+1` одной заявке и `−1` другой оставляют и
    число строк, и сумму столбца прежними. Проверено делом — сверка объявила
    годной базу с двумя неверными суммами.

    Суммы при этом остаются и считаются ПЕРВЫМИ: они дёшевы, а из красного
    агрегата сразу видно, какая таблица виновата. Построчный проход дороже (он
    читает обе базы целиком) и потому идёт вторым — но идёт всегда, иначе он был
    бы проверкой на удачу.

    Возвращает список расхождений; пустой — сошлось всё.
    """
    tablicy = list(Base.metadata.sorted_tables)

    rashozhdenia = sverit_shemu(target)
    if rashozhdenia:
        return rashozhdenia

    seychas = {t.name: _svodka(source, t) for t in tablicy}
    vsego = sum(s["строк"] for s in seychas.values())
    rashozhdenia = sverit(seychas, target, tablicy)

    # Разошедшийся итог говорит, В КАКОЙ таблице беда, но не в какой строке.
    # «Строк было 21, стало 20» не отвечает на вопрос «кого искать», а искать
    # придётся именно строку — заведённого во время переезда клиента, например.
    # Поэтому по таблицам с красным итогом идём построчно СРАЗУ, а по остальным
    # не идём вовсе: они уже сошлись, и полный проход по ним стоил бы чтения
    # всей базы ради ничего.
    spornye = [t for t in tablicy if rashozhdenia and _rashodyatsya(seychas[t.name], _svodka(target, t))]
    if not rashozhdenia:
        print(f"Итоги сошлись, сличаю построчно {vsego} строк", flush=True)
        spornye = tablicy

    for table in spornye:
        rashozhdenia.extend(sverit_postrochno(source, target, table))
        if len(rashozhdenia) > PREDEL_RASHOZHDENIY * 4:
            rashozhdenia.append("...дальше не смотрю: расхождений слишком много")
            break
    if rashozhdenia:
        return rashozhdenia

    # Названо ровно то, что проверено, и назван ИСТОЧНИК: зелёный итог, за
    # которым стоит сравнение с неподвижной копией, обещал бы про живую базу
    # больше, чем проверил.
    print(
        f"Сверка: схема сошлась с моделями; источник {_bez_parolya(source)} и цель"
        f" совпали построчно — {vsego} строк в {len(tablicy)} таблицах"
    )
    return []


def _bez_parolya(engine) -> str:
    """Адрес базы без пароля: сверка печатает его в лог установки."""
    return engine.url.render_as_string(hide_password=True)


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
    parser.add_argument(
        "--verify",
        action="store_true",
        help="только сверить УЖЕ ПЕРЕНЕСЁННОЕ: схему цели с моделями, число строк "
        "и суммы по каждой таблице. Ничего не пишет ни в источник, ни в цель",
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

    # Сверка отдельным заходом. Зовётся установщиком ПОСЛЕ переноса и ДО
    # переключения приложения: это тот самый шаг, на зелёном исходе которого
    # переезд признаётся удавшимся, а на любом другом — не признаётся, и
    # приложение остаётся на SQLite, ничего не откатывая.
    if args.verify:
        rashozhdenia = proverit(source, target)
        if rashozhdenia:
            print("СВЕРКА НЕ СОШЛАСЬ:")
            for stroka in rashozhdenia:
                print(f"  {stroka}")
            print(
                "\nПереключать приложение на эту базу НЕЛЬЗЯ. Исходная база не"
                " тронута — оставьте OPENCRM_DB_URL на sqlite, всё продолжает"
                " работать как раньше."
            )
            raise SystemExit(1)
        return

    rashozhdenia = perenesti(
        source, target, razmer_pachki=args.batch, tolko_proverka=args.check_only
    )

    if rashozhdenia:
        print(
            "\nПеренос НЕ удался. Исходная база не тронута — приложение"
            " по-прежнему работает на ней, переключать ничего не нужно."
        )
        raise SystemExit(1)

    if not args.check_only:
        print(
            "\nДанные перенесены и сошлись. Переключать приложение на новую базу"
            " можно только после отдельной сверки (--verify): она снимает"
            " отпечаток источника ЗАНОВО и сравнивает его с целью.\n"
            "SQLite-файл не тронут ни при каком исходе — он открывался только на"
            " чтение."
        )


if __name__ == "__main__":
    main()
