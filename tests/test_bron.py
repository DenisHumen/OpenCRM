"""Бронь: кто держит товар и почему он не бронируется дважды.

Главная проверка здесь — вторая: заказ, заведённый из заявки, повторяет те же
товары, и наивное сложение двух источников удвоило бы бронь. Заметить такое на
экране нельзя: число выглядит правдоподобно, а продавец отказывает покупателю,
глядя на товар, который лежит на полке.

Разбор правила — `docs/19-sborka-zakaza.md` §Р3.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API

WH = f"{API}/warehouse"


@pytest.fixture(scope="module", autouse=True)
def bloki(root_client: TestClient):
    """Склад и заказы включены: бронь живёт на стыке."""
    from core.services import modules_service

    for blok in ("warehouse", "documents", "orders"):
        otvet = root_client.post(f"{API}/modules/{blok}", json={"enabled": True})
        # Код ответа проверяется: молчаливое переключение однажды откажет —
        # режим обслуживания, зависимость блока, — и файл упадёт не здесь, а на
        # 403 в первом же тесте, где про блоки не сказано ни слова.
        assert otvet.status_code == 200, f"{blok}: {otvet.text}"
    modules_service.invalidate()
    yield
    for blok in ("orders", "warehouse"):
        otvet = root_client.post(f"{API}/modules/{blok}", json={"enabled": False})
        assert otvet.status_code == 200, f"{blok}: {otvet.text}"
    modules_service.invalidate()


@pytest.fixture
def tovar(root_client: TestClient) -> dict:
    otvet = root_client.post(
        f"{WH}/products", json={"name": "Сервер под бронь", "price": 4_500_000}
    )
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


@pytest.fixture
def zayavka(root_client: TestClient) -> dict:
    """Заявка вместе со своим клиентом: заказ заводится по номеру записи.

    Не по имени: одноимённых клиентов набор создаёт по одному на проверку, и
    система законно просит уточнить, о ком речь.
    """
    klient = root_client.post(f"{API}/clients", json={"name": "Держатель брони"}).json()
    otvet = root_client.post(
        f"{API}/deals", json={"title": "Заявка с бронью", "client_id": klient["id"]}
    )
    assert otvet.status_code == 201, otvet.text
    return {"id": otvet.json()["id"], "client_id": klient["id"]}


def prihod(client: TestClient, product_id: int, skolko: str) -> None:
    otvet = client.post(
        f"{WH}/moves", json={"product_id": product_id, "kind": "in", "quantity": skolko}
    )
    assert otvet.status_code == 201, otvet.text


def nalichie(client: TestClient, product_id: int) -> dict:
    otvet = client.get(f"{WH}/products/{product_id}/availability")
    assert otvet.status_code == 200, otvet.text
    return otvet.json()


def test_stroka_zayavki_derzhit_tovar(root_client, tovar, zayavka):
    prihod(root_client, tovar["id"], "5")
    assert nalichie(root_client, tovar["id"])["available_milli"] == 5000

    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    est = nalichie(root_client, tovar["id"])
    assert est["stock_milli"] == 5000, "остаток не меняется от брони — товар ещё на полке"
    assert est["reserved_milli"] == 3000
    assert est["available_milli"] == 2000


def test_vidno_kakaya_zayavka_derzhit(root_client, tovar, zayavka):
    """«Доступно 2 из 5» без ответа «а где три» — это вопрос без ответа."""
    prihod(root_client, tovar["id"], "5")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    derzhateli = nalichie(root_client, tovar["id"])["holders"]
    assert len(derzhateli) == 1
    assert derzhateli[0]["kind"] == "deal"
    assert derzhateli[0]["id"] == zayavka["id"]
    assert derzhateli[0]["title"] == "Заявка с бронью"
    assert derzhateli[0]["quantity_milli"] == 3000


def test_zakaz_iz_zayavki_ne_udvaivaet_bron(root_client, tovar, zayavka):
    """Заказ ПЕРЕНИМАЕТ бронь заявки, а не добавляет свою.

    Иначе три сервера в заявке и те же три в заказе дали бы шесть в брони — и
    продавец отказал бы покупателю, глядя на число, которого нет.
    """
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})
    assert nalichie(root_client, tovar["id"])["reserved_milli"] == 3000

    zakaz = root_client.post(
        f"{API}/orders", json={"kind": "sales_order", "deal_id": zayavka["id"], "client_id": zayavka["client_id"]}
    )
    assert zakaz.status_code == 201, zakaz.text
    zakaz_id = zakaz.json()["id"]
    stroka = root_client.post(
        f"{API}/orders/{zakaz_id}/lines", json={"product_id": tovar["id"], "quantity": "3"}
    )
    assert stroka.status_code == 201, stroka.text

    est = nalichie(root_client, tovar["id"])
    assert est["reserved_milli"] == 3000, "бронь удвоилась: заявка и заказ считаются дважды"
    assert est["available_milli"] == 7000


def test_zakaz_na_chast_zayavki_derzhat_oba(root_client, tovar, zayavka):
    """Заказ взял два из трёх — заявка продолжает держать оставшийся один."""
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})
    zakaz_id = root_client.post(
        f"{API}/orders", json={"kind": "sales_order", "deal_id": zayavka["id"], "client_id": zayavka["client_id"]}
    ).json()["id"]
    root_client.post(f"{API}/orders/{zakaz_id}/lines", json={"product_id": tovar["id"], "quantity": "2"})

    est = nalichie(root_client, tovar["id"])
    assert est["reserved_milli"] == 3000, "два в заказе плюс один в заявке — всё те же три"
    vidy = sorted(d["kind"] for d in est["holders"])
    assert vidy == ["deal", "order"]


def test_zakrytaya_zayavka_ne_derzhit(root_client, tovar, zayavka):
    """Товар по закрытой заявке либо ушёл, либо не уйдёт: держать нечего."""
    prihod(root_client, tovar["id"], "5")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})
    etapy = root_client.get(f"{API}/pipeline/stages").json()["items"]
    lost = next(e["key"] for e in etapy if e["kind"] == "lost")
    root_client.post(f"{API}/deals/{zayavka['id']}/move", json={"stage": lost, "lost_reason": "передумали"})

    est = nalichie(root_client, tovar["id"])
    assert est["reserved_milli"] == 0
    assert est["holders"] == []


def test_nehvatka_preduprezhdaet_a_ne_zapreshchaet(root_client, tovar, zayavka):
    """Продавать то, что ещё едет, — обычное дело; отказ сломал бы работу."""
    prihod(root_client, tovar["id"], "2")
    otvet = root_client.post(
        f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "5"}
    )
    assert otvet.status_code == 201, "нехватка не должна быть отказом"
    assert otvet.json()["shortage_milli"] == 3000

    spisok = root_client.get(f"{API}/deals/{zayavka['id']}/lines").json()
    assert spisok["items"][0]["shortage_milli"] == 3000


def test_svoya_trata_ne_bronruet(root_client, tovar, zayavka):
    """У упаковки нет карточки товара — и держать ей нечего."""
    prihod(root_client, tovar["id"], "5")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"name": "Упаковка", "quantity": "1", "price": 1000})
    assert nalichie(root_client, tovar["id"])["reserved_milli"] == 0


def test_chastichnaya_otgruzka_ne_syedaet_bron_zayavki(root_client, tovar, zayavka):
    """Отгруженное по заказу не имеет права вычесться дважды.

    Накладная по заказу наследует `deal_id` заявки и пишет его в движения, то
    есть отгруженное попадает и в «списано под заявку». Не вычти его из
    «передано заказам» — одно и то же вычтется два раза, и заявка на пятнадцать
    штук с заказом на десять после отгрузки этих десяти покажет бронь НОЛЬ
    вместо пяти: товар, который клиент ещё ждёт, свободно уйдёт другому.
    """
    prihod(root_client, tovar["id"], "20")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "15"},
    )
    zakaz_id = root_client.post(
        f"{API}/orders",
        json={"kind": "sales_order", "deal_id": zayavka["id"], "client_id": zayavka["client_id"]},
    ).json()["id"]
    root_client.post(
        f"{API}/orders/{zakaz_id}/lines", json={"product_id": tovar["id"], "quantity": "10"}
    )
    assert nalichie(root_client, tovar["id"])["reserved_milli"] == 15000

    # Отгружаем НАСТОЯЩЕЙ накладной по этому заказу, сам заказ не закрывая:
    # обычная частичная отгрузка. Голым движением беду не воспроизвести —
    # отгруженное считается через бумагу, а у движения мимо бумаги её нет.
    assert root_client.post(f"{API}/modules/waybills", json={"enabled": True}).status_code == 200
    nakladnaya = root_client.post(f"{API}/waybills/from-order/{zakaz_id}")
    assert nakladnaya.status_code in (200, 201), nakladnaya.text
    provedena = root_client.post(f"{API}/waybills/{nakladnaya.json()['id']}/post", json={})
    assert provedena.status_code == 200, provedena.text

    est = nalichie(root_client, tovar["id"])
    assert est["reserved_milli"] == 5000, "бронь заявки съедена двойным вычитанием"
    assert est["stock_milli"] == 10000
    assert est["available_milli"] == 5000


def test_usluga_ne_derzhit_bron(root_client, zayavka):
    """У услуги остатка нет и быть не может — держать ей нечего.

    Строка «выезд мастера» иначе показывала бы вечную нехватку на экране
    заявки, а карточка услуги — бронь на товар, которого не существует.
    """
    usluga = root_client.post(
        f"{WH}/products", json={"name": "Выезд под бронь", "is_service": True, "price": 50_000}
    ).json()
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": usluga["id"], "quantity": "1"},
    )

    est = nalichie(root_client, usluga["id"])
    assert est["reserved_milli"] == 0
    assert est["holders"] == []
    stroki = root_client.get(f"{API}/deals/{zayavka['id']}/lines").json()["items"]
    assert all(s["shortage_milli"] == 0 for s in stroki), "услуга показала нехватку"


def test_zayavka_derzhit_tovar_i_bez_bloka_zakazov(root_client, tovar, zayavka):
    """Ради этого расчёт и вынесен из блока заказов — и ни разу не пройден.

    Весь файл гоняется при включённых заказах, а без них из слагаемых исчезают
    два. Студия со складом и без заказов иначе увидела бы «доступно 5 из 5» на
    товар, обещанный покупателю.
    """
    from core.services import modules_service

    prihod(root_client, tovar["id"], "5")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "3"},
    )
    assert root_client.post(f"{API}/modules/orders", json={"enabled": False}).status_code == 200
    modules_service.invalidate()
    try:
        est = nalichie(root_client, tovar["id"])
        assert est["reserved_milli"] == 3000, "без заказов заявка перестала держать товар"
        assert est["expected_milli"] == 0
        assert est["available_milli"] == 2000
        assert [d["kind"] for d in est["holders"]] == ["deal"]
    finally:
        assert root_client.post(f"{API}/modules/orders", json={"enabled": True}).status_code == 200
        modules_service.invalidate()


def test_vtoroy_zakaz_iz_zayavki_ne_zavoditsya(root_client, tovar, zayavka):
    """Кнопку нажимают дважды, и товар не может стать нужен вдвое.

    Второй заказ повторил бы те же строки, а `promised` посчитал бы их обоими:
    три штуки в заявке стали бы шестью в брони, и продавец отказал бы
    покупателю, глядя на товар, лежащий на полке.
    """
    prihod(root_client, tovar["id"], "10")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "3"},
    )
    assert root_client.post(f"{API}/deals/{zayavka['id']}/order").status_code == 201

    vtoroy = root_client.post(f"{API}/deals/{zayavka['id']}/order")
    assert vtoroy.status_code == 409, vtoroy.text
    assert vtoroy.json()["error"]["code"] == "deal_order_exists"
    assert nalichie(root_client, tovar["id"])["reserved_milli"] == 3000


def test_otmenyonnyy_zakaz_vozvrashchaet_obeshchanie_zayavke(root_client, tovar, zayavka):
    """Отменили заказ — обещание вернулось к заявке само, без единой уборки.

    Ради этого бронь и считается запросом, а не хранится: «передано заказу» —
    результат отбора по незакрытым бумагам заявки, и отменённая из него просто
    выпадает. Храни мы признак «в заказе» на строке, отмена обязана была бы его
    снять — и не сняла бы ровно в тот раз, когда отмену сделали не тем путём.

    Проверяются три состояния подряд, и среднее — не украшение: без него
    «вернулось» неотличимо от «никогда и не уходило».
    """
    prihod(root_client, tovar["id"], "10")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "3"},
    )
    do_zakaza = nalichie(root_client, tovar["id"])
    assert do_zakaza["reserved_milli"] == 3000
    assert [d["kind"] for d in do_zakaza["holders"]] == ["deal"]

    zakaz_id = root_client.post(
        f"{API}/orders",
        json={"kind": "sales_order", "deal_id": zayavka["id"], "client_id": zayavka["client_id"]},
    ).json()["id"]
    assert root_client.post(
        f"{API}/orders/{zakaz_id}/lines", json={"product_id": tovar["id"], "quantity": "3"}
    ).status_code == 201

    s_zakazom = nalichie(root_client, tovar["id"])
    assert s_zakazom["reserved_milli"] == 3000, "бронь удвоилась"
    assert [d["kind"] for d in s_zakazom["holders"]] == ["order"], (
        "обещание не перешло к заказу — значит и возвращать будет нечего"
    )

    otmena = root_client.post(f"{API}/orders/{zakaz_id}/cancel", json={})
    assert otmena.status_code == 200, otmena.text

    posle = nalichie(root_client, tovar["id"])
    assert posle["reserved_milli"] == 3000, (
        f"после отмены заказа бронь стала {posle['reserved_milli']} — товар перестал быть обещанным"
    )
    assert [d["kind"] for d in posle["holders"]] == ["deal"], (
        f"держатель после отмены: {posle['holders']} — обещание не вернулось к заявке"
    )
    assert posle["available_milli"] == 7000
