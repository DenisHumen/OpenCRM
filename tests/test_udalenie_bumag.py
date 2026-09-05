"""Бумага, заведённая по ошибке и ничего не сделавшая, удаляется; жившая — нет.

Правило «бланк не удаляется никогда» смягчено владельцем 05.09.2026: заведённая
случайно бумага висела в списке вечно, и отмена этого не лечила — отменённая
остаётся в списке. Граница проходит по следам: движения склада, деньги,
накладные по основанию, проведение. Есть след — бумага жила, и её не тронуть.
"""

import itertools

import pytest

from tests.conftest import API

DOCS = f"{API}/documents"
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


@pytest.fixture
def client_row(root_client):
    return root_client.post(f"{API}/clients", json={"name": f"Клиент {uniq()}"}).json()


def product(root_client, stock="10"):
    item = root_client.post(
        f"{STOCK}/products", json={"name": f"Деталь {uniq()}", "price": 500, "cost": 100}
    ).json()
    root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": stock, "kind": "in"})
    return item


def _v_zhurnale(root_client, number: str) -> list[dict]:
    return [
        e for e in root_client.get(f"{API}/audit", params={"action": "document.deleted"}).json()["items"]
        if e["entity_label"] == number
    ]


def test_kvitantsiya_po_oshibke_udalyaetsya_i_ostavlyaet_sled(root_client, client_row):
    doc = root_client.post(f"{DOCS}", json={"client_id": client_row["id"], "item": "Ноутбук"}).json()
    r = root_client.delete(f"{DOCS}/{doc['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": doc["id"], "number": doc["number"], "deleted": True}
    assert root_client.get(f"{DOCS}/{doc['id']}").status_code == 404
    sled = _v_zhurnale(root_client, doc["number"])
    assert sled and sled[0]["value_before"] == "intake"


def test_zakrytuyu_kvitantsiyu_udalit_nelzya(root_client, client_row):
    doc = root_client.post(f"{DOCS}", json={"client_id": client_row["id"], "item": "Телефон"}).json()
    for status in ("in_progress", "ready", "closed"):
        assert root_client.post(f"{DOCS}/{doc['id']}/status", json={"status": status}).status_code == 200
    r = root_client.delete(f"{DOCS}/{doc['id']}")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "document_in_use"
    assert root_client.get(f"{DOCS}/{doc['id']}").status_code == 200


def test_zakaz_bez_sledov_udalyaetsya_a_provedyonnyy_net(root_client, client_row):
    item = product(root_client)
    pustoy = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(f"{ORDERS}/{pustoy['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    assert root_client.delete(f"{ORDERS}/{pustoy['id']}").status_code == 200

    otgruzhen = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(f"{ORDERS}/{otgruzhen['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    assert root_client.post(f"{ORDERS}/{otgruzhen['id']}/close", json={}).status_code == 200
    r = root_client.delete(f"{ORDERS}/{otgruzhen['id']}")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "document_in_use"


def test_zakaz_s_provedyonnoy_nakladnoy_ne_udalyaetsya_a_chernovik_ukhodit_s_nim(root_client, client_row):
    """Проведённая накладная помнит основание — заказ держится; черновик по
    заказу без заказа не значит ничего и уходит вместе с ним."""
    item = product(root_client)
    order = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    chernoviki = [w for w in root_client.get(f"{WAYBILLS}", params={"basis_id": order["id"]}).json()["items"]]
    assert chernoviki, "черновик по заказу заводится сам"
    assert root_client.delete(f"{ORDERS}/{order['id']}").status_code == 200
    assert root_client.get(f"{WAYBILLS}/{chernoviki[0]['id']}").status_code == 404

    order2 = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(f"{ORDERS}/{order2['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    wb = root_client.get(f"{WAYBILLS}", params={"basis_id": order2["id"]}).json()["items"][0]
    assert root_client.post(f"{WAYBILLS}/{wb['id']}/post", json={}).status_code == 200
    # Накладная закрыла заказ (05.09.2026): удалять нельзя уже потому, что он
    # закрыт, — а проведённая бумага по основанию держит его и так.
    r = root_client.delete(f"{ORDERS}/{order2['id']}")
    assert r.status_code == 422, r.text


def test_chernovik_nakladnoy_udalyaetsya_provedyonnaya_net(root_client, client_row):
    item = product(root_client)
    wb = root_client.post(f"{WAYBILLS}", json={"kind": "waybill_out", "client_id": client_row["id"]}).json()
    root_client.post(f"{WAYBILLS}/{wb['id']}/lines", json={"product_id": item["id"], "quantity": "1", "price": 500})
    assert root_client.delete(f"{WAYBILLS}/{wb['id']}").status_code == 200
    assert root_client.get(f"{WAYBILLS}/{wb['id']}").status_code == 404

    wb2 = root_client.post(f"{WAYBILLS}", json={"kind": "waybill_out", "client_id": client_row["id"]}).json()
    root_client.post(f"{WAYBILLS}/{wb2['id']}/lines", json={"product_id": item["id"], "quantity": "1", "price": 500})
    assert root_client.post(f"{WAYBILLS}/{wb2['id']}/post", json={}).status_code == 200
    r = root_client.delete(f"{WAYBILLS}/{wb2['id']}")
    assert r.status_code == 422 and r.json()["error"]["code"] == "document_in_use"


def test_chuzhoy_razdel_ne_udalyaet_ne_svoyo(root_client, client_row):
    """Заказ и квитанция живут в одной таблице: ручка бланков заказ не трогает."""
    order = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    r = root_client.delete(f"{DOCS}/{order['id']}")
    assert r.status_code == 422 and r.json()["error"]["code"] == "document_wrong_section"
    assert root_client.delete(f"{WAYBILLS}/{order['id']}").status_code == 422
