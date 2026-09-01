"""Сверка живой базы с моделями и то, как она ведёт себя при обновлении.

Проверка существует ради одного обещания: **обновление на сервере не оставит
базу в состоянии, о котором никто не знает**. Либо схема сошлась, либо
приложение не поднялось и обновление откатилось — третьего быть не должно.
"""

import pytest
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


# --- общий замок на схему -----------------------------------------------------


def test_rabota_so_shemoy_idyot_pod_obshchim_zamkom():
    """`_ensure_schema` обязан брать общий замок, а не догадываться по ошибке.

    **НАЙДЕНО ЗАМЕРОМ.** Четыре процесса, поднявшись разом на пустой базе, без
    замка дают такую картину (воспроизведено на настоящей MySQL):

        процесс 1: УПАЛ за 0.27 с — (1050, "Table 'roles' already exists")
        процесс 2: УПАЛ за 0.27 с — (1050, "Table 'roles' already exists")
        процесс 0: поднял схему за 8.95 с

    И это не «один воркер не поднялся»: uvicorn считает отказ старта
    непоправимым и гасит ВЕСЬ контейнер — `/healthz` замолкает, обновление
    откатывается. С замком поднимаются все четыре.

    Обратите внимание на имя таблицы: падение приходит на `roles`, а прежняя
    терпимость к соседу проверяла `has_table(users)`. `create_all` идёт
    топологическим порядком и валится на той таблице, до которой дошёл, — то
    есть догадка не работала никогда.
    """
    import ast
    import inspect

    from web import main as web_main

    telo = ast.parse(inspect.getsource(web_main._ensure_schema))
    imena = {u.attr for u in ast.walk(telo) if isinstance(u, ast.Attribute)}
    assert "zamok_shemy" in imena, (
        "работа со схемой идёт без общего замка — три процесса из четырёх умрут "
        "на старте и утащат за собой контейнер"
    )

    # И догадки по исключению больше нет: она молчаливо не работала.
    #
    # Смотрим ДЕРЕВО, а не текст: слово `has_table` стоит в объяснении выше, и
    # поиск по строке краснел бы на исправном коде. Ровно та же ловушка, что
    # была со сторожем чтения копии базы.
    pod_zamkom = ast.parse(inspect.getsource(web_main._shema_pod_zamkom))
    for derevo in (telo, pod_zamkom):
        obrashcheniya = {u.attr for u in ast.walk(derevo) if isinstance(u, ast.Attribute)}
        assert "has_table" not in obrashcheniya, (
            "вернулась догадка по исключению вместо ожидания"
        )


def test_zamok_shemy_pravda_ne_puskaet_vtorogo():
    """Замок обязан РАЗВОДИТЬ, а не просто существовать.

    Проверка на деле, а не по имени: два соединения к той же базе, второе не
    должно получить замок, пока держит первое. Иначе «замок» окажется
    украшением, и узнать об этом можно будет только на боевом сервере.
    """
    import threading

    from sqlalchemy import func, select

    from database.schema_check import IMYA_ZAMKA_SHEMY, zamok_shemy
    from database.session import engine, prefiks_zamka

    if engine.dialect.name != "mysql":
        pytest.skip("именованные замки есть только у MySQL")

    vtoroy_vzyal = []
    derzhim = threading.Event()
    otpuskaem = threading.Event()

    def pervyy():
        with zamok_shemy(engine):
            derzhim.set()
            otpuskaem.wait(timeout=10)

    potok = threading.Thread(target=pervyy)
    potok.start()
    try:
        assert derzhim.wait(timeout=10), "первый не взял замок вовсе"
        # Ноль секунд ожидания: нам нужен ответ «занято», а не очередь.
        with engine.connect() as vtoroye:
            vtoroy_vzyal.append(
                vtoroye.execute(
                    select(func.get_lock(f"{IMYA_ZAMKA_SHEMY}_{prefiks_zamka(engine)}", 0))
                ).scalar()
            )
    finally:
        otpuskaem.set()
        potok.join(timeout=10)

    assert vtoroy_vzyal == [0], (
        f"второй получил замок, пока его держит первый: {vtoroy_vzyal} — "
        "замок не разводит, и гонка на старте остаётся"
    )


def test_zamok_shemy_ne_zapiraet_sosednyuyu_bazu(chistaya_baza, monkeypatch):
    """Соседняя установка на том же сервере обязана работать своим ходом.

    `GET_LOCK` в MySQL — замок уровня СЕРВЕРА, а стережёт он схему ОДНОЙ базы.
    Пока имя было постоянным, две установки на общем сервере ждали друг друга:
    соседская миграция держит замок до пяти минут, наш процесс не дожидается и
    отказывается стартовать — `/healthz` молчит, обновление откатывается, а
    чинить идут не то, что сломалось.

    Проверка зовёт НАСТОЯЩИЙ `zamok_shemy` для обеих баз, а не берёт замок по
    имени руками: имя руками совпало бы с новым и на откаченном коде, то есть
    проверка была бы зелёной ровно там, где нужна.

    Ожидание сбито в ноль: с общим именем второй ждал бы пять минут и только
    потом отказал — проверка выглядела бы зависшей, а не красной.
    """
    import threading

    from sqlalchemy import create_engine

    from database import schema_check as sc
    from database.session import engine

    if engine.dialect.name != "mysql":
        pytest.skip("именованные замки есть только у MySQL")

    monkeypatch.setattr(sc, "ZHDAT_ZAMOK_SEKUND", 0)
    sosed = create_engine(chistaya_baza)
    itog = []
    derzhim = threading.Event()
    otpuskaem = threading.Event()

    def svoy():
        with sc.zamok_shemy(engine):
            derzhim.set()
            otpuskaem.wait(timeout=15)

    potok = threading.Thread(target=svoy)
    potok.start()
    try:
        assert derzhim.wait(timeout=15), "свой замок не взялся вовсе"
        try:
            with sc.zamok_shemy(sosed):
                itog.append("взял")
        except RuntimeError as otkaz:
            itog.append(f"отказ: {otkaz}")
    finally:
        otpuskaem.set()
        potok.join(timeout=15)
        sosed.dispose()

    assert itog == ["взял"], (
        f"соседняя база не смогла взять свой замок: {itog} — "
        "имя замка общее на сервер, и установки запирают друг друга"
    )
