"""Общий слой запросов: страницы и поиск по подстроке.

Проверки здесь двойные: и сам приём (`database/query.py`), и то, что им
пользуются все репозитории. Первое без второго бесполезно — работающий
`page_of`, о котором половина кода не знает, чинит половину бед.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql

from database.models import Client
from database.query import (
    MAX_PER_PAGE,
    clamp_per_page,
    contains,
    contains_norm,
    escape_like,
    offset_for,
    page_of,
    page_without_total,
    search_norm,
)
from database.repositories import clients as clients_repo


# --- смещение страницы -------------------------------------------------------


@pytest.mark.parametrize("page", [0, -1, -1000])
def test_smeshchenie_ne_byvaet_otritsatelnym(page):
    """`OFFSET -50` MySQL считает синтаксической ошибкой — то есть пятисоткой.

    Считает смещение одна функция на весь проект, и проверяется здесь именно
    она: позволь отрицательному числу дойти до запроса — и пятисотка придёт
    сразу во всех списках.
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


# --- страница без счёта ------------------------------------------------------


def test_stranitsa_bez_schyota_znaet_est_li_eshchyo(db):
    """Лишняя строка вместо второго запроса: пришла — значит есть ещё.

    Точный `total` стоит ровно столько же, сколько сама выборка (замерено:
    заявки 3128 против 3165 мс), а в командной палитре его никто не показывает.
    Проверяем оба края — при семи совпадениях «есть ещё», при шести нет, — и то,
    что сама страница от разведки не поехала: лишняя строка отбрасывается, а не
    показывается седьмой.
    """
    for i in range(7):
        db.add(Client(name=f"бзсчт-{i:02d}"))
    db.flush()

    stmt = select(Client).where(Client.name.like("бзсчт-%")).order_by(Client.name)
    rows, has_more = page_without_total(db, stmt, page=1, per_page=6)
    assert len(rows) == 6, "страница отдала не тот размер"
    assert has_more is True, "седьмое совпадение есть, а «есть ещё» не сказано"

    rovno, has_more = page_without_total(db, stmt.limit(None), page=1, per_page=7)
    assert len(rovno) == 7
    assert has_more is False, "обещано продолжение, которого нет"

    # Страницы не теряют и не двоят строк — то же требование, что и у `page_of`.
    sobrano = []
    for page in range(1, 5):
        part, _ = page_without_total(db, stmt, page=page, per_page=2)
        sobrano.extend(c.id for c in part)
    assert len(sobrano) == 7 and len(set(sobrano)) == 7, sobrano


def test_palitra_ne_delaet_vtorogo_zaprosa(db):
    """На одну группу палитры — ровно один запрос и ни одного `count(`.

    Это и есть весь смысл шага: не удешевить счёт, а убрать его. Проверка
    механическая, через `before_cursor_execute`, — по времени такую пропажу не
    заметить, а вернуть счёт обратно можно одной строкой.
    """
    from sqlalchemy import event

    from database.session import engine

    statements = []
    listener = lambda conn, cursor, statement, *rest: statements.append(statement)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        clients_repo.search_top(db, q="чего-то-заведомо-нет", per_page=6)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert [s for s in statements if "count(" in s.lower()] == [], (
        "палитра всё ещё считает найденное:\n" + "\n".join(statements)
    )
    assert len(statements) == 1, "на группу палитры ушёл не один запрос:\n" + "\n".join(statements)


# --- нормализованная колонка поиска -----------------------------------------


def test_normalizatsiya_odna_na_zapis_i_chtenie(db):
    """Одна функция на обе стороны: ею пишут колонку и ею же приводят запрос.

    Разъехавшись, эти двое дали бы молча не находящиеся карточки — без ошибки и
    без записи в журнал. Поэтому проверяется не «поиск работает», а сама
    `search_norm`: регистр приводится, кириллица тоже (её приводит Python, а не
    SQL), а `%`, `_` и `/` остаются буквами.
    """
    assert search_norm("БРУСНИКА") == "брусника"
    assert search_norm("Ноутбук ASUS") == "ноутбук asus"
    # Пустое поле не съедает разделитель и не обнуляет склейку целиком.
    assert search_norm("Иван", "", "ООО") == "иван\n\nооо"
    assert search_norm(None) == ""
    # Перевод строки внутри значения — пробел: иначе описание с абзацами
    # завело бы в склейку лишнюю границу поля.
    assert search_norm("первая\nвторая") == "первая вторая"

    nuzhnyy = Client(name="нрмлз ООО «Путь/Дорога» 100% скидка_один")
    db.add(nuzhnyy)
    db.add(Client(name="нрмлз Посторонний"))
    db.flush()

    # База у набора общая и переживает файл, поэтому смотрим только на свои
    # карточки: соседний тест мог оставить в ней «Ромашку» и проценты.
    for needle in ("нрмлз ООО «Путь/Дорога»", "100% скидка_один", "НРМЛЗ ООО «ПУТЬ/ДОРОГА»"):
        rows, _total = clients_repo.search(db, q=needle)
        svoi = [c.name for c in rows if c.name.startswith("нрмлз")]
        assert svoi == [nuzhnyy.name], f"{needle}: {svoi}"


