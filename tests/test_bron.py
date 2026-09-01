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
        root_client.post(f"{API}/modules/{blok}", json={"enabled": True})
    modules_service.invalidate()
    yield
    for blok in ("orders", "warehouse"):
        root_client.post(f"{API}/modules/{blok}", json={"enabled": False})
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
