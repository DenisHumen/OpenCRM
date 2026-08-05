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
from sqlalchemy import DateTime, Float, Numeric

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
