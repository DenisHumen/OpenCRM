"""Засев `phone_norm` при накате телефонии — на населённой базе.

Миграция `a9e64c17f235` заводит колонку `clients.phone_norm` и заполняет её
нормализованными номерами существующих клиентов. Делалось это построчно: один
`SELECT` и по одному `UPDATE` на карточку.

**Чем это плохо, если считать не строками, а временем.** Точка входа
(`docker/entrypoint.sh`) гонит `alembic upgrade head` ДО старта приложения —
значит всё время миграции `/healthz` не отвечает вовсе. Автообновление
(`deploy/updater.py`) ждёт его тридцать попыток по четыре секунды, около двух
минут, и не дождавшись объявляет обновление сломанным и откатывает и код, и
базу. На боевом объёме, названном в шапке `tests/test_speed.py` (200 000
клиентов), двести тысяч отдельных обращений в это окно не укладываются, и
сервер запирается на старой версии. Соседняя миграция `d3b8c05f1e2a` цену
такого обхода называет прямо: «минуты вместо секунд».

Одним запросом нельзя: `normalize_phone` — это питон (плюс, ведущие нули, код
страны, добавочный), и переписать его выражением SQL значило бы завести ВТОРУЮ
нормализацию. Разошлись бы они молча, и заявка перестала бы находить своего
клиента. Поэтому пачками: значения считает питон, уезжают они одним
`UPDATE ... CASE` на тысячу строк.

Проверка гоняет НАСТОЯЩУЮ миграцию на настоящей MySQL и заведомо больше одной
пачки — иначе граница пачки, единственное новое место, осталась бы непройденной.
"""

import pathlib

from sqlalchemy import create_engine, text

from core.utils import normalize_phone

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Ревизия телефонии и та, что перед ней.
TELEFONIYA = "a9e64c17f235"
DO_NEYO = "d6b30f84c917"

#: Больше одной пачки: размер пачки в миграции — тысяча.
SKOLKO_KLIENTOV = 1050


def _obyazatelnye(soedinenie, tablitsa: str) -> dict:
    """Чем заполнить колонки таблицы, у которых нет умолчания.

    Спрашивается у базы, а не выписывается списком. Выписанный список — это
    копия схемы в тесте: он устареет на первой же миграции с новой обязательной
    колонкой, и проверка покраснеет отказом ВСТАВКИ, ничего не сказав про то,
    ради чего написана. Здесь это уже случилось дважды подряд.
    """
    kolonki = soedinenie.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tablitsa "
            "AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
            "AND EXTRA NOT LIKE '%auto_increment%'"
        ),
        {"tablitsa": tablitsa},
    ).all()
    return {
        imya: (0 if tip in ("int", "bigint", "tinyint", "decimal") else "")
        for imya, tip in kolonki
    }


def _vstavit(soedinenie, tablitsa: str, **svoyo) -> int:
    """Вставить одну строку, дополнив её обязательными колонками."""
    znacheniya = {**_obyazatelnye(soedinenie, tablitsa), **svoyo}
    stolbtsy = ", ".join(f"`{imya}`" for imya in znacheniya)
    mesta = ", ".join(f":{imya}" for imya in znacheniya)
    soedinenie.execute(
        text(f"INSERT INTO {tablitsa} ({stolbtsy}) VALUES ({mesta})"), znacheniya
    )
    return int(soedinenie.execute(text("SELECT LAST_INSERT_ID()")).scalar())


def _zavesti_klientov(soedinenie, nomera: list[str]) -> None:
    """Вставить карточки, заполнив ВСЕ обязательные колонки той ревизии.

    Состав колонок спрашивается у базы, а не выписывается сюда списком.
    Выписанный список — это копия схемы в тесте: он устареет на первой же
    миграции, добавившей обязательную колонку, и проверка покраснеет отказом
    вставки, ничего не сказав про засев номеров. Ровно это здесь уже случилось
    дважды — на `company`, потом на `notes`, которой на той ревизии ещё нет.
    """
    kolonki = soedinenie.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clients' "
            "AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
            "AND EXTRA NOT LIKE '%auto_increment%'"
        )
    ).all()

    imena, znacheniya = [], {}
    for imya, tip in kolonki:
        if imya in ("phone", "name"):
            continue
        imena.append(imya)
        znacheniya[imya] = 0 if tip in ("int", "bigint", "tinyint", "decimal") else ""

    stolbtsy = ", ".join(["name", "phone"] + imena)
    mesta = ", ".join([":name", ":phone"] + [f":{imya}" for imya in imena])
    soedinenie.execute(
        text(f"INSERT INTO clients ({stolbtsy}) VALUES ({mesta})"),
        [
            {"name": f"Клиент {nomer}", "phone": telefon, **znacheniya}
            for nomer, telefon in enumerate(nomera)
        ],
    )


