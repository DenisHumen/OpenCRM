"""Накладная по заказу заводится сама и повторяет заказ, пока не проведена.

Просьба владельца 05.09.2026 («связь блоков»): бумага появляется с первой
товарной строки заказа, а не по кнопке в конце; правка заказа переписывает
черновик; закрытие заказа проводит этот черновик, а не заводит второй; отмена
заказа уносит черновик. Выключается настройкой `auto_waybill`, а при
выключенном блоке накладных не работает вовсе — блока нет.
"""

import itertools

import pytest

from tests.conftest import API

ORDERS = f"{API}/orders"
WAYBILLS = f"{API}/waybills"
STOCK = f"{API}/warehouse"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client):
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    root_client.patch(f"{API}/settings", json={"values": {"auto_waybill": "1"}})


@pytest.fixture
def client_row(root_client):
    return root_client.post(f"{API}/clients", json={"name": f"Покупатель {uniq()}"}).json()


def product(root_client, stock="10", service=False):
    item = root_client.post(
        f"{STOCK}/products",
        json={"name": f"Деталь {uniq()}", "price": 500, "cost": 100, "is_service": service},
    ).json()
    if not service:
        root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": stock, "kind": "in"})
    return item


def order(root_client, client_row, kind="sales_order"):
    r = root_client.post(f"{ORDERS}", json={"kind": kind, "client_id": client_row["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def chernoviki(root_client, order_id: int) -> list[dict]:
    return root_client.get(f"{WAYBILLS}", params={"basis_id": order_id}).json()["items"]


def stroki(root_client, waybill_id: int) -> list[dict]:
    return root_client.get(f"{WAYBILLS}/{waybill_id}").json()["lines"]


def test_chernovik_poyavlyaetsya_s_pervoy_tovarnoy_strokoy_i_povtoryaet_zakaz(root_client, client_row):
    item = product(root_client)
    z = order(root_client, client_row)
    assert chernoviki(root_client, z["id"]) == [], "пустой заказ — бумаги нет"

    usluga = product(root_client, service=True)
    root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": usluga["id"], "quantity": "1"})
    assert chernoviki(root_client, z["id"]) == [], "услуга не отгружается — бумаги нет"

    line = root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "3"}).json()
    [wb] = chernoviki(root_client, z["id"])
    assert wb["status"] == "draft" and wb["kind"] == "waybill_out"
    assert [(s["product_id"], s["quantity_milli"]) for s in stroki(root_client, wb["id"])] == [(item["id"], 3000)]

    root_client.patch(f"{ORDERS}/{z['id']}/lines/{line['id']}", json={"quantity": "5"})
    assert [s["quantity_milli"] for s in stroki(root_client, wb["id"])] == [5000]

    root_client.delete(f"{ORDERS}/{z['id']}/lines/{line['id']}")
    assert chernoviki(root_client, z["id"]) == [], "товарных строк не осталось — черновик ушёл"


def test_zakaz_postavshchiku_zavodit_prikhodnuyu(root_client, client_row):
    item = product(root_client)
    z = order(root_client, client_row, kind="purchase_order")
    root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "2", "price": 100})
    [wb] = chernoviki(root_client, z["id"])
    assert wb["kind"] == "waybill_in"


def test_zakrytie_provodit_svoy_chernovik_a_ne_zavodit_vtoroy(root_client, client_row):
    """Количества черновика правятся руками («собрано четыре») — уезжает то,
    что в черновике, а вторая накладная по заказу не появляется."""
    item = product(root_client, stock="10")
    z = order(root_client, client_row)
    root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "5"})
    [wb] = chernoviki(root_client, z["id"])
    [line] = stroki(root_client, wb["id"])
    assert root_client.patch(f"{WAYBILLS}/{wb['id']}/lines/{line['id']}", json={"quantity": "4"}).status_code == 200

    assert root_client.post(f"{ORDERS}/{z['id']}/close", json={}).status_code == 200
    vse = chernoviki(root_client, z["id"])
    assert len(vse) == 1 and vse[0]["id"] == wb["id"] and vse[0]["status"] == "issued"
    ostatok = root_client.get(f"{STOCK}/products/{item['id']}").json()["stock_milli"]
    assert ostatok == 6000, "уехало то, что стояло в черновике: четыре"


def test_otmena_zakaza_unosit_svoy_chernovik(root_client, client_row):
    item = product(root_client)
    z = order(root_client, client_row)
    root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    [wb] = chernoviki(root_client, z["id"])
    assert root_client.post(f"{ORDERS}/{z['id']}/cancel").status_code == 200
    assert root_client.get(f"{WAYBILLS}/{wb['id']}").status_code == 404


def test_ruchnoy_chernovik_zakaz_ne_trogaet(root_client, client_row):
    """Заведённый руками черновик по заказу человек и уберёт: зеркало его не
    удаляет, но строки повторяет — накладная по заказу обязана совпадать с ним."""
    root_client.patch(f"{API}/settings", json={"values": {"auto_waybill": "0"}})
    try:
        item = product(root_client)
        z = order(root_client, client_row)
        root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
        assert chernoviki(root_client, z["id"]) == [], "автоматика выключена"
        wb = root_client.post(f"{WAYBILLS}/from-order/{z['id']}").json()
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"auto_waybill": "1"}})
    assert root_client.post(f"{ORDERS}/{z['id']}/cancel").status_code == 200
    assert root_client.get(f"{WAYBILLS}/{wb['id']}").status_code == 200, "ручной черновик остался"


def test_bez_bloka_nakladnykh_chernovika_net(root_client, client_row):
    root_client.post(f"{API}/modules/waybills", json={"enabled": False})
    try:
        item = product(root_client)
        z = order(root_client, client_row)
        r = root_client.post(f"{ORDERS}/{z['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
        assert r.status_code == 201, r.text
    finally:
        root_client.post(f"{API}/modules/waybills", json={"enabled": True})
    assert chernoviki(root_client, z["id"]) == []
