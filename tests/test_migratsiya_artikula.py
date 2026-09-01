"""Засев артикулов при накате `c4f92a1e8d73` — на НАСЕЛЁННОЙ базе.

Набор строит схему с нуля, значит `UPDATE` этой миграции идёт по нулю строк: он
зелёный и не доказывает ничего. Это сказано в шапке самой миграции, и там же
приведён ручной прогон на пятидесяти тысячах товаров. Ручной прогон живёт один
раз — следующая правка отбора «наших» артикулов его не повторит, а покраснеть
будет негде: на боевой базе `ALTER … NOT NULL` упадёт на первой же строке без
артикула, и обновление откатит и код, и базу.

Беда здесь однажды была настоящей. Максимум искали по верхним пятидесяти
строкам диапазона, а `A-00012300` — восемь цифр вместо шести — при побайтном
сравнении стоит ВЫШЕ любого нашего и остаётся в диапазоне. Полсотни таких
заслоняли настоящий максимум, счёт шёл с единицы и упирался в занятое:
`Duplicate entry 'A-000123'` посреди миграции.
"""

import pathlib

from sqlalchemy import create_engine, text

ROOT = pathlib.Path(__file__).resolve().parent.parent

ARTIKUL = "c4f92a1e8d73"
DO_NEYO = "b8d41f7a2c95"

#: Товаров заведомо больше, чем окно поиска максимума, и больше, чем три
#: попытки подбора: на малых числах повтор дотягивается до нужного номера сам,
#: и сломанный отбор выглядит исправным.
SKOLKO_TOVAROV = 300

#: Ручной артикул НАШЕГО вида и полсотни подделок ВЫШЕ него.
RUCHNOY = "A-000123"
PODDELKI = [f"{RUCHNOY}{n:02d}" for n in range(50)]
CHUZHIE = ["ZX-9", "A-777", "A-B00000"]


def _nakatit(url: str, kuda: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, kuda)


def _obyazatelnye(soedinenie) -> dict:
    """Чем заполнить колонки без умолчания.

    Спрашивается у базы, а не выписывается списком: выписанный устареет на
    первой же миграции с новой колонкой и покраснеет отказом ВСТАВКИ, ничего не
    сказав про засев.
    """
    kolonki = soedinenie.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'"
            " AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL"
            " AND EXTRA NOT LIKE '%auto_increment%'"
        )
    ).all()
    return {
        imya: (0 if tip in ("int", "bigint", "tinyint", "decimal") else "")
        for imya, tip in kolonki
        if imya not in ("name", "sku")
    }


def _zavesti(soedinenie, imena_i_sku) -> None:
    zapolniteli = _obyazatelnye(soedinenie)
    stolbtsy = ["name", "sku", *zapolniteli]
    soedinenie.execute(
        text(
            f"INSERT INTO products ({', '.join(f'`{s}`' for s in stolbtsy)}) "
            f"VALUES ({', '.join(f':{s}' for s in stolbtsy)})"
        ),
        [{"name": imya, "sku": sku, **zapolniteli} for imya, sku in imena_i_sku],
    )


def test_zasev_artikulov_prodolzhaet_schyot_posle_ruchnogo(chistaya_baza):
    """Каждому товару достался СВОЙ артикул, и счёт продолжился после ручного.

    Опасностей три, и все молчаливые. Одинаковый артикул двоим — `UNIQUE`
    уронит `ALTER` посреди обновления. Счёт с единицы — упрётся в заведённый
    руками `A-000123`. Счёт от похожего, но чужого (`A-777`, `A-B00000`,
    подделки) — уведёт всю будущую нумерацию.
    """
    _nakatit(chistaya_baza, DO_NEYO)

    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            _zavesti(
                soedinenie,
                [(f"Товар {n}", None) for n in range(SKOLKO_TOVAROV)]
                + [(f"Ручной {sku}", sku) for sku in [RUCHNOY, *PODDELKI, *CHUZHIE]],
            )

        _nakatit(chistaya_baza, ARTIKUL)

        with dvigatel.connect() as soedinenie:
            stroki = soedinenie.execute(text("SELECT name, sku FROM products")).all()
            pusto = soedinenie.execute(
                text("SELECT COUNT(*) FROM products WHERE sku IS NULL OR sku = ''")
            ).scalar()

        vse = [sku for _, sku in stroki]
        assert len(vse) == SKOLKO_TOVAROV + 1 + len(PODDELKI) + len(CHUZHIE)
        assert pusto == 0, "колонка не заполнена — `NOT NULL` навешен на пустоту"
        assert len(set(vse)) == len(vse), "миграция выдала одинаковые артикулы"

        for sku in [RUCHNOY, *PODDELKI, *CHUZHIE]:
            assert sku in vse, f"ручной артикул {sku} переписан миграцией"

        # Ручной артикул тоже нашего вида — из сравнения его исключаем: сверяем
        # ВЫДАННЫЕ, а он был заведён руками.
        nachalo = int(RUCHNOY[2:]) + 1
        vydannye = sorted(
            int(sku[2:])
            for sku in vse
            if sku != RUCHNOY and sku.startswith("A-") and len(sku) == 8 and sku[2:].isdigit()
        )
        assert vydannye == list(range(nachalo, nachalo + SKOLKO_TOVAROV)), (
            f"счёт пошёл не после ручного: {vydannye[:3]}…{vydannye[-3:]}"
        )
    finally:
        dvigatel.dispose()
