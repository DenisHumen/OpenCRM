"""нормализованная колонка поиска у клиентов и заявок

Revision ID: d3b8c05f1e2a
Revises: c2a58f31d9e0
Create Date: 2026-08-11 10:00:00.000000

Общий поиск (Ctrl+K) отвечал 8834 мс на большой базе — 200 000 клиентов,
400 000 заявок. Три постоянных множителя, и это третий из них: подменённая
Python-функция `lower()` вызывалась на КАЖДУЮ строку и КАЖДУЮ колонку. У
клиента это пять вызовов на строку, миллион на один запрос.

Приём тот же, что уже отработан на `phone_norm`: рядом с исходными полями
лежит их склейка, приведённая к нижнему регистру. Поиск сравнивает две заранее
приведённые строки — `lower()` из SQL уходит совсем, и после переезда на MySQL
поведение не меняется.

Замерено на большой базе (медиана, только чтение): клиенты 295 → 38,6 мс,
заявки 354 → 96,5 мс.

**Индекса на `search_text` нет и не предлагается.** Подстрока с ведущим `%` его
не берёт ни в SQLite, ни в MySQL, а префиксный по склейке бессмыслен: она
начинается с имени, и «иванов» не нашёл бы «Алексея Иванова». Колонка здесь не
индекс, а более дешёвый стог сена.

**И FTS5 тоже нет — и не будет.** FTS5 порождает пять теневых таблиц, которых
нет в `Base.metadata`: на свежей установке базу строит `web/main.py:_ensure_schema`
через `create_all`, значит `deals_fts` там не появится никогда, `schema_check`
промолчит (он сверяет только модели), `/healthz` ответит 200 — и поиск упадёт
«no such table». Это ровно то запрещённое третье состояние «работает, но схема
не та», которого в проекте не должно существовать. MySQL FULLTEXT хуже:
`Index(..., mysql_prefix='FULLTEXT')` в SQLite собирается ОБЫЧНЫМ индексом, все
сторожа зелёные, пользы ноль, а запрос требует диалектной ветки.

Порядок миграции — по правилу для населённой базы. Новых таблиц и внешних
ключей нет, поэтому шагов три: колонка со `server_default` (существующие строки
получают пустую строку, и `NOT NULL` выполняется сразу — так же сделано для
`phone_norm` в `a9e64c17f235`), затем ОДИН `UPDATE` на таблицу без построчного
обхода через ORM, и всё.

`downgrade` сносит обе колонки: миграция добавочная и обратимая целиком.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from database.query import SEARCH_SEP
from database.session import unicode_registr_dlya


revision: str = "d3b8c05f1e2a"
down_revision: Union[str, None] = "c2a58f31d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Ширина склейки клиента: сумма ширин слагаемых (200+200+64+32+255+500 = 1251)
#: плюс пять разделителей, с запасом.
SHIRINA_KLIENTA = 1300

#: Таблицы описаны здесь своими руками, а не взяты из моделей. Миграция обязана
#: собираться и через год, когда модель уедет вперёд: `Client.search_text`
#: может стать шире, а эта ревизия должна остаться той, что была.
_clients = sa.table(
    "clients",
    sa.column("search_text", sa.String),
    sa.column("name", sa.String),
    sa.column("company", sa.String),
    sa.column("phone", sa.String),
    sa.column("phone_norm", sa.String),
    sa.column("email", sa.String),
    sa.column("tags", sa.String),
)

_deals = sa.table(
    "deals",
    sa.column("search_text", sa.Text),
    sa.column("title", sa.String),
    sa.column("description", sa.Text),
)


def _sklejka(*columns):
    """Склейка колонок ровно та же, что собирает `database.query.search_norm`.

    Три вещи, каждая из которых нужна:

    * `coalesce(col, '')` на КАЖДОЙ части — в обоих движках конкатенация с NULL
      даёт NULL, и одна пустая колонка обнулила бы всю строку целиком;
    * `replace(col, '\\n', ' ')` — разделителем служит перевод строки, и
      описание заявки, в котором он есть сам по себе, завело бы в склейку лишнюю
      границу поля. Python-сторона делает то же самое, и разъехаться им нельзя;
    * сложение средствами SQLAlchemy, а не `func.concat`: она соберёт `||` для
      SQLite и `concat()` для MySQL — приём уже применён в фильтре по меткам.

    `type_=sa.String()` у `replace` не украшение: без объявленного типа
    SQLAlchemy считает результат функции нетипизированным и собирает сложение
    как АРИФМЕТИЧЕСКОЕ `+`. В SQLite это молча даёт `0.0` вместо склейки, и вся
    колонка заполнилась бы нулями — без единой ошибки.
    """
    parts = []
    for column in columns:
        if parts:
            parts.append(sa.literal(SEARCH_SEP))
        parts.append(
            sa.func.replace(
                sa.func.coalesce(column, ""), SEARCH_SEP, " ", type_=sa.String()
            )
        )
    skleyka = parts[0]
    for part in parts[1:]:
        skleyka = skleyka + part
    return sa.func.lower(skleyka)


def upgrade() -> None:
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_text",
                sa.String(length=SHIRINA_KLIENTA),
                nullable=False,
                server_default="",
            )
        )

    with op.batch_alter_table("deals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_text",
                # MEDIUMTEXT в MySQL: обычный TEXT это 65 535 БАЙТ, кириллица по
                # два — склейка названия с длинным описанием упёрлась бы в
                # потолок и была бы молча обрезана в нестрогом режиме.
                sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
                nullable=False,
                # Обычный DEFAULT '' у TEXT в MySQL — ошибка 1101 и оборванная
                # посреди миграция. Форму-выражение принимают оба движка.
                server_default=sa.text("('')"),
            )
        )

    bind = op.get_bind()
    # Без этой строки `lower()` в двух UPDATE ниже знает только ASCII: свой
    # движок alembic строит через `engine_from_config`, мимо `_make_engine`, и
    # подписчика с подменой на нём нет. «БРУСНИКА» осталась бы заглавной на всей
    # уже населённой базе, и карточка перестала бы находиться — молча, ровно на
    # том обновлении, ради которого всё делается.
    unicode_registr_dlya(bind)

    # По одному UPDATE на таблицу. Построчного обхода через ORM здесь быть не
    # может: 200 000 клиентов и 400 000 заявок — это минуты вместо секунд, а
    # обновление на боевом сервере ждёт эту миграцию до старта приложения.
    bind.execute(
        _clients.update().values(
            search_text=_sklejka(
                _clients.c.name,
                _clients.c.company,
                _clients.c.phone,
                _clients.c.phone_norm,
                _clients.c.email,
                _clients.c.tags,
            )
        )
    )
    bind.execute(
        _deals.update().values(
            search_text=_sklejka(_deals.c.title, _deals.c.description)
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("deals", schema=None) as batch_op:
        batch_op.drop_column("search_text")
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.drop_column("search_text")