def _nakatit(url: str, kuda: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, kuda)


def test_zasev_nomerov_perezhivaet_granitsu_pachki(chistaya_baza):
    """Каждому клиенту достался ЕГО номер, а не соседский.

    Опасность у пачечной записи ровно одна и своя: `UPDATE ... CASE id WHEN …`
    сопоставляет значение с ключом, и перепутанные местами или потерянные на
    границе пары дадут карточки с чужими номерами. Заметить такое потом нельзя
    ничем: номер выглядит настоящим, просто принадлежит другому человеку, и
    звонок садится не в ту карточку.

    Поэтому клиентов заведомо больше одной пачки, номера у всех разные, а
    сверяется каждый — с тем, что даёт `normalize_phone`, то есть с
    единственной нормализацией в системе.
    """
    _nakatit(chistaya_baza, DO_NEYO)

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            soedinenie.execute(
                text(
                    "INSERT INTO site_settings (`key`, `value`) "
                    "VALUES ('default_country_code', '380')"
                )
            )
            # Номера разные и разной формы: с плюсом, с ведущим нулём, с
            # международным префиксом — чтобы сверялась именно нормализация, а
            # не переписывание строки как есть.
            formy = ("+38067{:07d}", "067{:07d}", "0038067{:07d}", "38067{:07d}")
            _zavesti_klientov(
                soedinenie,
                [formy[n % len(formy)].format(n) for n in range(SKOLKO_KLIENTOV)],
            )

        _nakatit(chistaya_baza, TELEFONIYA)

        with dvigatel.connect() as soedinenie:
            stroki = soedinenie.execute(
                text("SELECT phone, phone_norm FROM clients ORDER BY id")
            ).all()

        assert len(stroki) == SKOLKO_KLIENTOV, "клиенты не доехали до миграции"
        assert len(stroki) > 1000, "пачка одна — граница пачки не проверена"

        razoshlos = [
            f"{phone!r} → {norm!r}, а должно {normalize_phone(phone, '380')[:32]!r}"
            for phone, norm in stroki
            if norm != normalize_phone(phone, "380")[:32]
        ]
        assert razoshlos == [], (
            f"номера засеяны неверно ({len(razoshlos)} из {len(stroki)}):\n  "
            + "\n  ".join(razoshlos[:5])
        )
    finally:
        dvigatel.dispose()


def test_zasev_ne_delaet_zaprosa_na_kazhduyu_kartochku(chistaya_baza):
    """Число обновлений растёт от числа ПАЧЕК, а не от числа карточек.

    Это и есть вся правка. Проверять её счётом, а не временем, — то же правило,
    что в `tests/test_speed.py`: «уложились в N миллисекунд» мигает на чужой
    машине и в контейнере под нагрузкой.

    **Считает сама MySQL, а не слушатель SQLAlchemy, и это не прихоть.** Первая
    редакция вешала `before_cursor_execute` на свой движок и была ЛОЖНО-ЗЕЛЁНОЙ:
    alembic поднимает собственное соединение, слушатель не видел ни одного
    запроса, счётчик оставался нулём, и «ноль обновлений» проходило проверку
    «не больше десяти». Покраснения не случилось даже на заведомо построчной
    редакции — то есть проверка не проверяла ничего.

    `Com_update` из GLOBAL STATUS считает на сервере и потому видит любое
    соединение. Отсюда же и нижняя граница в утверждении: счётчик, показавший
    ноль, означает «смотрю не туда», а не «обновлений не было».
    """
    _nakatit(chistaya_baza, DO_NEYO)

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            soedinenie.execute(
                text(
                    "INSERT INTO site_settings (`key`, `value`) "
                    "VALUES ('default_country_code', '380')"
                )
            )
            _zavesti_klientov(
                soedinenie, [f"+38067{n:07d}" for n in range(SKOLKO_KLIENTOV)]
            )

        def com_update() -> int:
            with dvigatel.connect() as s:
                stroka = s.execute(
                    text("SHOW GLOBAL STATUS LIKE 'Com_update'")
                ).first()
                return int(stroka[1])

        do = com_update()
        _nakatit(chistaya_baza, TELEFONIYA)
        stalo = com_update() - do

        assert stalo >= 1, (
            "счётчик обновлений показал ноль — значит проверка смотрит не туда, "
            "а не что обновлений не было"
        )
        # Тысяча в пачке, 1050 карточек — значит две пачки, плюс возможные
        # обновления самой alembic. Запас стократный: запрос на карточку дал бы
        # больше тысячи.
        assert stalo <= 20, (
            f"обновлений ушло {stalo} при {SKOLKO_KLIENTOV} карточках — "
            "засев по-прежнему идёт построчно"
        )
    finally:
        dvigatel.dispose()