def test_sovpadenie_ne_pereskakivaet_granitsu_polya(db):
    """Склейка не должна находить то, чего не находили пять отдельных `OR`.

    Раньше колонки проверялись раздельно, и запрос «Иван Ромашка» не мог
    совпасть сразу с именем и фирмой. Склей их пробелом — и совпал бы: поиск
    начал бы находить карточки по словам из РАЗНЫХ полей, то есть отвечать не на
    тот вопрос. Разделитель — перевод строки, которого в строке поиска нет.

    Слова взяты в том порядке, в каком поля стоят в склейке (имя, потом фирма):
    иначе проверка проходила бы и с пробелом — просто потому, что подстроки в
    обратном порядке в склейке нет всё равно.
    """
    nuzhnyy = Client(name="грнц Иван", company="грнцРомашка")
    db.add(nuzhnyy)
    db.flush()

    def svoi(needle: str) -> list[int]:
        rows, _total = clients_repo.search(db, q=needle)
        return [c.id for c in rows if (c.name or "").startswith("грнц")]

    assert svoi("грнц Иван грнцРомашка") == [], "совпадение перескочило границу поля"

    # А по каждому полю в отдельности карточка находится как и раньше.
    assert svoi("грнц Иван") == [nuzhnyy.id]
    assert svoi("грнцРомашка") == [nuzhnyy.id]


# --- шаблоны в том, что ввёл человек ----------------------------------------


def test_procent_ischet_procent_a_ne_vsyo(db):
    """Поиск по одному знаку `%` возвращал всю таблицу.

    На базе с тысячами карточек это выгрузка всего справочника клиентов по
    случайному нажатию — и человеком, которому положено видеть svoi десять.
    """
    db.add(Client(name="прцнт Скидка 100% для svoiх"))
    db.add(Client(name="прцнт Обычный клиент"))
    db.add(Client(name="прцнт Ещё один"))
    db.flush()

    everything, all_total = clients_repo.search(db, per_page=MAX_PER_PAGE)
    assert all_total >= 3, "в базе должно быть больше трёх карточек, иначе проверка ничего не ловит"

    rows, total = clients_repo.search(db, q="%")
    assert total < all_total, "поиск по одному «%» вернул всю таблицу"
    assert [c.name for c in rows if c.name.startswith("прцнт")] == ["прцнт Скидка 100% для svoiх"]


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

    И регистр по-прежнему не важен: обе стороны сравнения приведены заранее
    одной и той же `search_norm`.
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


def test_znak_ekranirovaniya_ne_obratnaya_kosaya():
    """Обратная косая читается по-разному в зависимости от режима сервера.

    `ESCAPE '\\'` означает разное при `NO_BACKSLASH_ESCAPES` и без него, а режим
    задаётся на сервере и меняется не нами. Ошибки при этом не случится: поиск
    просто найдёт не то, и молча. Поэтому знак выбран посторонний.
    """
    sql = str(
        select(Client.id)
        .where(contains(Client.name, "a_b"))
        .compile(dialect=mysql.dialect())
    )
    assert "ESCAPE '/'" in sql
    assert "\\" not in sql


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


def test_ravnye_stroki_vsegda_v_odnom_poryadke(db):
    """Списки сортируются по времени правки, а оно хранится с точностью до секунды.

    Пять карточек, заведённых подряд, получают одну и ту же отметку, и порядок
    между ними не задан ничем. Пока страницу отдают целиком, это незаметно; но
    выдача идёт кусками, и каждая страница — отдельный запрос. База вправе
    разложить равные строки в этот раз иначе, чем в прошлый: тогда одна карточка
    попадает на две страницы, а другая ни на одну, и человек уверен, что клиент
    пропал.
    """
    for i in range(6):
        db.add(Client(name=f"тайбрейк-{i:02d}"))
    db.flush()

    collected = []
    for page in range(1, 4):
        rows, _total = clients_repo.search(db, q="тайбрейк-", page=page, per_page=2)
        collected.extend(client.id for client in rows)

    assert len(collected) == 6, "страница потеряла или продублировала строки"
    assert len(set(collected)) == 6, f"строка попала на две страницы: {collected}"


