"""Круг «снял → залил в ЧИСТУЮ MySQL → сверил». Копия, которую ни разу не
разворачивали, — это надежда, а не копия.

В проекте есть снятие копии (`scripts/snapshot_db.py`, шаг `backup` в
`deploy/updater.py`, `scripts/backup.sh`) и есть проверка ФАЙЛА копии
(`scripts/verify_backup.py`, `tests/test_snapshot_db.py`). Не было ни одного
прогона, который бы этот файл ЗАЛИЛ: движок в `test_snapshot_db.py` подставной,
и он отвечает то, что ему велели отвечать. То есть проверено было всё, кроме
единственного вопроса, ради которого копия существует, — встанет ли из неё
система.

**Чем это отличается от репетиции отката** (`tests/test_otkat_naselyonnoy.py`,
`/home/crmtest/otkat` на стенде). Та спрашивает «умеет ли ОБНОВЛЕНИЕ вернуться
назад» и заливает то, что сама только что сняла: обе стороны врут одинаково, и
враньё дампера ей невидимо. Здесь спрашивается другое — «годится ли ЭТОТ ФАЙЛ,
чтобы из него встала система», и сверяется он не с собой, а с живой базой,
откуда снят. Нужны обе.

Дампер у нас рукописный (разбор — в шапке `scripts/snapshot_db.py`, довод
короткий: клиента MySQL в образе приложения нет и заводить его некуда). Цена
этого решения — экранирование, и три беды из неё растут:

1. **Враньё значений.** Кавычка, обратная косая, перевод строки, эмодзи в
   utf8mb4, длинный текст. Отсюда построчная сверка, а не сверка числа строк:
   потерянная кавычка числа строк не меняет.
2. **Неполнота набора таблиц.** Копия обязана нести ВСЕ таблицы живой базы, а
   не те, что были на день написания дампера. Новая таблица, не попавшая в
   копию, молчит до дня аварии.
3. **`alembic_version`.** Без неё восстановленная база не знает своей версии, и
   следующее обновление накатит миграции поверх уже накатанных.

Чего круг НЕ проверяет и почему: двоичные значения. Дампер их сегодня не
записывает вовсе — разбор и сторож в `test_dvoichnoe_znachenie_damper_poka_ne_beryot`.
"""

import os
import pathlib
import re
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pymysql.constants import CLIENT
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from database import schema_check
from database.models import Client
from database.session import Base
from scripts.snapshot_db import RAZMER_PACHKI, pochemu_ne_celaya, revizia, snyat

#: Значения, на которых ломается самодельное экранирование.
#:
#: Каждое здесь по своей причине, а не для количества: одиночная кавычка
#: закрывает литерал, обратная косая съедает следующий знак, `;` с переводом
#: строки разрезает дамп на этом месте, эмодзи не влезает в utf8, `\0` и `\Z`
#: MySQL требует экранировать особо, `%s` ломает подстановку в драйвере.
ZLYE = (
    "odna ' kavychka",
    'dvoynaya " kavychka',
    "obratnaya \\ kosaya",
    "dve \\\\ kosye",
    "konec;\nINSERT INTO clients (name) VALUES ('poddelka');",
    "perevod\nstroki, vozvrat\rkaretki i tab\ttut",
    "эмодзи 🌿 и знак ₴",
    "%s %d %% i {figurnye}",
    "nulevoy\x00bayt",
    "ctrl-z \x1a konec",
    "-- opencrm snapshot complete",
)

#: Строк в `clients` — заведомо больше одной пачки дампера.
#:
#: Пачка — это граница, на которой заканчивается один `INSERT` и начинается
#: следующий, а такие границы теряют строку молча.
KLIENTOV = RAZMER_PACHKI + 3

KOREN = pathlib.Path(__file__).resolve().parent.parent


# --- обвязка круга ------------------------------------------------------------


@contextmanager
def _dvizhok(url: str):
    """Движок, который закрывается в любом исходе."""
    dvizhok = create_engine(url)
    try:
        yield dvizhok
    finally:
        dvizhok.dispose()


