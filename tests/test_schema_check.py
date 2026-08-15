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


def snesti_tablitsu(dvigatel, imya: str) -> None:
    """Снести таблицу вместе со ссылками на неё — нарочно, ради проверки.

    Проверкам ниже нужна база, в которой таблицы НЕ ХВАТАЕТ. Взять такую можно
    только снеся её, а `warehouses` держат ссылки из движений и переездов:
    голый `DROP TABLE` MySQL отвергает («cannot drop table referenced by a
    foreign key constraint»), и падала на этом сама проверка, а не то, что она
    проверяет.

    Снимаем проверку ключей ровно на время сноса. Это не поблажка тесту: база
    без таблицы и с висящими на неё ссылками — как раз то состояние, в котором
    оказывается сервер после оборванной миграции или ручной правки, и описывают
    эти проверки именно его. Возвращаем признак на месте: соединение уходит
    обратно в пул, и оставить на нём выключенные ключи значит испортить
    соседей.
    """
    with dvigatel.begin() as soedinenie:
        soedinenie.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            soedinenie.execute(text(f"DROP TABLE {imya}"))
        finally:
            soedinenie.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


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


def test_propavshaya_kolonka_zamechena(chistaya_baza):
    """Именно этот случай `create_all` не ловил никогда.

    Новую таблицу он создаёт, а колонку в существующую — нет. Расхождение
    молчит до первого запроса к полю, и падает там, где выглядит поломкой
    раздела, а не схемы.
    """
    url = chistaya_baza
    other = create_engine(url)
    Base.metadata.create_all(other)
    with other.begin() as connection:
        connection.execute(text("ALTER TABLE products DROP COLUMN note"))

    report = schema_check.check(other)
    assert not report.ok
    assert "products.note" in report.missing_columns
    assert "products.note" in report.summary()


def test_propavshaya_tablitsa_zamechena(chistaya_baza):
    url = chistaya_baza
    other = create_engine(url)
    Base.metadata.create_all(other)
    snesti_tablitsu(other, "warehouses")

    report = schema_check.check(other)
    assert not report.ok
    assert "warehouses" in report.missing_tables


def test_lishnee_ne_schitaetsya_polomkoy(chistaya_baza):
    """Лишняя таблица — обычно след отката, и запретить её значит запретить откат.

    Откат — главный способ починки: обновление, у которого нельзя вернуться
    назад, страшнее любой лишней таблицы.
    """
    url = chistaya_baza
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


def test_pustaya_baza_otlichaetsya_ot_naselyonnoy(chistaya_baza):
    """От этого различия зависит, чем поднимать схему.

    Пустую можно построить `create_all` и отметить как «на последней миграции».
    Населённую — только провести миграциями: `create_all` не добавляет колонок и
    потому оставил бы расхождение вместо того, чтобы его закрыть.
    """
    url = chistaya_baza
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
    assert answer.json()["status"] == "ok"
    assert answer.json()["schema"] == "ok"


def test_podrobnyy_otchyot_zakryt_pravom(root_client, manager_client):
    detailed = root_client.get(f"{API}/system/schema")
    assert detailed.status_code == 200, detailed.text
    body = detailed.json()
    assert body["ok"] is True
    assert body["revision"]
    assert body["missing_columns"] == []

    assert manager_client.get(f"{API}/system/schema").status_code == 403


def test_migratsii_dovodyat_pustuyu_bazu_do_modeley(chistaya_baza):
    """Путь сервера целиком: пустой файл → `alembic upgrade head` → сходится.

    Это и есть обещание автоматического обновления. Ломается оно ровно тогда,
    когда миграцию забыли дописать вслед за моделью, — и ломается здесь, в
    наборе, а не на боевом сервере.
    """
    from alembic import command
    from alembic.config import Config

    url = chistaya_baza
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


def test_sklad_est_srazu_posle_migratsiy(chistaya_baza):
    """Миграция обязана оставить хотя бы один склад.

    Без места система не примет ни одного прихода, а взяться ему после
    обновления неоткуда: посев при старте страхует, но полагаться на страховку
    там, где есть основной путь, — значит однажды обнаружить, что страховки нет.
    """
    from alembic import command
    from alembic.config import Config

    url = chistaya_baza
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


def test_baza_sobrannaya_mimo_migratsiy_otmechaetsya(chistaya_baza, monkeypatch):
    """Так выглядит база, построенная прежним безусловным `create_all`.

    Гнать по ней миграции с нуля нельзя — первая же упрётся в «table
    site_settings already exists», и приложение не поднимется вовсе. Именно это
    и случилось дважды: на базе разработки и на одноразовом стенде.

    Раз схема сходится, база фактически на голове — просто об этом никто не
    записал. Отмечаем и идём дальше.
    """
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    import web.main as main

    url = chistaya_baza
    other = create_engine(url)
    Base.metadata.create_all(other)
    assert schema_check.current_revision(other) is None
    assert not schema_check.is_empty(other)

    monkeypatch.setattr(main, "engine", other)
    main._ensure_schema()

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert schema_check.current_revision(other) == head, "база осталась неотмеченной"


def test_baza_mimo_migratsiy_i_ne_shoditsya_ostanavlivaet(chistaya_baza, monkeypatch):
    """Чинить вслепую нечего: неизвестно, каких шагов не хватает.

    Молчаливая догадка здесь опаснее честной остановки — она отметила бы как
    «последнюю» базу, в которой чего-то нет, и расхождение стало бы вечным.
    """
    import pytest

    import web.main as main

    url = chistaya_baza
    other = create_engine(url)
    Base.metadata.create_all(other)
    snesti_tablitsu(other, "warehouses")

    monkeypatch.setattr(main, "engine", other)
    with pytest.raises(RuntimeError) as failure:
        main._ensure_schema()
    assert "warehouses" in str(failure.value)


def test_healthz_ne_vryot_pri_lezhachey_baze(monkeypatch):
    """Лежащая база обязана СБИТЬ ответ 200 — на этом держится откат обновления.

    Обновление ждёт `/healthz` и, не дождавшись, возвращает прошлую версию
    вместе с базой. Ответь он 200 при неотвечающей базе — обновление посчитало
    бы деплой удачным и оставило сломанный сайт: контейнер поднят, страница
    отдаёт пятисотки, а откатывать уже никто не станет.

    Свойство это ни одна проверка не стерегла: у `/healthz` были сторожа на
    схему, режим обслуживания и Redis, а на саму базу — нет. При этом именно
    база и есть то единственное, ради чего он ходит в `SELECT 1`.

    Про Redis решение обратное и тоже намеренное (`tests/test_ratelimit_shared`):
    его `/healthz` не опрашивает вовсе, потому что заменой контейнера
    приложения лежащий Redis не чинится, а цикл перезапусков поверх аварии
    соседа — это своя авария. С базой не так: без неё приложение не работает
    вовсе, и говорить обратное было бы враньём.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    import web.main as glavnoe

    class BazaNeOtvechaet:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("база не отвечает"))

        def close(self):
            pass

    monkeypatch.setattr(glavnoe, "SessionLocal", BazaNeOtvechaet)
    # `raise_server_exceptions=False` — чтобы увидеть КОД ОТВЕТА, а не поймать
    # исключение: снаружи докер и обновлятор видят именно код.
    otvet = TestClient(glavnoe.app, raise_server_exceptions=False).get("/healthz")
    assert otvet.status_code != 200, (
        "при лежачей базе /healthz ответил 200 — обновление сочтёт такой деплой "
        "удачным и не откатится"
    )
