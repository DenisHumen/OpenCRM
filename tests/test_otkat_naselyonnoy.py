"""Откат миграций на НАСЕЛЁННОЙ базе: данные обязаны пережить круг.

Соседняя проверка (`test_schema_conventions.py::test_every_migration_can_be_rolled_back`)
гоняет круг «вверх — вниз — вверх» на пустой базе и сверяет схему. Это ловит
забытый индекс, но молчит о главном, ради чего откат вообще существует: на
боевом сервере откатывают базу С ДАННЫМИ, и вопрос там один — целы ли они.

Разница не теоретическая. `DROP COLUMN` на пустой таблице проходит всегда;
на населённой он ведёт себя иначе и упирается в то, чего на пустой нет вовсе:
внешние ключи со строками по обе стороны, `NOT NULL` с уже записанными
значениями, индексы, построенные по данным.

Проверяется круг вокруг ПОСЛЕДНЕЙ пачки миграций, а не вся история: сеять
осмысленные данные на каждой ревизии за два года — работа, которая устареет
на следующей неделе, а ловит она то же самое.
"""

import pathlib

from sqlalchemy import create_engine, text

KOREN = pathlib.Path(__file__).resolve().parent.parent

#: Ревизия ДО пачки и голова после неё. Между ними: артикул всем товарам,
#: адрес клиента, строки заявки.
DO_PACHKI = "b8d41f7a2c95"
POSLE_PACHKI = "e2b64f1a7c85"

TOVAROV = 40
KLIENTOV = 25


def _skolko(url: str) -> dict[str, int]:
    dvigatel = create_engine(url)
    try:
        with dvigatel.connect() as soedinenie:
            return {
                tablitsa: soedinenie.scalar(text(f"SELECT COUNT(*) FROM {tablitsa}"))
                for tablitsa in ("products", "clients", "deals")
            }
    finally:
        dvigatel.dispose()


def test_dannye_perezhivayut_krug_vverkh_i_vniz(chistaya_baza, nakatit, naselit):
    """Накатили пачку на населённую базу, откатили — строки на месте.

    Три опасности, и все три видны только на данных. `DROP TABLE deal_lines`
    при живых ссылках на заявки и товары. `DROP COLUMN` четырёх колонок адреса,
    заполненных засевом миграции. И `sku`, который на пути вверх стал
    обязательным и уникальным: обратный ход обязан снять оба ограничения, не
    тронув сами значения — артикулы уже наклеены на коробки.
    """
    nakatit(chistaya_baza, DO_PACHKI)

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            naselit(
                soedinenie,
                "products",
                [{"name": f"Товар {n}", "sku": None} for n in range(TOVAROV)],
            )
            naselit(
                soedinenie,
                "clients",
                [
                    {"name": f"Клиент {n}", "phone": f"+38067000{n:04d}",
                     "phone_norm": f"38067000{n:04d}"}
                    for n in range(KLIENTOV)
                ],
            )
    finally:
        dvigatel.dispose()

    bylo = _skolko(chistaya_baza)
    assert bylo["products"] == TOVAROV and bylo["clients"] == KLIENTOV

    nakatit(chistaya_baza, POSLE_PACHKI)
    naverkhu = _skolko(chistaya_baza)
    assert naverkhu == bylo, f"накат потерял строки: {bylo} → {naverkhu}"

    _otkatit(chistaya_baza, DO_PACHKI)
    stalo = _skolko(chistaya_baza)
    assert stalo == bylo, f"откат потерял строки: {bylo} → {stalo}"

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.connect() as soedinenie:
            # Артикулы, выданные миграцией, откат НЕ стирает: они уже наклеены
            # на коробки, и вернуть их потом будет неоткуда.
            s_artikulom = soedinenie.scalar(
                text("SELECT COUNT(*) FROM products WHERE sku IS NOT NULL AND sku <> ''")
            )
            # Колонок адреса быть не должно — иначе откат не отработал вовсе.
            kolonok = soedinenie.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clients'"
                    " AND COLUMN_NAME IN ('country', 'city', 'zip_code', 'address')"
                )
            )
            tablitsa_strok = soedinenie.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'deal_lines'"
                )
            )
    finally:
        dvigatel.dispose()

    assert s_artikulom == TOVAROV, f"откат стёр выданные артикулы: осталось {s_artikulom}"
    assert kolonok == 0, "откат не снял колонки адреса"
    assert tablitsa_strok == 0, "откат не снёс таблицу строк заявки"


def test_povtornyy_nakat_posle_otkata_prokhodit(chistaya_baza, nakatit, naselit):
    """Откатили и накатили снова — на тех же данных, а не на пустоте.

    Это и есть настоящий сценарий починки: обновление встало, база вернулась
    назад, беду поправили, накатили заново. Второй накат идёт по данным, где
    артикулы УЖЕ выданы предыдущим накатом, — и обязан не подавиться ими.
    Проверка написана на эту беду: засев артикулов ищет максимум среди своих, и
    остатки прошлого наката для него неотличимы от ручных.
    """
    nakatit(chistaya_baza, DO_PACHKI)
    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            naselit(
                soedinenie,
                "products",
                [{"name": f"Товар {n}", "sku": None} for n in range(TOVAROV)],
            )
    finally:
        dvigatel.dispose()

    nakatit(chistaya_baza, POSLE_PACHKI)
    _otkatit(chistaya_baza, DO_PACHKI)
    nakatit(chistaya_baza, POSLE_PACHKI)

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.connect() as soedinenie:
            vse = [sku for (sku,) in soedinenie.execute(text("SELECT sku FROM products"))]
    finally:
        dvigatel.dispose()

    assert len(vse) == TOVAROV
    assert all(vse), "после повторного наката остались товары без артикула"
    assert len(set(vse)) == len(vse), "повторный накат выдал одинаковые артикулы"


def _otkatit(url: str, kuda: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(KOREN / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, kuda)
