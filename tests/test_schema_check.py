"""Сверка живой базы с моделями и то, как она ведёт себя при обновлении.

Проверка существует ради одного обещания: **обновление на сервере не оставит
базу в состоянии, о котором никто не знает**. Либо схема сошлась, либо
приложение не поднялось и обновление откатилось — третьего быть не должно.
"""

import sqlalchemy as sa
from sqlalchemy import create_engine, text

from database import schema_check
from database.session import Base, engine
from tests.conftest import API


def test_zhivaya_baza_shoditsya_s_modelyami():
    """Основная проверка: на рабочей базе расхождений нет.

    Она же сторож для будущих правок: добавили модели колонку без миграции —
    падает здесь, а не на сервере через неделю.
    """
    report = schema_check.check(engine)
    assert report.ok, report.summary()
    assert report.missing_tables == ()
    assert report.missing_columns == ()
    assert report.revision, "база не отмечена ни одной миграцией"


def test_propavshaya_kolonka_zamechena(tmp_path):
    """Именно этот случай `create_all` не ловил никогда.

    Новую таблицу он создаёт, а колонку в существующую — нет. Расхождение
    молчит до первого запроса к полю, и падает там, где выглядит поломкой
    раздела, а не схемы.
    """
    url = f"sqlite:///{tmp_path / 'gap.db'}"
    other = create_engine(url)
    Base.metadata.create_all(other)
    with other.begin() as connection:
        connection.execute(text("ALTER TABLE products DROP COLUMN note"))

    report = schema_check.check(other)
    assert not report.ok
    assert "products.note" in report.missing_columns
    assert "products.note" in report.summary()


def test_propavshaya_tablitsa_zamechena(tmp_path):
    url = f"sqlite:///{tmp_path / 'notable.db'}"
    other = create_engine(url)
    Base.metadata.create_all(other)
    with other.begin() as connection:
        connection.execute(text("DROP TABLE warehouses"))

    report = schema_check.check(other)
    assert not report.ok
    assert "warehouses" in report.missing_tables


def test_lishnee_ne_schitaetsya_polomkoy(tmp_path):
    """Лишняя таблица — обычно след отката, и запретить её значит запретить откат.

    Откат — главный способ починки: обновление, у которого нельзя вернуться
    назад, страшнее любой лишней таблицы.
    """
    url = f"sqlite:///{tmp_path / 'extra.db'}"
    other = create_engine(url)
    Base.metadata.create_all(other)
    with other.begin() as connection:
        connection.execute(text("CREATE TABLE leftovers (id INTEGER PRIMARY KEY)"))

    report = schema_check.check(other)
    assert report.ok, "лишняя таблица объявлена поломкой"
    assert "leftovers" in report.extra_tables
    # `alembic_version` в лишние не попадает: иначе список был бы непустым
    # всегда, и его перестали бы читать.
    assert "alembic_version" not in report.extra_tables


def test_pustaya_baza_otlichaetsya_ot_naselyonnoy(tmp_path):
    """От этого различия зависит, чем поднимать схему.

    Пустую можно построить `create_all` и отметить как «на последней миграции».
    Населённую — только провести миграциями: `create_all` не добавляет колонок и
    потому оставил бы расхождение вместо того, чтобы его закрыть.
    """
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    other = create_engine(url)
    assert schema_check.is_empty(other)

    # База, где миграции сорвались на первой же, — тоже пустая: строить её надо
    # с нуля, а не «доводить».
    with other.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    assert schema_check.is_empty(other)

    Base.metadata.create_all(other)
    assert not schema_check.is_empty(other)


def test_healthz_govorit_pro_skhemu(base_client):
    """Обновление опирается на этот ответ: не дождался 200 — откатился.

    Подробностей здесь нет намеренно: адрес открыт наружу, а «чего именно не
    хватает в базе» — не то, что стоит рассказывать всем.
    """
    answer = base_client.get("/healthz")
    assert answer.status_code == 200
    assert answer.json() == {"status": "ok", "schema": "ok"}


def test_podrobnyy_otchyot_zakryt_pravom(root_client, manager_client):
    detailed = root_client.get(f"{API}/system/schema")
    assert detailed.status_code == 200, detailed.text
    body = detailed.json()
    assert body["ok"] is True
    assert body["revision"]
    assert body["missing_columns"] == []

    assert manager_client.get(f"{API}/system/schema").status_code == 403


def test_migratsii_dovodyat_pustuyu_bazu_do_modeley(tmp_path):
    """Путь сервера целиком: пустой файл → `alembic upgrade head` → сходится.

    Это и есть обещание автоматического обновления. Ломается оно ровно тогда,
    когда миграцию забыли дописать вслед за моделью, — и ломается здесь, в
    наборе, а не на боевом сервере.
    """
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{(tmp_path / 'byhand.db').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    other = create_engine(url)
    report = schema_check.check(other)
    assert report.ok, report.summary()
    # Миграции обязаны довести базу до головы, а не до середины.
    assert report.revision == _head_revision()


def _head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


def test_sklad_est_srazu_posle_migratsiy(tmp_path):
    """Миграция обязана оставить хотя бы один склад.

    Без места система не примет ни одного прихода, а взяться ему после
    обновления неоткуда: посев при старте страхует, но полагаться на страховку
    там, где есть основной путь, — значит однажды обнаружить, что страховки нет.
    """
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{(tmp_path / 'places.db').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    other = create_engine(url)
    with other.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT name, is_default FROM warehouses")
        ).all()
    assert rows, "после миграций не осталось ни одного склада"
    assert sum(1 for _, is_default in rows if is_default) == 1
