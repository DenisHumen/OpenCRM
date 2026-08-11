"""Ревизор схемы: соглашения проверяются кодом, а не на глаз.

Модулей стало много, и писали их разные руки. Соглашение, которое держится
только на внимательности, живёт до первого невнимательного дня — а расходятся
схемы тихо: таблица создалась, тесты модуля зелёные, и только через полгода
выясняется, что в одном месте деньги дробные, а в другом удаление клиента
уносит с собой историю звонков.

Тест обходит ВСЕ модели проекта разом, поэтому новый модуль попадает под
проверку сам, без правки этого файла. Исключения перечислены поимённо и с
объяснением: молчаливого исключения быть не должно.
"""

import pytest
from sqlalchemy import DateTime, Float, Numeric, String

from database.session import Base

# Импорт ради регистрации всех моделей в метаданных: без него ревизор увидит
# только те таблицы, которые кто-то успел импортировать раньше.
import database.models  # noqa: F401

TABLES = sorted(Base.metadata.tables.values(), key=lambda t: t.name)

# Колонки, которые хранят деньги. Ищем по имени: назвать поле `price` и
# сделать его дробным — самая частая и самая тихая ошибка в учётных системах.
MONEY_HINTS = ("amount", "price", "cost", "sum", "total", "paid", "prepaid", "balance")

# Внешние ключи, которым индекс не нужен, и почему.
INDEX_EXEMPT: dict[str, str] = {}

# Колонки, хранящие ключ этапа воронки. Внешним ключом он не является намеренно
# (этап можно заархивировать, а сделка обязана остаться читаемой), поэтому
# согласованность этих колонок СУБД не проверяет — её приходится проверять здесь.
STAGE_KEY_HOME = ("pipeline_stages", "key")
STAGE_KEY_COLUMNS = (
    ("deals", "stage"),
    ("deal_stage_changes", "from_stage"),
    ("deal_stage_changes", "to_stage"),
    # Куда акт выполненных работ переводит заявку. Четвёртое место, где живёт
    # ключ этапа, — и последнее, где о ширине можно забыть: обрезанный ключ на
    # акте означал бы, что «акт закрыт → заявка обязана стоять в его
    # `next_stage`» перестало сходиться, а это единственная проверка, по
    # которой расхождение «списалось, а этап не сменился» вообще обнаружимо.
    ("documents", "next_stage"),
)


def columns_with(table, hints):
    return [c for c in table.columns if any(h in c.name.lower() for h in hints)]


def test_every_table_is_reachable_from_models():
    """Таблица, появившаяся мимо models/__init__.py, не попадёт ни в этот
    ревизор, ни в чужие миграции. Пусть её отсутствие будет заметно."""
    assert TABLES, "метаданные пусты — модели не импортировались"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_foreign_keys_say_what_happens_on_delete(table):
    """Удаление родителя — это решение, а не умолчание.

    Без явного ondelete СУБД возьмёт своё поведение по умолчанию (RESTRICT в
    одних, NO ACTION в других), и что станет с историей звонков при удалении
    клиента, выяснится в тот день, когда клиента удалят.
    """
    for column in table.columns:
        for fk in column.foreign_keys:
            assert fk.ondelete, (
                f"{table.name}.{column.name} → {fk.target_fullname}: "
                "не сказано, что делать при удалении родителя"
            )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_foreign_keys_are_indexed(table):
    """По внешнему ключу всегда фильтруют — это его назначение.

    Без индекса «письма этого клиента» читают таблицу писем целиком. На сотне
    записей незаметно, на сотне тысяч — заметно всем сразу.
    """
    indexed = {tuple(idx.columns.keys()) for idx in table.indexes}
    single = {names[0] for names in indexed if len(names) == 1}
    # первая колонка составного индекса тоже годится для поиска по ней
    single |= {names[0] for names in indexed if len(names) > 1}
    single |= set(table.primary_key.columns.keys())
    for constraint in table.constraints:
        cols = list(getattr(constraint, "columns", []))
        if cols:
            single.add(cols[0].name)

    for column in table.columns:
        if not column.foreign_keys:
            continue
        key = f"{table.name}.{column.name}"
        if key in INDEX_EXEMPT:
            continue
        assert column.index or column.name in single or column.unique, (
            f"{key}: внешний ключ без индекса — поиск по нему читает таблицу целиком"
        )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_money_is_never_fractional(table):
    """0.1 + 0.2 != 0.3, и итог по колонке расходится с суммой карточек.

    Деньги в проекте — целые в минорных единицах (копейки, центы). Одна дробная
    колонка ломает это соглашение для всех отчётов, куда она попадёт.
    """
    for column in columns_with(table, MONEY_HINTS):
        assert not isinstance(column.type, (Float, Numeric)), (
            f"{table.name}.{column.name}: деньги дробным типом — "
            "храни целыми в минорных единицах"
        )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_time_is_stored_without_a_zone(table):
    """В базе — naive UTC, зона приводится на границе API.

    Смешать aware и naive в одной таблице значит получить сравнение, которое
    врёт на величину смещения. Ошибка тихая: напоминание просто сработает не
    тогда.
    """
    for column in table.columns:
        if isinstance(column.type, DateTime):
            assert not column.type.timezone, (
                f"{table.name}.{column.name}: время с зоной — "
                "в базе naive UTC, приводи на границе API"
            )


