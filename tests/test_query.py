"""Общий слой запросов: страницы и поиск по подстроке.

Проверки здесь двойные: и сам приём (`database/query.py`), и то, что им
пользуются все репозитории. Первое без второго бесполезно — работающий
`page_of`, о котором половина кода не знает, чинит половину бед.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite

from database.models import Client
from database.query import MAX_PER_PAGE, clamp_per_page, contains, escape_like, offset_for, page_of
from database.repositories import clients as clients_repo


# --- смещение страницы -------------------------------------------------------


@pytest.mark.parametrize("page", [0, -1, -1000])
def test_smeshchenie_ne_byvaet_otritsatelnym(page):
    """`OFFSET -50` MySQL считает синтаксической ошибкой — то есть пятисоткой.

    SQLite такое прощает и молча отдаёт первую страницу, поэтому на нынешней
    базе беда не видна вовсе. Появится она в день переезда, сразу и во всех
    списках, — если позволить отрицательному числу дойти до запроса.
    """
    assert offset_for(page, 50) == 0


def test_smeshchenie_schitaetsya_po_obrezannomu_razmeru():
    """Обрезали размер страницы — обрезаем и смещение, иначе страницы разъедутся.

    Спросили `per_page=1000` при потолке 500: если смещение второй страницы
    считать по тысяче, а строк отдавать пятьсот, то записи с 500-й по 999-ю не
    покажет ни одна страница. Пропажа тихая: и первая, и вторая страница
    выглядят полными.
    """
    assert offset_for(2, 10**6) == MAX_PER_PAGE
    assert clamp_per_page(10**6) == MAX_PER_PAGE
    assert clamp_per_page(0) == 1
    assert clamp_per_page(-5) == 1


def test_stranitsa_nulevaya_otdayot_pervuyu(db):
    for name in ("сгмтчк-один", "сгмтчк-два", "сгмтчк-три"):
        db.add(Client(name=name))
    db.flush()

    rows, total = clients_repo.search(db, q="сгмтчк-", page=0, per_page=2)
    assert total == 3
    assert len(rows) == 2


def test_ogromnyy_razmer_stranitsy_obrezaetsya(db):
    """Иначе «покажи всё» на выросшей базе — это вся таблица разом в память."""
    stmt = select(Client).order_by(Client.id)
    rows, _total = page_of(db, stmt, page=1, per_page=10**9)
    assert len(rows) <= MAX_PER_PAGE

    compiled = str(stmt.limit(clamp_per_page(10**9)).compile(compile_kwargs={"literal_binds": True}))
    assert f"LIMIT {MAX_PER_PAGE}" in compiled


# --- шаблоны в том, что ввёл человек ----------------------------------------


def test_procent_ischet_procent_a_ne_vsyo(db):
    """Поиск по одному знаку `%` возвращал всю таблицу.

    На базе с тысячами карточек это выгрузка всего справочника клиентов по
    случайному нажатию — и человеком, которому положено видеть свои десять.
    """
    db.add(Client(name="прцнт Скидка 100% для своих"))
    db.add(Client(name="прцнт Обычный клиент"))
    db.add(Client(name="прцнт Ещё один"))
    db.flush()

    everything, all_total = clients_repo.search(db, per_page=MAX_PER_PAGE)
    assert all_total >= 3, "в базе должно быть больше трёх карточек, иначе проверка ничего не ловит"

    rows, total = clients_repo.search(db, q="%")
    assert total < all_total, "поиск по одному «%» вернул всю таблицу"
    assert [c.name for c in rows if c.name.startswith("прцнт")] == ["прцнт Скидка 100% для своих"]


def test_podchyorkivanie_ne_zamenyaet_lyuboy_simvol(db):
    """`_` в LIKE — «любой один символ», и в поисковую строку он попадал как есть."""
    db.add(Client(name="пдчрк_один"))
    db.add(Client(name="пдчркХодин"))
    db.flush()

    rows, total = clients_repo.search(db, q="пдчрк_один")
    assert [c.name for c in rows] == ["пдчрк_один"]
    assert total == 1


def test_znak_ekranirovaniya_ishchetsya_kak_bukva(db):
    """Сам `/` — обычный символ в имени файла или адресе, искать его надо буквально."""
    db.add(Client(name="экрзнк ООО «Путь/Дорога»"))
    db.add(Client(name="экрзнк Посторонний"))
    db.flush()

    rows, total = clients_repo.search(db, q="Путь/Дорога")
    assert [c.name for c in rows] == ["экрзнк ООО «Путь/Дорога»"]
    assert total == 1


def test_obychnyy_poisk_ne_slomalsya(db):
    """Экранирование не должно мешать тому, ради чего поиск и нужен.

    И регистр по-прежнему не важен — на SQLite это держится на подмене `lower()`
    из `database/session.py`, знающей кириллицу.
    """
    db.add(Client(name="брснк Брусника"))
    db.add(Client(name="брснк БРУСНИКА и партнёры"))
    db.add(Client(name="брснк Другое"))
    db.flush()

    rows, total = clients_repo.search(db, q="брснк брусника")
    assert total == 2


def test_escape_like_stavit_svoy_znak_pervym():
    """Иначе `/` из введённого станет экранированием для следующей буквы и съест её."""
    assert escape_like("a/b") == "a//b"
    assert escape_like("a%b") == "a/%b"
    assert escape_like("a_b") == "a/_b"
    assert escape_like("/%") == "///%"


def test_ekranirovanie_odinakovo_na_oboikh_dvizhkakh():
    """`ESCAPE '\\'` на MySQL читается по-разному в зависимости от режима сервера.

    Знак выбран так, чтобы оба диалекта собрали одно и то же выражение: иначе
    поиск, проверенный на SQLite, поведёт себя иначе в бою на MySQL — и заметить
    это будет нечем, потому что ошибки не случится, просто найдётся не то.
    """
    stmt = select(Client.id).where(contains(Client.name, "a_b"))
    sqlite_sql = str(stmt.compile(dialect=sqlite.dialect()))
    mysql_sql = str(stmt.compile(dialect=mysql.dialect()))
    assert "ESCAPE '/'" in sqlite_sql
    assert "ESCAPE '/'" in mysql_sql


# --- все репозитории пользуются общим ---------------------------------------


def test_ni_odin_repozitoriy_ne_schitaet_smeshchenie_sam():
    """Стоит одному считать по-своему — и починка перестаёт быть общей."""
    import pathlib

    repo_dir = pathlib.Path(__file__).resolve().parent.parent / "database" / "repositories"
    guilty = [
        path.name
        for path in repo_dir.glob("*.py")
        if "offset((page" in path.read_text(encoding="utf-8")
    ]
    assert guilty == [], f"считают смещение сами: {guilty}"


def test_poisk_po_stroke_vezde_ekranirovan():
    """`ilike(f"%{...}%")` мимо `contains` — это опять «%» вместо всей таблицы."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    guilty = []
    for path in list((root / "database").rglob("*.py")) + list((root / "core").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.i?like\(", line) and "escape=" not in line:
                guilty.append(f"{path.relative_to(root)}:{number}")
    assert guilty == [], "поиск по подстроке мимо contains():\n" + "\n".join(guilty)
