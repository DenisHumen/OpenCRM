"""Перенос данных из одной базы в другую.

Живого MySQL в наборе тестов нет, а логика переноса от движка не зависит:
сверка ревизий, очистка цели, пакеты, сличение итогов — всё это одинаково
работает и на паре SQLite. Проверяем здесь именно её, а совместимость с самим
MySQL стережёт `test_mysql_portability.py`.

Настоящий переезд на MySQL 8.0.46 прогонялся отдельно, на населённой базе:
5925 строк, суммы по целым столбцам сошлись, текст в 16 800 знаков с эмодзи
доехал посимвольно, исходный файл не изменился ни на байт.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import models  # noqa: F401 — наполняет metadata
from database.session import Base
from scripts.migrate_to_mysql import (
    _po_poryadku,
    nayti_sirot,
    _svodka,
    perenesti,
    proverit,
    sverit,
    sverit_shemu,
)

REVIZIA = "test-revision-0001"


def _baza(tmp_path, imya, revizia=REVIZIA):
    """Пустая база со схемой и отметкой ревизии."""
    engine = create_engine(f"sqlite:///{tmp_path / imya}")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": revizia})
    return engine


def _skolko(engine, table):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


@pytest.fixture()
def istochnik(tmp_path):
    engine = _baza(tmp_path, "src.db")
    nastroyki = Base.metadata.tables["site_settings"]
    with engine.begin() as c:
        c.execute(
            insert(nastroyki),
            [{"key": f"клюё-{n}", "value": f"значение 🌿 {n}"} for n in range(250)],
        )
    return engine


def test_raznye_revizii_ostanavlivayut_perenos(istochnik, tmp_path):
    """Разные ревизии — разный набор колонок.

    Перенести при таком расхождении означает либо упасть на полпути, либо
    молча не довезти то, чего в целевой схеме ещё нет. Второе хуже: беду
    заметят не сразу и не свяжут с переездом.
    """
    cel = _baza(tmp_path, "dst.db", revizia="sovsem-drugaya")
    with pytest.raises(SystemExit) as beda:
        perenesti(istochnik, cel)
    assert "Схемы разошлись" in str(beda.value)
    assert _skolko(cel, Base.metadata.tables["site_settings"]) == 0


def test_perenos_dovozit_vsyo_i_sverka_shoditsya(istochnik, tmp_path):
    cel = _baza(tmp_path, "dst.db")
    # пачка нарочно меньше числа строк: иначе пакетная вставка не проверяется
    rashozhdenia = perenesti(istochnik, cel, razmer_pachki=40)
    assert rashozhdenia == []
    nastroyki = Base.metadata.tables["site_settings"]
    assert _skolko(cel, nastroyki) == 250
    with cel.connect() as c:
        znachenie = c.execute(
            select(nastroyki.c.value).where(nastroyki.c.key == "клюё-7")
        ).scalar_one()
    assert znachenie == "значение 🌿 7"


def test_povtornyy_perenos_ne_dubliruet(istochnik, tmp_path):
    """Цель к моменту переноса НЕ пуста — её населяют сами миграции.

    Этапы воронки, роль root и три десятка её прав сеются миграциями, поэтому
    наивная вставка поверх падает на уникальности — и не на первой таблице, а
    на третьей, когда часть данных уже перенесена. Источник здесь единственная
    власть, поэтому цель перед вставкой очищается, и повторный запуск обязан
    давать тот же результат, а не удвоенный.
    """
    cel = _baza(tmp_path, "dst.db")
    perenesti(istochnik, cel, razmer_pachki=40)
    rashozhdenia = perenesti(istochnik, cel, razmer_pachki=40)
    assert rashozhdenia == []
    assert _skolko(cel, Base.metadata.tables["site_settings"]) == 250


def test_istochnik_ne_menyaetsya(istochnik, tmp_path):
    """Пока исходная база цела, неудавшийся переезд стоит одной строки в конфиге."""
    nastroyki = Base.metadata.tables["site_settings"]
    bylo = _skolko(istochnik, nastroyki)
    perenesti(istochnik, _baza(tmp_path, "dst.db"), razmer_pachki=40)
    assert _skolko(istochnik, nastroyki) == bylo


def test_poterya_stroki_ne_prohodit_molcha(istochnik, tmp_path):
    """Сверка обязана ловить недовезённое, иначе она бесполезна.

    Забираем из цели одну строку после переноса — ровно то, что оставило бы
    оборванное соединение или переполненный пакет, — и требуем, чтобы сверка
    это назвала. Проверка, срабатывающая только когда всё хорошо, не проверка.
    """
    cel = _baza(tmp_path, "dst.db")
    tablicy = list(Base.metadata.sorted_tables)
    do = {t.name: _svodka(istochnik, t) for t in tablicy}
    perenesti(istochnik, cel, razmer_pachki=40)
    assert sverit(do, cel, tablicy) == []

    nastroyki = Base.metadata.tables["site_settings"]
    with cel.begin() as c:
        c.execute(nastroyki.delete().where(nastroyki.c.key == "клюё-7"))

    rashozhdenia = sverit(do, cel, tablicy)
    assert any("site_settings.строк" in s for s in rashozhdenia), rashozhdenia


# --- сверка отдельным заходом -------------------------------------------------


def test_sverka_otdelnym_zahodom_shoditsya_posle_perenosa(istochnik, tmp_path):
    """`--verify` снимает отпечаток источника ЗАНОВО и сравнивает с целью.

    Отдельный заход нужен по существу: проверка, выполняющаяся только вместе с
    удачным переносом, проверяет само действие, а не его итог. Именно на её
    зелёном исходе установщик признаёт переезд удавшимся и только после этого
    переписывает OPENCRM_DB_URL.
    """
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik, cel) == []
    assert proverit(istochnik, cel) == []


def test_sverka_lovit_poteryannuyu_stroku(istochnik, tmp_path):
    """Молчаливая потеря строки — худшее, что может случиться при переносе.

    Проверяем ту самую беду: перенос прошёл, а потом из цели пропала строка.
    Сверка обязана это назвать — иначе приложение переключат на базу, в которой
    чего-то нет, и обнаружат это через месяц.
    """
    cel = _baza(tmp_path, "dst.db")
    perenesti(istochnik, cel)
    nastroyki = Base.metadata.tables["site_settings"]
    with cel.begin() as c:
        c.execute(nastroyki.delete().where(nastroyki.c.key == "клюё-7"))

    rashozhdenia = proverit(istochnik, cel)
    assert rashozhdenia, "потеря строки прошла сверку молча"
    assert any("site_settings" in stroka for stroka in rashozhdenia)


def test_sverka_lovit_nedostachu_v_sheme(istochnik, tmp_path):
    """Совпадение ревизий alembic — НЕ то же самое, что сошедшаяся схема.

    Ревизия говорит, какие шаги отмечены выполненными; схема — что из них
    получилось. Расходятся они ровно в том случае, ради которого всё
    затевается: миграция прошла наполовину, успела отметиться и оставила
    таблицу без колонки. Такую базу нельзя объявлять годной.
    """
    cel = _baza(tmp_path, "dst.db")
    perenesti(istochnik, cel)
    with cel.begin() as c:
        c.execute(text("ALTER TABLE site_settings DROP COLUMN updated_at"))

    assert sverit_shemu(cel), "нехватка колонки не замечена"
    assert proverit(istochnik, cel), "сверка пропустила базу с недостающей колонкой"


def test_oborvavshiysya_perenos_ne_vydayotsya_za_udachnyy(istochnik, tmp_path, monkeypatch):
    """Отказ посередине оставляет цель наполовину заполненной.

    Схема в такой базе сходится, `/healthz` зелёный, и переключённое на неё
    приложение показало бы пустую CRM, ничем не отличимую от исправной. Молчать
    об этом нельзя: код возврата обязан быть ненулевым, а вызывающий — остаться
    на SQLite.
    """
    cel = _baza(tmp_path, "dst.db")

    import scripts.migrate_to_mysql as skript

    def upast(*args, **kwargs):
        raise RuntimeError("соединение с целью потеряно")

    monkeypatch.setattr(skript, "_perenesti_tablicy", upast)
    rashozhdenia = skript.perenesti(istochnik, cel)
    assert rashozhdenia, "оборвавшийся перенос объявлен удачным"
    assert any("оборвал" in stroka for stroka in rashozhdenia)


def test_vyborka_idyot_v_ustoychivom_poryadke():
    """У `finance_operations` есть ссылка на саму себя (`corrects_id`).

    Порядок вставки внутри таблицы держался ровно на том, что SQLite отдаёт
    строки в порядке rowid, а поправка всегда старше поправляемой. Явного
    порядка не было вовсе: смена плана выборки уронила бы перенос на внешнем
    ключе — не сразу и не воспроизводимо.
    """
    tablica = Base.metadata.tables["finance_operations"]
    zapros = str(_po_poryadku(tablica))
    assert "ORDER BY" in zapros, "выборка идёт в произвольном порядке"
    assert "finance_operations.id" in zapros


# --- построчная сверка --------------------------------------------------------
#
# Агрегатная сверка (число строк + суммы по целым столбцам) не видит порчу,
# которая сама себя компенсирует. Проверено делом: одной заявке `+1`, другой
# `-1` — число строк то же, сумма столбца та же, сверка зелёная, и сайт
# переключают на базу с двумя неверными суммами.

TOVARY = Base.metadata.tables["products"]


@pytest.fixture()
def istochnik_s_tovarami(tmp_path):
    """База с деньгами в колонках: на них и ставится опыт с компенсацией."""
    engine = _baza(tmp_path, "src-tovary.db")
    with engine.begin() as c:
        c.execute(
            insert(TOVARY),
            [
                {"name": f"Товар {n}", "unit": "pcs", "price_minor": 1000 + n, "note": ""}
                for n in range(1, 21)
            ],
        )
    return engine


def test_kompensiruyushchaya_porcha_ne_prohodit_sverku(istochnik_s_tovarami, tmp_path):
    """+1 одной строке и −1 другой: итог тот же, данные врут.

    Ровно этот опыт и был поставлен руками: агрегатная сверка объявила базу
    годной, а в ней лежали две неверные суммы. Здесь она обязана назвать обе.
    """
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_tovarami, cel) == []
    assert proverit(istochnik_s_tovarami, cel) == []

    with cel.begin() as c:
        c.execute(TOVARY.update().where(TOVARY.c.id == 1).values(price_minor=TOVARY.c.price_minor + 1))
        c.execute(TOVARY.update().where(TOVARY.c.id == 2).values(price_minor=TOVARY.c.price_minor - 1))

    # Агрегат по-прежнему сходится — это и есть суть находки.
    do = {t.name: _svodka(istochnik_s_tovarami, t) for t in Base.metadata.sorted_tables}
    assert sverit(do, cel, [TOVARY]) == [], "опыт поставлен неверно: агрегат уже разошёлся"

    rashozhdenia = proverit(istochnik_s_tovarami, cel)
    assert rashozhdenia, "компенсирующая порча прошла сверку молча"
    slitno = " ".join(rashozhdenia)
    assert "products" in slitno and "price_minor" in slitno, rashozhdenia
    assert "id=1" in slitno and "id=2" in slitno, ("сверка не назвала строки поимённо", rashozhdenia)


def test_sverka_nazyvaet_nedovezyonnuyu_stroku_poimenno(istochnik_s_tovarami, tmp_path):
    """Записанное после снятия копии — это строка, которой в цели нет.

    Так пропал заведённый во время переезда клиент: перенос шёл из копии, а
    сверка сравнивала ту же копию с целью и про живой файл сказать не могла
    ничего. Теперь сверке дают живой источник, и она обязана назвать не только
    «строк стало меньше», но и КАКУЮ строку не довезли: по числу человек не
    поймёт, кого искать.
    """
    cel = _baza(tmp_path, "dst.db")
    perenesti(istochnik_s_tovarami, cel)

    # Так выглядит клиент, заведённый через экран CRM уже после снятия копии.
    with istochnik_s_tovarami.begin() as c:
        c.execute(insert(TOVARY), {"id": 777, "name": "Заведён во время переезда", "unit": "pcs", "note": ""})

    rashozhdenia = proverit(istochnik_s_tovarami, cel)
    assert rashozhdenia, "запись, сделанная во время переезда, потерялась молча"
    slitno = " ".join(rashozhdenia)
    assert "products" in slitno and "id=777" in slitno, rashozhdenia


def test_chestnyy_perenos_ne_krasneet(istochnik_s_tovarami, tmp_path):
    """Парная проверка: без неё «сверку» легко доделать до вечно красной.

    Вечно красная сверка не строже — она просто отключает переезд, и первым же
    действием её выключат.
    """
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_tovarami, cel) == []
    assert proverit(istochnik_s_tovarami, cel) == []
    assert proverit(istochnik_s_tovarami, cel) == [], "сверка не должна ничего менять"


# --- источник открывается только на чтение ------------------------------------


def test_istochnik_otkryvaetsya_tolko_na_chtenie(istochnik_s_tovarami, tmp_path, monkeypatch):
    """Свойство, на котором держится обратимость всего переезда.

    Пока SQLite-файл цел, неудавшийся переезд стоит одной строки в конфиге:
    вернуть `OPENCRM_DB_URL` на sqlite и перезапуститься. Испорченный стоит
    восстановления из копии, то есть потерянного дня.

    Держится это на одной строке в `main()` — приписке `mode=ro&uri=true` к
    адресу источника. Проверки выше зовут `perenesti`/`proverit` напрямую и
    мимо неё проходят: найдено нарочной поломкой сторожа — снятие приписки не
    роняло ни одного теста.

    Поэтому здесь зовётся именно `main()`, и проверяется не текст адреса, а
    поведение: по соединению, которое он завёл, запись обязана быть отвергнута
    самой базой.
    """
    import scripts.migrate_to_mysql as skript

    put_istochnika = tmp_path / "src-tovary.db"
    assert put_istochnika.is_file(), "фикстура положила базу в другое место"
    cel = _baza(tmp_path, "dst.db")

    zavedennye = []
    nastoyashchiy = skript.create_engine

    def zapomnit(url, *args, **kwargs):
        engine = nastoyashchiy(url, *args, **kwargs)
        zavedennye.append(engine)
        return engine

    monkeypatch.setattr(skript, "create_engine", zapomnit)
    monkeypatch.setattr(
        "sys.argv",
        [
            "migrate_to_mysql.py",
            "--source", f"sqlite:///{put_istochnika.as_posix()}",
            "--target", str(cel.url),
            "--check-only",
        ],
    )

    skript.main()

    assert zavedennye, "main() не завёл ни одного движка"
    source = zavedennye[0]
    with pytest.raises(Exception) as beda:
        with source.begin() as c:
            c.execute(text("INSERT INTO site_settings (key, value) VALUES ('порча', 'x')"))
    assert "readonly" in str(beda.value).lower(), (
        "исходная база открыта на ЗАПИСЬ: неудачный переезд перестал быть "
        f"бесплатным. Отказ был другой: {beda.value}"
    )


# --- сверку обманывали двумя способами, оба проверены делом --------------------
#
# Компенсирующая порча (выше) была первой из трёх. Две оставшиеся нашлись на
# настоящем переезде (стенд, MySQL 8, 300 299 строк) и обе давали ЗЕЛЁНУЮ
# сверку на испорченной базе:
#
#   1. ДРОБНЫЕ выпадали из построчного сличения целиком: `duration_sec` 12.5 в
#      источнике против 99999.5 в цели — сверка молчала. В коде это было
#      объявлено осознанно (`Float` в MySQL — четыре байта против восьми у
#      SQLite, побайтное сравнение краснело бы всегда), но объявленная дыра
#      остаётся дырой. Лекарство — допуск, а не исключение из сравнения.
#
#   2. ЗНАЧЕНИЕ ВНЕ ОБЛАСТИ ИСТИННОСТИ не было объявлено НИГДЕ: `is_service` = 1
#      в источнике против 2 в цели. Обе стороны SQLAlchemy читает как `True`,
#      сумм по логическим столбцам не берут вовсе — разницы не видел никто.
#
# Каждая проверка идёт ПАРОЙ со своей противоположностью: сверка, которая
# краснеет всегда, не строже — её выключат первым же действием.

RABOTY = Base.metadata.tables["works"]
DOSKI = Base.metadata.tables["boards"]


@pytest.fixture()
def istochnik_s_drobnymi(tmp_path):
    """Доска с работами: дробные живут только здесь (длительность и кадрирование)."""
    engine = _baza(tmp_path, "src-raboty.db")
    with engine.begin() as c:
        c.execute(insert(DOSKI), {"id": 1, "title": "Доска", "description": ""})
        c.execute(
            insert(RABOTY),
            [
                {
                    "id": n,
                    "board_id": 1,
                    "work_uid": f"uid-{n}",
                    "kind": "video",
                    "title": f"Работа {n}",
                    "description": "",
                    "project_url": "",
                    "sort_order": n * 10,
                    "status": "ready",
                    "original_name": f"{n}.mp4",
                    "mime": "video/mp4",
                    "size_bytes": 1000 + n,
                    # 1/3 нарочно: именно на таком числе четырёхбайтный FLOAT в
                    # MySQL и расходится с восьмибайтным в SQLite.
                    "duration_sec": 12.5 + n / 3,
                    "preview_focus": 0.25,
                }
                for n in range(1, 6)
            ],
        )
    return engine


def test_sverka_lovit_porchu_v_drobnom_stolbtse(istochnik_s_drobnymi, tmp_path):
    """12.5 против 99999.5 — сверка обязана назвать столбец и строку."""
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_drobnymi, cel) == []
    assert proverit(istochnik_s_drobnymi, cel) == []

    with cel.begin() as c:
        c.execute(RABOTY.update().where(RABOTY.c.id == 2).values(duration_sec=99999.5))
        c.execute(RABOTY.update().where(RABOTY.c.id == 3).values(preview_focus=0.99))

    rashozhdenia = proverit(istochnik_s_drobnymi, cel)
    assert rashozhdenia, "дробные столбцы снова выпали из сличения"
    slitno = " ".join(rashozhdenia)
    assert "duration_sec" in slitno and "id=2" in slitno, rashozhdenia
    assert "preview_focus" in slitno and "id=3" in slitno, rashozhdenia


def test_okruglenie_chetyryohbaytnogo_float_ne_krasneet(istochnik_s_drobnymi, tmp_path):
    """Парная проверка: то самое, из-за чего дробные когда-то и выбросили.

    MySQL возвращает 0,3333333333 как 0,33333334 на ЧЕСТНОМ переносе тоже.
    Сверка, краснеющая на каждом переезде, не строже — она просто выключена.
    """
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_drobnymi, cel) == []

    # Так выглядит то же самое число, доехавшее через четыре байта.
    import struct

    with cel.begin() as c:
        for stroka in c.execute(select(RABOTY.c.id, RABOTY.c.duration_sec)).all():
            urezannoe = struct.unpack("f", struct.pack("f", stroka.duration_sec))[0]
            c.execute(
                RABOTY.update().where(RABOTY.c.id == stroka.id).values(duration_sec=urezannoe)
            )

    assert proverit(istochnik_s_drobnymi, cel) == [], (
        "сверка краснеет на обычной потере точности FLOAT — её выключат первым же действием"
    )


def test_sverka_lovit_znachenie_vne_oblasti_istinnosti(istochnik_s_tovarami, tmp_path):
    """1 против 2: обе стороны читаются как «истина», а данные разные.

    Найдено делом и не было объявлено нигде. Сумм по логическим столбцам не
    берут, побайтного сравнения после разбора в `bool` не остаётся — значение
    вне области истинности проезжало переезд насквозь.
    """
    # Обе стороны обязаны читаться как «истина», иначе опыт не про то: 0 против
    # 2 поймала бы и прежняя сверка, потому что это False против True.
    with istochnik_s_tovarami.begin() as c:
        c.execute(text("UPDATE products SET is_service = 1 WHERE id = 3"))
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_tovarami, cel) == []
    assert proverit(istochnik_s_tovarami, cel) == []

    # Мимо ORM и мимо типа: ровно так это и выглядит в живой базе.
    with cel.begin() as c:
        c.execute(text("UPDATE products SET is_service = 2 WHERE id = 3"))
    with cel.connect() as c:
        assert c.execute(text("SELECT is_service FROM products WHERE id = 3")).scalar() == 2
    with istochnik_s_tovarami.connect() as c:
        assert c.execute(text("SELECT is_service FROM products WHERE id = 3")).scalar() == 1

    rashozhdenia = proverit(istochnik_s_tovarami, cel)
    assert rashozhdenia, "значение вне области истинности прошло сверку молча"
    slitno = " ".join(rashozhdenia)
    assert "is_service" in slitno and "id=3" in slitno, rashozhdenia


def test_odinakovye_logicheskie_ne_krasneyut(istochnik_s_tovarami, tmp_path):
    """Парная проверка к предыдущей: 0 и 1 обязаны сходиться молча."""
    cel = _baza(tmp_path, "dst.db")
    assert perenesti(istochnik_s_tovarami, cel) == []
    with cel.begin() as c:
        c.execute(text("UPDATE products SET is_service = 1 WHERE id = 4"))
    with istochnik_s_tovarami.begin() as c:
        c.execute(text("UPDATE products SET is_service = 1 WHERE id = 4"))
    assert proverit(istochnik_s_tovarami, cel) == []


# --- строки, ссылающиеся в пустоту ---------------------------------------------
#
# Найдено живым прогоном переезда на настоящем докере. Перенос доходил до
# `deals`, получал от MySQL `1452 Cannot add or update a child row` и падал
# ПОСЕРЕДИНЕ: сайт к этому времени уже закрыт на обслуживание, цель наполовину
# заполнена, а в логе — имя ограничения вместо имени беды.
#
# Возможно это потому, что SQLite следит за внешними ключами только по просьбе.
# Приложение просит (`PRAGMA foreign_keys=ON` в `database/session.py`), а
# миграции НЕ просят и не могут: alembic в batch-режиме пересоздаёт таблицы, и
# включённые ключи ломают этот приём. Окно узкое, но открытое.
#
# Правило: спрашиваем это ПЕРВЫМ и у одного источника — ни цели, ни поднятой
# MySQL, ни закрытого сайта для вопроса не нужно.

ZAYAVKI = Base.metadata.tables["deals"]
KLIENTY = Base.metadata.tables["clients"]


def test_sirota_ostanavlivaet_perenos_do_pervoy_zapisi(tmp_path):
    """Заявка на несуществующего клиента: не начинаем вовсе и называем строку."""
    istochnik = _baza(tmp_path, "src-sirota.db")
    with istochnik.begin() as c:
        # foreign_keys у SQLite по умолчанию выключены — ровно так сирота и
        # заводится в жизни, поэтому опыт ставится без всяких ухищрений.
        c.execute(insert(KLIENTY), {"name": "Настоящий клиент"})
        c.execute(insert(ZAYAVKI), {"title": "На живого", "client_id": 1, "stage": "new"})
        c.execute(insert(ZAYAVKI), {"title": "В пустоту", "client_id": 999, "stage": "new"})
    cel = _baza(tmp_path, "dst.db")

    with pytest.raises(SystemExit) as beda:
        perenesti(istochnik, cel)

    skazano = str(beda.value)
    assert "deals.client_id" in skazano, skazano
    assert "999" in skazano, "не названа строка, из-за которой всё встало"
    # И главное: в цели не появилось НИ ОДНОЙ строки — переносить не начинали.
    with cel.connect() as c:
        assert c.execute(select(func.count()).select_from(KLIENTY)).scalar_one() == 0


def test_celye_kluchi_ne_meshayut_perenosu(tmp_path):
    """Парная проверка: на целой базе осмотр обязан молчать.

    Без неё проверку легко «починить» до того, что она откажет всегда, — а
    отказавший всегда переезд ничем не лучше не проверяющего.
    """
    istochnik = _baza(tmp_path, "src-celaya.db")
    with istochnik.begin() as c:
        c.execute(insert(KLIENTY), {"name": "Клиент"})
        c.execute(insert(ZAYAVKI), {"title": "На живого", "client_id": 1, "stage": "new"})
        # NULL — законная связь: ограничение молчит на нём в обеих базах.
        # Берём необязательную ссылку (у заявки клиент обязателен).
        c.execute(insert(KLIENTY), {"name": "Клиент без начальника", "manager_id": None})
    cel = _baza(tmp_path, "dst.db")

    assert nayti_sirot(istochnik) == []
    assert perenesti(istochnik, cel) == []
    assert proverit(istochnik, cel) == []


def test_osmotr_nazyvaet_vse_svyazi_a_ne_pervuyu(tmp_path):
    """Разбирать такое приходится целиком, а не по одной находке за прогон."""
    istochnik = _baza(tmp_path, "src-mnogo.db")
    with istochnik.begin() as c:
        c.execute(insert(KLIENTY), {"name": "Клиент"})
        c.execute(insert(ZAYAVKI), [{"title": "В пустоту", "client_id": 777, "stage": "new"},
                                    {"title": "И эта", "client_id": 778, "stage": "new"}])
        c.execute(insert(KLIENTY), {"name": "Клиент без начальника", "manager_id": 404})

    naydeno = nayti_sirot(istochnik)

    slitno = " ".join(naydeno)
    assert "deals.client_id" in slitno, naydeno
    assert "clients.manager_id" in slitno, naydeno
    assert "2 строк" in slitno, "не сказано, сколько именно строк в первой связи"
