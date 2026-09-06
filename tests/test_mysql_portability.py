"""Схема обязана строиться в MySQL и означать в ней ровно то, что в моделях.

Беда этого рода заводится не там, где отказывает: DDL пишут в миграции, а
встаёт он на боевом сервере — посреди обновления, когда часть таблиц уже
создана, а `alembic_version` показывает ревизию, до которой дошли.

Расхождения бывают двух родов, и опасен второй.

*Шумные*: MySQL отвергает DDL, миграция обрывается. Неприятно, но видно сразу.
`TEXT NOT NULL DEFAULT ''` — ошибка 1101, `WHERE key = ...` — ошибка 1064.

*Тихие*: DDL проходит, а смысл меняется. Строки начинают сравниваться без учёта
регистра — и `UNIQUE` на токене сессии считает одним значением два разных.
`DATETIME` без указания точности округляет доли секунды — и лента событий
выстраивается случайно. `TEXT` меряется в байтах, а не в символах — и письмо на
русском не помещается в колонку, объявленную с запасом. Ни одно из этих
расхождений себя не объявляет.

Проверки ниже механические нарочно: они читают исходники моделей и миграций и
сверяют, какой DDL SQLAlchemy **соберёт** для диалекта mysql. Живой прогон
такого не заменяет: колонка с чужим сравнением или без долей секунды создаётся
молча и работает — до первого случая, ради которого правило и заведено. Здесь
же требование названо поимённо, по колонкам, и стоит это миллисекунды.

Живьём проверено отдельно, на MySQL 8.0.46: все миграции доходят до головы,
`database.schema_check` сообщает о полном совпадении с моделями, время без
правила уезжает на 3 часа, с правилом сходится в секунду.
"""

import ast
import pathlib

import pytest
from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import database.types  # noqa: F401 — регистрирует правила переносимости
from database import models  # noqa: F401 — наполняет metadata
from database.session import Base

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "database" / "migrations" / "versions"

#: Колонки, где регистр обязан значить. Опознавательные знаки и ключи доступа:
#: слить «AbC» с «abc» здесь — значит либо потерять запись, либо пустить не того.
POBAYTNO = {
    ("user_sessions", "token_hash"),
    ("share_links", "token"),
    ("mail_messages", "message_id"),
    ("products", "sku"),
}

#: Колонки, которым 64 КБ мало.
DLINNYE = {
    ("mail_messages", "body_text"),
    ("mail_messages", "body_html"),
    # Склейка названия заявки с её описанием: описание — `Text`, и на длинном
    # описании обычный TEXT упёрся бы в 65 535 БАЙТ (кириллица по два). MySQL в
    # нестрогом режиме обрежет молча — заявка перестанет находиться по концу
    # описания, ничем этого не объявив. Ровно тот класс беды, ради которого
    # написан этот файл: DDL проходит, а смысл меняется.
    ("deals", "search_text"),
    # Подробности напоминания: сюда кладут разбор с эмодзи из мессенджера, а
    # эмодзи в utf8mb4 — четыре байта. Двадцать тысяч знаков обычному TEXT
    # уже не по силам, и в строгом режиме это отказ на сохранении.
    ("tasks", "note"),
}


def _mysql_ddl(table_name: str) -> str:
    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=mysql.dialect()))


def _column_line(table_name: str, column: str) -> str:
    for line in _mysql_ddl(table_name).splitlines():
        stripped = line.strip()
        if stripped.startswith(column + " "):
            return stripped
    raise AssertionError(f"в DDL таблицы {table_name} нет колонки {column}")


def test_vsya_shema_sobiraetsya_dlya_mysql():
    """Ни одна таблица не должна спотыкаться на сборке DDL для MySQL."""
    beda = []
    for name in Base.metadata.tables:
        try:
            _mysql_ddl(name)
        except Exception as exc:  # noqa: BLE001 — интересен сам факт и причина
            beda.append(f"{name}: {exc}")
    assert beda == [], "DDL для MySQL не собирается:\n" + "\n".join(beda)