@contextmanager
def _svoya_shema(hvost: str):
    """Отдельная схема на том же сервере, убирается в любом исходе.

    Не `chistaya_baza` (tests/conftest.py) по одной причине: та даётся на ОДНУ
    проверку, а источник круга живёт на весь файл — миграции до `head` стоят
    полминуты, и платить их четырежды не за что. Цель круга — как раз она.
    """
    osnovnoy = os.environ["OPENCRM_DB_URL"]
    koren, _, konec = osnovnoy.rpartition("/")
    imya, _, parametry = konec.partition("?")
    # Имя от базы набора, а не от имени проверки: базы на сервере общие, а
    # соседний агент гоняет тот же файл на своей.
    svoyo = f"{imya}_{hvost}"[:64]
    sluzhebnyy = create_engine(osnovnoy)
    try:
        with sluzhebnyy.connect() as soedinenie:
            soedinenie.execute(text(f"DROP DATABASE IF EXISTS {svoyo}"))
            soedinenie.execute(text(
                f"CREATE DATABASE {svoyo} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            ))
            soedinenie.commit()
        yield f"{koren}/{svoyo}?{parametry}" if parametry else f"{koren}/{svoyo}"
    finally:
        with sluzhebnyy.connect() as soedinenie:
            soedinenie.execute(text(f"DROP DATABASE IF EXISTS {svoyo}"))
            soedinenie.commit()
        sluzhebnyy.dispose()


def _zalit(url: str, damp: pathlib.Path) -> None:
    """Залить файл копии так же, как это делают руками: `mysql < ФАЙЛ`.

    Файл уезжает на сервер ЦЕЛИКОМ, одним разговором. Резать его по `;` нельзя:
    точка с запятой живёт и внутри значений, и самодельный резак проверял бы
    сам себя, а не копию — ровно то место, где дампер и врёт.
    """
    dvizhok = create_engine(url, connect_args={"client_flag": CLIENT.MULTI_STATEMENTS})
    syroe = dvizhok.raw_connection()
    try:
        kursor = syroe.cursor()
        kursor.execute(damp.read_text(encoding="utf-8"))
        while kursor.nextset():
            pass
        syroe.commit()
    finally:
        syroe.close()
        dvizhok.dispose()


def _tablicy(soedinenie) -> list[str]:
    stroki = soedinenie.execute(text("SHOW FULL TABLES")).all()
    return sorted(r[0] for r in stroki if r[1] == "BASE TABLE")


def _stroki(url: str) -> dict[str, list[tuple]]:
    """Все строки всех таблиц. Порядок задаём сами — база его не обещает."""
    with _dvizhok(url) as dvizhok, dvizhok.connect() as soedinenie:
        return {
            tablitsa: sorted(
                (tuple(r) for r in soedinenie.execute(text(f"SELECT * FROM `{tablitsa}`"))),
                key=repr,
            )
            for tablitsa in _tablicy(soedinenie)
        }


def _ustroystvo(url: str) -> dict[str, str]:
    """`SHOW CREATE TABLE` по всем таблицам, без счётчика автонумерации.

    Счётчик выбрасываем: он показывает СЛЕДУЮЩЕЕ значение, а оно у залитой
    таблицы своё — расхождение по нему означало бы «строки на месте», а не
    беду.
    """
    with _dvizhok(url) as dvizhok, dvizhok.connect() as soedinenie:
        return {
            tablitsa: re.sub(
                r" AUTO_INCREMENT=\d+",
                "",
                soedinenie.execute(text(f"SHOW CREATE TABLE `{tablitsa}`")).one()[1],
            )
            for tablitsa in _tablicy(soedinenie)
        }


def _chem_otlichayutsya(bylo: dict, stalo: dict, tablicy: list[str]) -> str:
    """Первая разошедшаяся строка каждой таблицы — рядом с тем, чем была.

    Без неё отказ говорит «таблицы разошлись» и умолкает, а разошлись они на
    одном знаке из десяти тысяч.
    """
    rasskaz = []
    for tablitsa in tablicy:
        levo, pravo = bylo[tablitsa], stalo.get(tablitsa, [])
        rasskaz.append(f"{tablitsa}: строк было {len(levo)}, стало {len(pravo)}")
        para = next(
            ((a, b) for a, b in zip(levo, pravo) if a != b),
            (levo[len(pravo)], None) if len(levo) > len(pravo) else None,
        )
        if para:
            rasskaz.append(f"    было:  {para[0]!r:.300}")
            rasskaz.append(f"    стало: {para[1]!r:.300}")
    return "залитая копия разошлась с базой, откуда снята:\n" + "\n".join(rasskaz)


# --- сам круг -----------------------------------------------------------------


@pytest.fixture(scope="module")
def snyataya_kopiya(tmp_path_factory):
    """Населённая база на `head` и снятая с неё копия. Один раз на файл.

    Данные — настоящие таблицы продукта, а не выдуманная табличка: экранирование
    ломается на значениях, и значения обязаны лежать там, где они лежат в жизни.
    """
    from alembic import command
    from alembic.config import Config

    with _svoya_shema("kopiya_otkuda") as otkuda:
        config = Config(str(KOREN / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", otkuda)
        command.upgrade(config, "head")

        _naselit_zlom(otkuda)

        damp = tmp_path_factory.mktemp("kopiya") / "opencrm.sql"
        with _dvizhok(otkuda) as dvizhok:
            _, strok = snyat(dvizhok, damp)
        # Круг начинается с годного файла: иначе всё, что ниже, проверяло бы
        # не копию, а собственную заливку пустоты.
        assert pochemu_ne_celaya(damp) == "", pochemu_ne_celaya(damp)
        assert strok > KLIENTOV, f"копия сняла {strok} строк — засев не доехал"

        yield SimpleNamespace(otkuda=otkuda, damp=damp)


def _naselit_zlom(url: str) -> None:
    """Положить в базу то, на чём ломается экранирование.

    `Client` заводится моделью — она знает сегодняшние колонки и умолчания, и
    засев переживёт любую будущую миграцию. Запись в ленте — запросом: на её
    вставку через модель подписан слушатель в `tests/conftest.py`, и ничья
    запись отсюда покраснела бы у соседа в `test_audit.py`.
    """
    zapolnitel = [Client(name=f"Zapolnitel {n:04d}") for n in range(KLIENTOV - len(ZLYE))]
    zlye = [
        Client(name=zloe[:200], company=zloe[:200], tags=zloe[:100], address=zloe[:300])
        for zloe in ZLYE
    ]
    with _dvizhok(url) as dvizhok:
        with Session(dvizhok) as sessiya:
            sessiya.add_all(zapolnitel + zlye)
            sessiya.commit()
            klient_id = zlye[0].id
        with dvizhok.begin() as soedinenie:
            soedinenie.execute(
                text(
                    "INSERT INTO client_notes (client_id, kind, body, happened_at)"
                    " VALUES (:client_id, :kind, :body, :happened_at)"
                ),
                [
                    {
                        "client_id": klient_id,
                        "kind": "note",
                        "body": zloe + "\n" + ("dlinnyy hvost 🌿 " * 400),
                        "happened_at": datetime(2026, 9, 2, 10, 11, 12, 123456),
                    }
                    for zloe in ZLYE
                ],
            )


def test_stroki_perezhivayut_zalivku_v_chistuyu_bazu(chistaya_baza, snyataya_kopiya):
    """Построчно, а не по числу строк: съеденная кавычка числа не меняет.

    Сверяется ВСЁ, что есть в базе, — и засеянное злом, и то, что насеяли сами
    миграции (права ролей, этапы воронки, склад «Основной»). Строка, потерянная
    дампером в любой из них, вылезет здесь.
    """
    _zalit(chistaya_baza, snyataya_kopiya.damp)

    bylo = _stroki(snyataya_kopiya.otkuda)
    stalo = _stroki(chistaya_baza)

    # Засев на месте — иначе сверка двух пустот была бы зелёной.
    with _dvizhok(snyataya_kopiya.otkuda) as dvizhok, dvizhok.connect() as soedinenie:
        imena = {imya for (imya,) in soedinenie.execute(text("SELECT name FROM clients"))}
    ne_doehali = [zloe for zloe in ZLYE if zloe[:200] not in imena]
    assert not ne_doehali, f"злые значения не легли в базу: {ne_doehali}"
    assert len(bylo["clients"]) == KLIENTOV, (
        f"в базе {len(bylo['clients'])} клиентов вместо {KLIENTOV} — пачка дампера "
        f"({RAZMER_PACHKI}) больше не перекрыта, и её границу круг не проходит"
    )

    razoshlis = [t for t in sorted(bylo) if bylo[t] != stalo.get(t)]
    assert not razoshlis, _chem_otlichayutsya(bylo, stalo, razoshlis)


def test_v_kopii_ves_nabor_tablits_i_ikh_ustroystvo(chistaya_baza, snyataya_kopiya):
    """Из залитой копии система обязана подняться, а не «почти подняться».

    Спрашивается тем же, чем спрашивает точка входа: `schema_check.check`. Он
    сверяет живую схему с моделями, и приложение с несошедшейся схемой не
    поднимается вовсе — значит его отказ здесь и есть «система не встала».

    Устройство таблиц сверяется отдельно от их набора: индекс, внешний ключ и
    сличение потерять можно, не потеряв ни одной колонки, и тогда система
    встанет — и будет искать по 200 000 карточек перебором.
    """
    _zalit(chistaya_baza, snyataya_kopiya.damp)

    with _dvizhok(chistaya_baza) as dvizhok:
        otchyot = schema_check.check(dvizhok)
    assert otchyot.ok, f"из копии система не встанет: {otchyot.summary()}"

    bylo, stalo = _ustroystvo(snyataya_kopiya.otkuda), _ustroystvo(chistaya_baza)
    assert set(stalo) == set(bylo), (
        f"копия принесла не тот набор таблиц: нет {sorted(set(bylo) - set(stalo))}, "
        f"лишние {sorted(set(stalo) - set(bylo))}"
    )
    inache = [t for t in sorted(bylo) if bylo[t] != stalo[t]]
    assert not inache, "устройство таблиц не воспроизвелось: " + "; ".join(
        f"{t}\n  было:  {bylo[t]}\n  стало: {stalo[t]}" for t in inache
    )


def test_kopiya_pomnit_reviziyu_alembic(chistaya_baza, snyataya_kopiya):
    """Без `alembic_version` база не знает своей версии.

    Беда тихая и отложенная: система на такой базе поднимется, а следующее
    обновление начнёт накатывать миграции с самого начала — по данным, где они
    уже накачены. `alembic_version` в моделях не описана (её ведёт сам
    alembic), поэтому сверка схемы её пропажу не заметит.
    """
    _zalit(chistaya_baza, snyataya_kopiya.damp)

    with _dvizhok(snyataya_kopiya.otkuda) as istochnik, _dvizhok(chistaya_baza) as tsel:
        bylo, stalo = revizia(istochnik), revizia(tsel)

    assert bylo != "none", "в базе-источнике нет отметки миграций — сверять нечего"
    assert stalo == bylo, (
        f"залитая копия стоит на ревизии {stalo!r} вместо {bylo!r}: следующее "
        "обновление накатит миграции поверх уже накаченных"
    )


#: Типы колонок, значения которых дампер не умеет записывать.
DVOICHNYE = (sa.LargeBinary, sa.BINARY, sa.VARBINARY, sa.BLOB)


def test_dvoichnoe_znachenie_damper_poka_ne_beryot(chistaya_baza, tmp_path):
    """Двоичное значение валит снятие копии, и в моделях его поэтому быть не должно.

    `pymysql` отдаёт двоичный литерал строкой с суррогатами (`escape_bytes`
    декодирует байты как ascii с `surrogateescape`), а дамп пишется в utf-8 —
    любой байт от 0x80 роняет снятие с `UnicodeEncodeError`. Сегодня беда спит:
    двоичных колонок в моделях нет ни одной.

    Сторож стоит на обоих концах. Появится двоичная колонка — обновление
    перестанет снимать копию перед миграциями, то есть откатываться станет
    некуда; научится дампер двоичному — покраснеет первая половина, и тогда
    двоичное значение надо гнать по кругу наравне с остальными.
    """
    with _dvizhok(chistaya_baza) as dvizhok:
        with dvizhok.begin() as soedinenie:
            soedinenie.execute(text(
                "CREATE TABLE proba_dvoichnogo (id INT PRIMARY KEY, znachenie VARBINARY(8))"
            ))
            soedinenie.execute(
                text("INSERT INTO proba_dvoichnogo VALUES (1, :znachenie)"),
                {"znachenie": bytes([0x00, 0x7F, 0x80, 0xFF])},
            )
        try:
            snyat(dvizhok, tmp_path / "damp.sql")
        except UnicodeEncodeError:
            umeet = False
        else:
            umeet = True
    assert not umeet, (
        "дампер научился записывать двоичное значение — сверка ниже больше не "
        "нужна, а двоичное значение надо гнать по кругу наравне с остальными "
        "(ZLYE в этом файле)"
    )

    nashlis = [
        f"{tablitsa}.{kolonka.name}"
        for tablitsa, opisanie in sorted(Base.metadata.tables.items())
        for kolonka in opisanie.columns
        if isinstance(kolonka.type, DVOICHNYE)
    ]
    assert not nashlis, (
        "двоичные колонки в моделях есть, а дампер их не записывает — копия "
        f"перед миграциями снята не будет вовсе: {', '.join(nashlis)}"
    )