def test_pervichnyy_klyuch_dopisan_v_sortirovku():
    """Проверяем сам запрос, а не только его исход.

    Исход теста выше сходится и на маленькой выборке, где равные строки обычно
    ложатся в один и тот же порядок сами собой. Обещания такого нет: MySQL при
    сортировке во временном файле порядка среди равных не гарантирует вовсе, и
    день, когда выдача не поместится в память, ничем не отмечен. Поэтому
    смотрим на текст запроса.
    """
    from database.query import _with_tiebreak

    stmt = select(Client).order_by(Client.updated_at.desc())
    sql = " ".join(str(_with_tiebreak(stmt).compile()).split())
    assert "ORDER BY clients.updated_at DESC, clients.id DESC" in sql, sql


def test_stranitsa_uhodit_v_bazu_s_klyuchom(db):
    """Ключ дописывает не помощник сам по себе, а СТРАНИЦА — по-настоящему.

    Соседняя проверка зовёт `_with_tiebreak` напрямую и смотрит на текст. Она
    остаётся зелёной, если из `page_of` убрать сам вызов: помощник цел, звать
    его перестали. А `page_of` — единственное место, где ключ дописывается всем
    страничным спискам разом, то есть половине выборок проекта.

    Поймано подрывом: строка `ordered = _with_tiebreak(stmt)`, заменённая на
    `ordered = stmt`, не уронила ни одной проверки в наборе.

    Поэтому смотрим на SQL, УШЕДШИЙ В БАЗУ, а не на выражение в коде.
    """
    from database.query import page_of
    from tests.conftest import Zaprosy

    with Zaprosy() as zapisano:
        page_of(db, select(Client).order_by(Client.updated_at.desc()), page=1, per_page=5)

    vyborki = [
        s for s in zapisano.spisok
        if "FROM clients" in s and "ORDER BY" in s
    ]
    assert vyborki, "страница не сходила в базу — проверять нечего"
    assert any("clients.id DESC" in s for s in vyborki), (
        "страница ушла в базу без первичного ключа в сортировке: " + "; ".join(vyborki)
    )


def test_schyotchik_ne_sortiruet(db):
    """Считать количество можно без сортировки — а база об этом не знает.

    `count(*)` от порядка не зависит, но подзапрос с `ORDER BY` заставляет базу
    честно отсортировать всё найденное, чтобы затем просто сосчитать. На таблице
    в сотню тысяч строк это лишняя сортировка на каждый показ страницы.
    """
    from sqlalchemy import event

    from database.session import engine

    statements = []
    listener = lambda conn, cursor, statement, *rest: statements.append(statement)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        clients_repo.search(db, q="чего-то-заведомо-нет", page=1, per_page=10)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    counting = [s for s in statements if "count(" in s]
    assert counting, "запрос на количество не найден"
    for statement in counting:
        assert "ORDER BY" not in statement.upper(), f"счётчик сортирует: {statement}"


# --- тип итога агрегата ------------------------------------------------------


def test_sum_v_mysql_prihodit_decimal(db):
    """Замер самой ловушки: `SUM()` отдаёт `Decimal`, а не целое.

    Проверка нужна не ради `as_int`, а ради того, ЧТО она утверждает про базу.
    Пока набор шёл на файле, тот же запрос давал `int`, и все подписи
    репозиториев (`-> int`) были правдой сами собой. На MySQL это перестало быть
    правдой молча — и первым сломался показ остатка склада, потому что
    `format_quantity` собирает строку через `f"{frac:03d}"`, а `Decimal` такой
    формат не принимает.

    Развалится этот тест ровно в одном случае: драйвер начнёт приводить итог
    сам. Тогда `as_int` станет лишним — но узнать об этом надо здесь, а не по
    исчезнувшей ошибке.
    """
    from decimal import Decimal

    from sqlalchemy import func

    from database.models import StockMove
    from database.query import as_int

    syroy = db.scalar(select(func.coalesce(func.sum(StockMove.quantity_milli), 0)))
    assert isinstance(syroy, Decimal), f"итог приехал {type(syroy).__name__}"
    assert isinstance(as_int(syroy), int)


def test_as_int_ne_teryaet_znachenie():
    """Приведение обязано быть точным: остаток и деньги — целые, и целые точные."""
    from decimal import Decimal

    from database.query import as_int

    assert as_int(None) == 0
    assert as_int(0) == 0
    assert as_int(Decimal("85000")) == 85000
    assert as_int(Decimal("-100")) == -100
    # Целое проходит насквозь, а не через строку: подпись обещает `int`, и
    # вызывающему всё равно, каким движком посчитан итог.
    assert as_int(42) == 42 and isinstance(as_int(42), int)
