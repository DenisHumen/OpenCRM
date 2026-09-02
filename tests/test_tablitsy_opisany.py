"""Каждая таблица названа в `docs/03-database.md`.

**Зачем сторож.** Документ сам обещает полноту: «перечень, выглядящий полным и
таковым не являющийся, хуже короткого». Обещание держится руками, а таблицы
заводятся кодом — и расходятся они молча.

Разошлись уже: `deal_lines` — таблица, из которой считается вся бронь и сумма
заявки, — не была названа в схеме ни строкой, при том что перечень перечисляет
даже счёт от змейки со страницы обслуживания. Найдено сплошным сличением
02.09.2026, одна из сорока.

Проверка сравнивает ИМЕНА, а не описания: судить, верно ли описано решение, она
не умеет. Её работа — не дать таблице остаться неназванной вовсе.
"""

import pathlib

import database.models  # noqa: F401 — импорт регистрирует таблицы в метаданных
from database.session import Base

KOREN = pathlib.Path(__file__).resolve().parent.parent
SHEMA = KOREN / "docs" / "03-database.md"


def test_perebor_tablits_ne_pustoy():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    assert len(Base.metadata.tables) > 30, (
        f"таблиц в метаданных {len(Base.metadata.tables)} — модели не импортировались, "
        "и следующая проверка зеленела бы впустую"
    )


def test_kazhdaya_tablitsa_nazvana_v_sheme():
    tekst = SHEMA.read_text(encoding="utf-8")
    bezymyannye = sorted(imya for imya in Base.metadata.tables if imya not in tekst)
    assert not bezymyannye, (
        "не названы в docs/03-database.md: "
        + ", ".join(bezymyannye)
        + ". Таблице либо место в перечне «Таблицы, у которых своей схемы здесь нет», "
        "либо свой раздел в «Пояснениях к решениям», если в ней есть что объяснять"
    )
