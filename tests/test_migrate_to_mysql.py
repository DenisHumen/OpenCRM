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
