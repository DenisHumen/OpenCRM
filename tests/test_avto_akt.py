"""Акт работ по заявке заводится сам и повторяет её строки, пока не проведён.

Просьба владельца 05.09.2026 («связь блоков»): бумага появляется с первой
строки заявки; правка строк переписывает акт; закрытие заявки на доске мимо
акта отменяет его с записью (списала заявка, второй путь к складу закрыт);
удаление заявки уносит акт. Выключается настройкой `auto_act`; без блока
бланков не работает вовсе.
"""

import itertools

import pytest

from tests.conftest import API

DEALS = f"{API}/deals"
DOCS = f"{API}/documents"
STOCK = f"{API}/warehouse"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client):
    for key in ("documents", "warehouse", "orders"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    root_client.patch(f"{API}/settings", json={"values": {"auto_act": "1"}})


@pytest.fixture
def deal(root_client):
    client = root_client.post(f"{API}/clients", json={"name": f"Клиент {uniq()}"}).json()
    r = root_client.post(f"{DEALS}", json={"title": f"Ремонт {uniq()}", "client_id": client["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def product(root_client, stock="10"):
    item = root_client.post(
        f"{STOCK}/products", json={"name": f"Деталь {uniq()}", "price": 500, "cost": 100}
    ).json()
    root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": stock, "kind": "in"})
    return item


def akty(root_client, deal_id: int) -> list[dict]:
    return [
        d for d in root_client.get(f"{DOCS}", params={"deal_id": deal_id, "kind": "act"}).json()["items"]
    ]


def stroki(root_client, act_id: int) -> list[dict]:
    return root_client.get(f"{DOCS}/acts/{act_id}").json()["lines"]


def stages(root_client) -> dict[str, str]:
    return {s["kind"]: s["key"] for s in root_client.get(f"{API}/pipeline/stages").json()["items"]}


def test_akt_poyavlyaetsya_so_strokoy_i_povtoryaet_zayavku(root_client, deal):
    assert akty(root_client, deal["id"]) == [], "заявка без строк — акта нет"
    item = product(root_client)
    line = root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "2"}).json()
    [act] = akty(root_client, deal["id"])
    assert act["status"] == "issued"
    assert [(s["product_id"], s["quantity_milli"]) for s in stroki(root_client, act["id"])] == [(item["id"], 2000)]

    svoya = root_client.post(f"{DEALS}/{deal['id']}/lines", json={"name": "Выезд", "quantity": "1", "price": 700}).json()
    assert [s["name"] for s in stroki(root_client, act["id"])] == [item["name"], "Выезд"]

    root_client.patch(f"{DEALS}/{deal['id']}/lines/{line['id']}", json={"quantity": "3"})
    assert [s["quantity_milli"] for s in stroki(root_client, act["id"])] == [3000, 1000]

    root_client.delete(f"{DEALS}/{deal['id']}/lines/{line['id']}")
    root_client.delete(f"{DEALS}/{deal['id']}/lines/{svoya['id']}")
    assert akty(root_client, deal["id"]) == [], "строк не осталось — акт ушёл"


def test_zakrytie_zayavki_na_doske_otmenyaet_avto_akt(root_client, deal):
    item = product(root_client)
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    [act] = akty(root_client, deal["id"])
    r = root_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stages(root_client)["won"]})
    assert r.status_code == 200, r.text
    posle = root_client.get(f"{DOCS}/acts/{act['id']}").json()
    assert posle["status"] == "cancelled", "заявка списала сама, акт больше не проводится"
    ostatok = root_client.get(f"{STOCK}/products/{item['id']}").json()["stock_milli"]
    assert ostatok == 9000, "списание одно — заявкой"


def test_provedyonnyy_rukami_akt_dvigaet_zayavku_i_bolshe_ne_zerkalitsya(root_client, deal):
    item = product(root_client)
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    [act] = akty(root_client, deal["id"])
    r = root_client.post(f"{DOCS}/acts/{act['id']}/complete", json={"stage": stages(root_client)["won"]})
    assert r.status_code == 200, r.text
    assert root_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == stages(root_client)["won"]
    ostatok = root_client.get(f"{STOCK}/products/{item['id']}").json()["stock_milli"]
    assert ostatok == 9000, "списал акт, заявка второй раз не списывает"


def test_udalenie_zayavki_unosit_avto_akt(root_client, deal):
    item = product(root_client)
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    [act] = akty(root_client, deal["id"])
    assert root_client.delete(f"{DEALS}/{deal['id']}").status_code in (200, 204)
    assert root_client.get(f"{DOCS}/acts/{act['id']}").status_code == 404


def test_posle_otmeny_rukami_akt_ne_zavoditsya_snova(root_client, deal):
    item = product(root_client)
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    [act] = akty(root_client, deal["id"])
    assert root_client.post(f"{DOCS}/acts/{act['id']}/cancel", json={}).status_code == 200
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "2"})
    assert [a["status"] for a in akty(root_client, deal["id"])] == ["cancelled"]


def test_ruchnoy_akt_zerkalo_ne_trogaet(root_client, deal):
    root_client.patch(f"{API}/settings", json={"values": {"auto_act": "0"}})
    try:
        item = product(root_client)
        root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
        assert akty(root_client, deal["id"]) == [], "автоматика выключена"
        act = root_client.post(f"{DOCS}/acts", json={"deal_id": deal["id"]}).json()
        root_client.post(f"{DOCS}/acts/{act['id']}/lines", json={"name": "Диагностика", "quantity": "1", "price": 300})
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"auto_act": "1"}})
    root_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stages(root_client)["won"]})
    assert root_client.get(f"{DOCS}/acts/{act['id']}").json()["status"] == "issued", "ручной акт остался человеку"
