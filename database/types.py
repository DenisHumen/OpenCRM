"""Типы колонок, у которых поведение MySQL надо задать явно.

Три места MySQL решает по-своему, и каждое молча портит данные или ломает
защиту: сравнение строк (регистр и хвостовые пробелы), длина ключа под
индексом, умолчание у TEXT.
"""

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles

#: Строка, которую сравнивают ПОБАЙТНО. Для токенов и хэшей.
#:
#: Сравнение в MySQL по умолчанию регистронезависимо (`utf8mb4_0900_ai_ci`), а
#: токен — base64, где регистр значащий: `UNIQUE` отвергает законно разные
#: токены, поиск находит ЧУЖУЮ сессию, а перебор дешевеет в 2^N раз.
def ExactString(length: int):  # noqa: N802 — имя типа, а не функции
    return String(length).with_variant(
        mysql.VARCHAR(length, collation="utf8mb4_bin"), "mysql"
    )


#: Длинный текст: тела писем, снимки для печати, вложенный JSON.
#:
#: `TEXT` — 65 535 **байт**, а не символов: кириллица по два, эмодзи четыре.
#: Письмо на 40 тысяч знаков не влезает: MySQL отвечает «Data too long» или, в
#: нестрогом режиме, молча режет посреди HTML — не открыть. `MEDIUMTEXT`: 16 МБ.
LongText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


#: Время с долями секунды — правило для ВСЕХ колонок сразу.
#:
#: `DATETIME` без точности округляет доли ВВЕРХ: событие 12:00:00.7 ложится в
#: будущее, а лента одной секунды выстраивается случайно. У типа, а не по
#: колонкам: их сотня в 20 файлах и 20 миграциях, а забытая молча теряет доли.
@compiles(DateTime, "mysql")
def _datetime_s_mikrosekundami(type_, compiler, **kw):
    return "DATETIME(6)"



def text_default(value: str = ""):
    """`server_default` для колонки TEXT — в форме, которую MySQL принимает.

    MySQL запрещает обычный `DEFAULT` у TEXT/BLOB (ошибка 1101, миграция рвётся
    на середине), но форму-выражение `DEFAULT ('')` принимает с 8.0.13 — и в
    `CREATE TABLE`, и в `ALTER TABLE ADD COLUMN`. На 8.0.46 проверено вставкой.
    """
    return text("('{}')".format(value.replace("'", "''")))
