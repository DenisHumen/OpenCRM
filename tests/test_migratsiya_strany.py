"""Засев страны при накате `d7a15c93b402` — на НАСЕЛЁННОЙ базе.

Миграция дописывает существующим клиентам страну по коду набора в телефоне.
Беда в том, что местный номер кода страны не содержит, а на подбор по длине
отвечает как ни в чём не бывало: московский `4951234567` начинается с `49` —
Германия, петербургский `812…` — Япония, мобильные `916…`, `950…`, `981…` —
Индия, Мьянма, Иран. Флаг чужой страны лёг бы в карточку молча и навсегда:
телефон старого клиента больше никто не правит, а по стране считают доставку.

В самом коде оговорка есть — `client_service._mezhdunarodnyy`. В миграции её
не было, и прошлый прогон на двухстах тысячах строк её отсутствия не заметил:
местные номера в нём начинались с нуля, а нулём не начинается ни один код.
"""

from sqlalchemy import create_engine, text

STRANY = "d7a15c93b402"
DO_NEYO = "c4f92a1e8d73"

#: Местные номера, набранные ОПАСНО: первые цифры каждого — настоящий код
#: страны из таблицы миграции. Именно на них проверка и держится.
MESTNYE = {
    "4951234567": "DE",  # Москва читается Германией
    "8121234567": "JP",  # Петербург — Японией
    "9161234567": "IN",  # мобильный — Индией
    "9501234567": "MM",  # мобильный — Мьянмой
}

#: Международные: страна у них названа самим номером, и она обязана появиться.
MEZHDUNARODNYE = {
    ("+380671234567", "380671234567"): "UA",
    ("+49 151 1234567", "491511234567"): "DE",
    ("+7 916 123-45-67", "79161234567"): "RU",
    ("+7 701 123-45-67", "77011234567"): "KZ",
    ("00 48 123 456 789", "48123456789"): "PL",
    # Общий код: США, Канада и десяток островов. Страны не даёт — и не должен.
    ("+1 212 555-12-34", "12125551234"): "",
}


def _strany_po_imeni(url: str) -> dict[str, str]:
    dvigatel = create_engine(url)
    try:
        with dvigatel.connect() as soedinenie:
            return {
                imya: strana
                for imya, strana in soedinenie.execute(
                    text("SELECT name, country FROM clients")
                ).all()
            }
    finally:
        dvigatel.dispose()


def _zaseyat(url: str, naselit, kod_strany: str = "") -> None:
    dvigatel = create_engine(url)
    try:
        with dvigatel.begin() as soedinenie:
            if kod_strany:
                naselit(
                    soedinenie,
                    "site_settings",
                    [{"key": "default_country_code", "value": kod_strany}],
                )
            naselit(
                soedinenie,
                "clients",
                [
                    {"name": f"местный {nomer}", "phone": nomer, "phone_norm": nomer}
                    for nomer in MESTNYE
                ]
                + [
                    {"name": f"международный {syroy}", "phone": syroy, "phone_norm": norm}
                    for syroy, norm in MEZHDUNARODNYE
                ],
            )
    finally:
        dvigatel.dispose()


def test_mestnyy_nomer_ne_daet_strany(chistaya_baza, nakatit, naselit):
    """Местный номер страны не получил, международный получил свою.

    Обе половины нужны вместе. Без первой миграция раздаёт чужие флаги; без
    второй «страну не ставим никому» тоже прошло бы — и оговорка, поставленная
    против первой беды, тихо выключила бы всю пользу от засева.
    """
    nakatit(chistaya_baza, DO_NEYO)
    _zaseyat(chistaya_baza, naselit)
    nakatit(chistaya_baza, STRANY)

    strany = _strany_po_imeni(chistaya_baza)

    for nomer, chuzhaya in MESTNYE.items():
        assert strany[f"местный {nomer}"] == "", (
            f"местный {nomer} получил страну {strany[f'местный {nomer}']}"
            f" (подбор по длине читает его как {chuzhaya})"
        )
    for (syroy, _), zhdyom in MEZHDUNARODNYE.items():
        assert strany[f"международный {syroy}"] == zhdyom


def test_nazvannyy_kod_strany_snimaet_ogovorku(chistaya_baza, nakatit, naselit):
    """Владелец назвал код своей страны — местные номера снова судятся.

    Тогда `normalize_phone` уже дописал этот код местным номерам, и `phone_norm`
    у них начинается со СВОЕЙ страны. Оговорка про `+` тут не только лишняя, а
    вредна: она выключила бы засев ровно там, где он верен.
    """
    nakatit(chistaya_baza, DO_NEYO)
    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as soedinenie:
            naselit(
                soedinenie,
                "site_settings",
                [{"key": "default_country_code", "value": "380"}],
            )
            naselit(
                soedinenie,
                "clients",
                # Местный `067…`, которому нормализация уже дописала `380`.
                [{"name": "свой", "phone": "067 123-45-67", "phone_norm": "380671234567"}],
            )
    finally:
        dvigatel.dispose()

    nakatit(chistaya_baza, STRANY)

    assert _strany_po_imeni(chistaya_baza)["свой"] == "UA"
