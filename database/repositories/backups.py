"""Заливка копии базы: дамп уходит на сервер пачками операторов.

**Не одним разговором.** Так было, и на этом восстановление ломалось: сервер
сверяет разговор с `max_allowed_packet` (64 МБ по умолчанию), а дамп рабочей
базы больше. То есть единственный путь восстановления отказывал ровно на тех
базах, ради которых копии и снимают.

**И не делением по `;`** — точка с запятой живёт внутри заметок и адресов.
Границу оператора ищет разбор (`operatory`) тем же правилом, каким считает
строки `scripts/verify_backup.py`. Круг «снял → залил → сверил» стоит в
`tests/test_vosstanovlenie_kopii.py`.

Живёт в `database/`, а не в службе: это разговор с движком о том, что в него
кладут, и граница базы (`tests/test_db_boundary.py`) держит его здесь.
"""

from collections.abc import Iterator
from pathlib import Path

from pymysql.constants import CLIENT
from sqlalchemy import create_engine

#: Сколько ждать чужой замок на таблицу. Заливка начинается с `DROP TABLE`, а
#: тот ждёт метаданных у всякой открытой транзакции — в том числе у запроса,
#: который заливку и позвал, если он не зафиксировался. Без предела ручка
#: висела бы до `lock_wait_timeout` сервера, а это год.
ZHDAT_ZAMOK_SEKUND = 30


#: Какую долю `max_allowed_packet` занимает одна пачка операторов.
#:
#: Запас, а не жадность: к самой пачке сервер прибавляет свою обвязку, а предел
#: он сверяет с ПАКЕТОМ, а не с текстом. Половина — то же соотношение, с каким
#: `snapshot_db.RAZMER_PACHKI` держит один `INSERT` заведомо меньше предела.
DOLYA_PAKETA = 2

#: Предел, если сервер о своём не сказал. Умолчание MySQL — 64 МБ.
PAKET_PO_UMOLCHANIYU = 64 * 1024 * 1024


def _predel_paketa(kursor) -> int:
    """Сколько байт сервер готов принять одним разговором."""
    try:
        kursor.execute("SELECT @@max_allowed_packet")
        stroka = kursor.fetchone()
        return max(int(stroka[0]) // DOLYA_PAKETA, 1024 * 1024)
    except Exception:  # noqa: BLE001 — молчащий сервер не повод падать до заливки
        return PAKET_PO_UMOLCHANIYU // DOLYA_PAKETA


def operatory(stroki) -> Iterator[str]:
    r"""Дамп по операторам, потоком.

    Границу оператора ищем разбором, а не делением по `;`: точка с запятой
    живёт внутри заметок и адресов, и разрезанный по ней `INSERT` — это
    синтаксическая ошибка на середине заливки. Правило то же, что у счётчика
    строк в `scripts/verify_backup.py`: внутри строкового литерала знаки не
    считаются, `\` экранирует следующий.

    Потоком, а не целиком: дамп рабочей базы — сотни мегабайт, и заливка не
    вправе требовать под себя столько же памяти.
    """
    kusok: list[str] = []
    v_stroke = ekran = False
    for stroka in stroki:
        # Строка-комментарий целиком пропускается, и это не украшение: апостроф
        # в пояснении («-- клиент O'Brien») открыл бы строковый литерал, и
        # разбор проглотил бы всё до следующего апострофа вместе с концами
        # операторов. Пропускаем только когда оператор ещё не начат.
        golaya = stroka.lstrip()
        if (
            not v_stroke
            and not "".join(kusok).strip()
            and (golaya.startswith("--") or golaya.startswith("#"))
        ):
            continue
        nachalo = 0
        for nomer, znak in enumerate(stroka):
            if ekran:
                ekran = False
            elif v_stroke:
                if znak == "\\":
                    ekran = True
                elif znak == "'":
                    v_stroke = False
            elif znak == "'":
                v_stroke = True
            elif znak == ";":
                kusok.append(stroka[nachalo : nomer + 1])
                nachalo = nomer + 1
                gotovo = "".join(kusok).strip()
                kusok = []
                if gotovo:
                    yield gotovo
        kusok.append(stroka[nachalo:])
    hvost = "".join(kusok).strip()
    if hvost:
        yield hvost


def zalit_damp(url: str, damp: Path) -> None:
    """Залить файл дампа в базу по адресу `url`. Дамп сам роняет и создаёт таблицы.

    **Пачками, а не одним разговором.** Прежде весь дамп уезжал одним
    `execute`, то есть одним пакетом: сервер сверяет его с `max_allowed_packet`
    (64 МБ по умолчанию) и на копии крупнее предела рвал соединение. Это
    единственный путь восстановления в продукте, и отказывал он ровно на тех
    базах, ради которых копии и снимают.
    """
    dvizhok = create_engine(url, connect_args={"client_flag": CLIENT.MULTI_STATEMENTS})
    syroe = dvizhok.raw_connection()
    try:
        kursor = syroe.cursor()
        kursor.execute(f"SET SESSION lock_wait_timeout={ZHDAT_ZAMOK_SEKUND}")
        predel = _predel_paketa(kursor)

        def otdat(pachka: list[str]) -> None:
            if not pachka:
                return
            kursor.execute("\n".join(pachka))
            while kursor.nextset():
                pass

        pachka: list[str] = []
        dlina = 0
        with damp.open("r", encoding="utf-8") as fayl:
            for operator in operatory(fayl):
                # Оператор длиннее предела в одиночку не разрезать, не разбирая
                # значения. Такого быть не должно — свой дампер держит `INSERT`
                # в пятьсот строк, — но чужой дамп бывает и другим, и внятный
                # отказ лучше оборванного соединения на середине.
                if len(operator.encode("utf-8")) > predel * DOLYA_PAKETA:
                    raise ValueError(
                        "dump statement is larger than max_allowed_packet; "
                        "raise it on the server or re-take the copy"
                    )
                if pachka and dlina + len(operator) > predel:
                    otdat(pachka)
                    pachka, dlina = [], 0
                pachka.append(operator)
                dlina += len(operator)
            otdat(pachka)
        syroe.commit()
    finally:
        syroe.close()
        dvizhok.dispose()