def test_vremya_hranit_doli_sekundy():
    """`DATETIME` без точности округляет доли секунды — и вверх.

    Событие в 12:00:00.7 легло бы как 12:00:01, то есть в будущее, а две записи
    одной секунды стали бы неразличимы по времени. Правило объявлено один раз
    для типа (database/types.py), поэтому проверяем разом все колонки: забытая
    колонка ничем себя не проявляет, кроме потерянных долей.
    """
    tupye = []
    for name, table in Base.metadata.tables.items():
        for column in table.c:
            if isinstance(column.type, DateTime):
                ddl = column.type.compile(dialect=mysql.dialect())
                if "(6)" not in ddl:
                    tupye.append(f"{name}.{column.name} -> {ddl}")
    assert tupye == [], (
        "в MySQL эти колонки потеряют доли секунды:\n" + "\n".join(tupye)
    )


@pytest.mark.parametrize(("table", "column"), sorted(POBAYTNO))
def test_opoznavatelnye_kolonki_sravnivayutsya_pobaytno(table, column):
    line = _column_line(table, column)
    assert "utf8mb4_bin" in line, (
        f"{table}.{column} в MySQL сравнивалась бы без учёта регистра: {line}\n"
        "Для токена это значит, что чужая запись, отличающаяся регистром, "
        "считается своей. Возьмите database.types.ExactString."
    )


@pytest.mark.parametrize(("table", "column"), sorted(DLINNYE))
def test_dlinnyy_tekst_ne_upiraetsya_v_64_kb(table, column):
    line = _column_line(table, column)
    assert "MEDIUMTEXT" in line.upper(), (
        f"{table}.{column} в MySQL ограничена 65 535 БАЙТАМИ: {line}\n"
        "Кириллица занимает по два байта, так что письмо на 40 тысяч знаков "
        "туда не влезет. Возьмите database.types.LongText."
    )


def _tekstovye_kolonki_s_prostym_default(path: pathlib.Path) -> list[str]:
    """Ищет `sa.Column(..., sa.Text(), ..., server_default='...')`.

    Именно строковый литерал: форму-выражение `sa.text("('')")` MySQL принимает,
    а голый литерал — нет.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guilty = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"Column", "mapped_column"}:
            continue
        tekstovaya = any(
            isinstance(arg, ast.Call)
            and getattr(arg.func, "attr", getattr(arg.func, "id", "")) in {"Text", "TEXT"}
            or isinstance(arg, ast.Name)
            and arg.id in {"Text", "TEXT"}
            for arg in node.args
        )
        if not tekstovaya:
            continue
        for kw in node.keywords:
            if kw.arg == "server_default" and isinstance(kw.value, ast.Constant):
                guilty.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno} "
                    f"server_default={kw.value.value!r}"
                )
    return guilty


def test_u_tekstovyh_kolonok_default_zapisan_vyrazheniem():
    """MySQL запрещает обычный DEFAULT у TEXT/BLOB — ошибка 1101.

    Миграция обрывается на середине: часть таблиц создана, часть нет, а
    `alembic_version` показывает ревизию, до которой дошли. Форму `DEFAULT ('')`
    MySQL принимает (проверено на 8.0.46).
    """
    guilty = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        guilty += _tekstovye_kolonki_s_prostym_default(path)
    for path in sorted((ROOT / "database" / "models").glob("*.py")):
        guilty += _tekstovye_kolonki_s_prostym_default(path)
    assert guilty == [], (
        "MySQL отвергнет эти колонки (ошибка 1101), и миграция оборвётся:\n"
        + "\n".join(guilty)
        + "\nЗапишите значение выражением: server_default=sa.text(\"('')\") "
        "или database.types.text_default()."
    )


def test_drayver_mysql_obyavlen_v_zavisimostyah():
    """Без драйвера переезд упирается в «No module named pymysql» на первом шаге."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pymysql" in pyproject.lower(), (
        "в pyproject.toml нет драйвера MySQL — подключиться к нему нечем"
    )