@pytest.mark.parametrize("table_name,column_name", STAGE_KEY_COLUMNS, ids=lambda v: str(v))
def test_stage_key_columns_are_as_wide_as_the_reference(table_name, column_name):
    """Одна величина — одна ширина, иначе часть значений хранится обрезанной.

    Ключ этапа генерируется `pipeline_service._free_key`: имя обрезается до 24
    символов, при совпадении добавляется суффикс — до 27. Лежит он в трёх местах,
    и внешнего ключа между ними нет, так что рассинхрон СУБД не поймает.

    Так уже было: `deals.stage` был VARCHAR(20) против String(32) в справочнике,
    это чинила миграция b2c48f7a91d5 — и не заметила ровно ту же пару колонок в
    соседней таблице `deal_stage_changes`. Проверка написана потому, что нашла
    вторую половину той же ошибки, а не про запас.

    Цена расхождения не в падении: SQLite длину не проверяет, MySQL в нестрогом
    режиме молча обрежет. Отчёт по воронке склеивает `to_stage` с
    `pipeline_stages.key`; обрезанный ключ не склеится ни с чем, и этап покажет
    ноль входов, ничем себя не выдав.
    """
    tables = Base.metadata.tables
    reference = tables[STAGE_KEY_HOME[0]].columns[STAGE_KEY_HOME[1]]
    column = tables[table_name].columns[column_name]

    assert isinstance(column.type, String) and isinstance(reference.type, String)
    assert column.type.length == reference.type.length, (
        f"{table_name}.{column_name}: ширина {column.type.length} против "
        f"{reference.type.length} у {STAGE_KEY_HOME[0]}.{STAGE_KEY_HOME[1]} — "
        "длинный ключ этапа обрежется молча, и заявка выпадет из отчётов"
    )


def test_models_and_migrations_do_not_diverge():
    """Схему умеют создавать двое, и они обязаны создавать одинаковую.

    `alembic upgrade head` строит её на сервере, `Base.metadata.create_all` —
    в lifespan. Пока эти двое расходятся, «работает локально» и «работает на
    сервере» перестают быть одним утверждением.

    Так уже было: `deals.stage` был VARCHAR(20) в миграции против String(32) в
    модели. SQLite длину VARCHAR не проверяет, поэтому весь набор тестов
    молчал, а на MySQL ключ этапа длиннее двадцати символов обрезался бы, и
    заявка переставала попадать в свою колонку.

    Проверка та же, что делает `alembic check`, но она стоит в наборе — а
    значит выполняется, в отличие от команды, которую надо не забыть запустить.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from database.session import Base, engine

    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert not diff, (
        "модели разошлись с миграциями:\n"
        + "\n".join(f"  {item}" for item in diff)
        + "\n\nПочинить миграцией, а не правкой модели под базу."
    )


def test_every_migration_can_be_rolled_back(tmp_path):
    """Круг: наверх, вниз до пустоты, снова наверх.

    Откат нужен ровно один раз — когда обновление на сервере встало посередине
    и надо вернуться к рабочей версии. Проверять его в этот момент поздно, а до
    этого момента никто и не проверяет: `upgrade` гоняется на каждом деплое,
    `downgrade` не гоняется никогда.

    Отдельная база в каталоге теста, а не общая: проверка сносит схему до
    основания, и делать это с базой, на которой стоит весь остальной набор,
    нельзя.

    Схема после круга сверяется с моделями. Отката «почти правильного» не
    бывает: миграция, которая при откате потеряла индекс, при следующем подъёме
    построит базу без него — и никто этого не заметит, пока запрос не начнёт
    читать таблицу целиком.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False

    command.upgrade(config, "head")

    # Убеждаемся, что миграции пошли именно сюда, ДО того как звать откат.
    # Если `env.py` снова начнёт затирать переданный адрес своим, откат уедет на
    # общую базу набора и снесёт её посреди прогона: соседние тесты посыпались
    # бы с непонятными ошибками, а причина осталась бы здесь. Пусть лучше
    # падает эта строка — она называет причину прямо.
    engine = create_engine(url)
    with engine.connect() as connection:
        built = MigrationContext.configure(connection).connection.dialect.get_table_names(connection)
    assert "clients" in built, (
        "миграции ушли не в тестовую базу: alembic подменил переданный адрес своим"
    )

    command.downgrade(config, "base")
    with engine.connect() as connection:
        left = MigrationContext.configure(connection).connection.dialect.get_table_names(connection)
    # `alembic_version` остаётся: это учётная таблица самого alembic, а не схемы.
    assert [name for name in left if name != "alembic_version"] == [], (
        "после отката до основания в базе остались таблицы: " + ", ".join(left)
    )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()

    assert not diff, (
        "после круга «вверх — вниз — вверх» схема разошлась с моделями:\n"
        + "\n".join(f"  {item}" for item in diff)
    )