# --- откат, сужающий колонку -------------------------------------------------

INDEKSY = "f9b41c7e2d08"
DO_INDEKSOV = "c5e19a3d7b46"


def _otkatit(url: str, kuda: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, kuda)


def test_otkat_perezhivaet_dlinnyy_klyuch_etapa(chistaya_baza):
    """Спуск ниже `f9b41c7e2d08` возможен, даже когда ключ этапа длинный.

    Эта ревизия расширяет ключи этапов с 20 знаков до 32, а её откат сужает
    обратно. Комментарий обещал, что длинные ключи «при этом обрежутся», — но на
    MySQL со `STRICT_TRANS_TABLES` (умолчание, проект его не переопределяет)
    сужение колонки под существующими данными не обрезает молча, а падает с
    1265 «Data too long».

    Ключи такой длины — штатное дело, а не выдумка: `pipeline_service` режет
    основу ключа до 24 знаков и добавляет суффикс при совпадении. Ради ровно
    такого этапа («Waiting for customer approval») колонку и расширяли.

    Отказ приходился на ПЕРВОЕ действие отката, поэтому ревизия обрывалась в
    самом начале: `alembic_version` оставался на месте, и спуск ниже становился
    невозможен вовсе.
    """
    from sqlalchemy import create_engine, text

    _nakatit(chistaya_baza, INDEKSY)

    dlinnyy = "waiting_for_customer_app"  # 24 знака — как режет pipeline_service
    assert len(dlinnyy) > 20, "опыт бессмыслен: ключ помещается в старую колонку"

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            klient = _vstavit(soedinenie, "clients", name="Заказчик отката")
            zayavka = _vstavit(
                soedinenie, "deals", title="Заявка отката", client_id=klient
            )
            _vstavit(
                soedinenie,
                "deal_stage_changes",
                deal_id=zayavka,
                from_stage=dlinnyy,
                to_stage=dlinnyy,
            )

        _otkatit(chistaya_baza, DO_INDEKSOV)

        with dvigatel.connect() as soedinenie:
            stalo = soedinenie.execute(
                text("SELECT from_stage, to_stage FROM deal_stage_changes")
            ).all()
        assert stalo == [(dlinnyy[:20], dlinnyy[:20])], (
            f"после отката в журнале {stalo}, а ожидалось укорочение до двадцати знаков"
        )
    finally:
        dvigatel.dispose()


# --- накат с промежуточной точки ---------------------------------------------


def test_nakat_s_predposledney_revizii_dayot_polnuyu_skhemu(chistaya_baza):
    """Схема сходится с моделями и при накате С СЕРЕДИНЫ, а не только с нуля.

    **Это сторож на ошибку, которая уже стоила отката боевого сервера.**
    Выгруженную ревизию правили вместо того, чтобы добавить новую: дописали
    колонку в уже применённую миграцию. Alembic применённое заново не гоняет —
    колонка не появилась, модель её требовала, сверка схемы отказалась поднимать
    приложение, и обновление откатилось вместе с базой.

    Почему этого не поймала соседняя проверка соответствия: она строит схему
    С НУЛЯ, прогоняя всю цепочку. Правленая миграция при таком построении
    выглядит целой — колонка-то в ней есть. Расхождение появляется ровно там,
    где ревизия УЖЕ отмечена применённой, то есть на всякой живой установке.

    Отсюда способ проверки: доводим базу до предпоследней ревизии — состояния,
    в котором стоит сервер перед обновлением, — и накатываем остаток. Если
    последний шаг чего-то не доносит, здесь это видно, а не на бою.
    """
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from alembic.config import Config
    from database.schema_check import check

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", chistaya_baza)
    skript = ScriptDirectory.from_config(config)
    golova = skript.get_current_head()
    predposledniy = skript.get_revision(golova).down_revision
    assert predposledniy, "у головы нет предшественника — цепочка из одной ревизии?"

    # Состояние сервера ПЕРЕД обновлением.
    _nakatit(chistaya_baza, predposledniy)
    # И само обновление.
    _nakatit(chistaya_baza, "head")

    dvigatel = create_engine(chistaya_baza)
    try:
        otchyot = check(dvigatel)
    finally:
        dvigatel.dispose()

    assert otchyot.ok, (
        "после наката с предпоследней ревизии схема НЕ сходится с моделями:\n  "
        + otchyot.summary()
        + "\n\nСамая вероятная причина: правили уже выгруженную миграцию вместо "
        "того, чтобы добавить новую. Применённую ревизию alembic заново не "
        "гоняет, и на живой установке правка не выполнится никогда."
    )