def test_migratsiya_zapolnyaet_sklejku_tak_zhe_kak_prilozhenie(tmp_path):
    """Склейку `search_text` собирают двое, и они обязаны собирать одинаковую.

    Миграция заполняет уже населённую базу одним `UPDATE` средствами SQL, а
    приложение пересчитывает колонку мэппер-событием через `search_norm`.
    Разойдись эти двое — и половина базы (та, что была до обновления) начнёт
    находиться иначе, чем та, что заведена после. Без ошибки, без записи в
    журнал: просто часть карточек не находится.

    Мест, где они могут разойтись, ровно три, и все три проверяются здесь:
    приведение регистра (в миграции `lower()` знает только ASCII, если не
    подставить свою функцию), склейка с NULL (в обоих движках она даёт NULL
    целиком) и перевод строки внутри значения (описание с абзацами завело бы в
    склейку лишнюю границу поля).
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from database.query import search_norm

    url = f"sqlite:///{tmp_path / 'sklejka.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False

    # Останавливаемся ПЕРЕД нужной ревизией и заводим данные так, как они лежали
    # бы на боевой базе к моменту обновления.
    command.upgrade(config, "c2a58f31d9e0")

    klient = ("БРУСНИКА Иванов", "ООО Ромашка", "+380 67 111-22-33", "380671112233",
              "A@B.COM", "VIP,ЗАКАЗ")
    zayavka = ("НОУТБУК Ремонт", "первая строка\nвторая СТРОКА")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clients (name, company, phone, phone_norm, email, tags,"
                " messenger, created_at, updated_at) VALUES (:n, :c, :p, :pn, :e, :t,"
                " '', '2025-01-01', '2025-01-01')"
            ),
            dict(zip(("n", "c", "p", "pn", "e", "t"), klient)),
        )
        # Карточка, у которой заполнено только имя: склейка с пустыми полями не
        # должна обнулиться целиком (`coalesce` на каждой части).
        connection.execute(
            text(
                "INSERT INTO clients (name, company, phone, phone_norm, email, tags,"
                " messenger, created_at, updated_at) VALUES ('Пустой', '', '', '', '',"
                " '', '', '2025-01-01', '2025-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO deals (title, client_id, stage, sort_order, description,"
                " prepaid, lost_reason, created_at, updated_at) VALUES (:t, 1, 'new', 0,"
                " :d, 0, '', '2025-01-01', '2025-01-01')"
            ),
            {"t": zayavka[0], "d": zayavka[1]},
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        stalo = {
            "клиент": connection.scalar(text("SELECT search_text FROM clients WHERE id = 1")),
            "пустой": connection.scalar(text("SELECT search_text FROM clients WHERE id = 2")),
            "заявка": connection.scalar(text("SELECT search_text FROM deals WHERE id = 1")),
        }
    engine.dispose()

    assert stalo["клиент"] == search_norm(*klient), (
        "миграция собрала склейку клиента иначе, чем приложение:\n"
        f"  миграция: {stalo['клиент']!r}\n  приложение: {search_norm(*klient)!r}"
    )
    assert stalo["пустой"] == search_norm("Пустой", "", "", "", "", ""), (
        f"пустые поля обнулили склейку целиком: {stalo['пустой']!r}"
    )
    assert stalo["заявка"] == search_norm(*zayavka), (
        "миграция собрала склейку заявки иначе, чем приложение:\n"
        f"  миграция: {stalo['заявка']!r}\n  приложение: {search_norm(*zayavka)!r}"
    )


def test_running_migrations_does_not_silence_the_application_log(tmp_path):
    """Прогон миграций не выключает журнал приложения.

    `logging.config.fileConfig` по умолчанию глушит все логгеры, созданные до
    него, — а alembic зовёт его из `env.py` на каждом прогоне. Из отдельного
    процесса это безобидно, но миграции вызывают и изнутри приложения: так
    делают тесты, так однажды сделает автоматическое обновление на старте. Тогда
    вместе с alembic замолкает `opencrm.events`, и «наблюдатель упал молча»
    превращается из фигуры речи в описание происходящего.

    Поймано обратным прогоном набора: тесты про упавшего наблюдателя падали не
    сами по себе, а после соседа, гонявшего миграции.
    """
    import logging

    from alembic import command
    from alembic.config import Config

    talker = logging.getLogger("opencrm.events")

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'quiet.db'}")
    command.upgrade(config, "head")

    assert not talker.disabled, "миграции выключили журнал событий"

    # Свой приёмник, а не `caplog`: pytest вешает свой заново на каждый тест и
    # тем самым чинит последствие внутри одного прогона. В бою чинить некому —
    # приложение настраивает журнал один раз при старте. Проверяем то, что
    # важно на самом деле: запись доходит до приёмника, повешенного после
    # миграций.
    heard = []

    class Ear(logging.Handler):
        def emit(self, record):
            heard.append(record.getMessage())

    ear = Ear(level=logging.ERROR)
    talker.addHandler(ear)
    try:
        talker.error("подписчик упал")
    finally:
        talker.removeHandler(ear)

    assert heard == ["подписчик упал"], "после миграций ошибки не доходят до журнала"
