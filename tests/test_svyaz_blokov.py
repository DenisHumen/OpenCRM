"""Связи блоков: статус в одном блоке меняет положение дел в других.

Проверка владельца 05.09.2026 — «если где-то заказ проводится, его статус
должен меняться в других модулях; ничего не должно зависать». Зонд по всем
путям нашёл шесть мест, где бумага зависала или склад двигался дважды; здесь
каждое закрыто своей проверкой.
"""

import pytest

from database.models import Document
from database.models.document import STATUS_ISSUED
from database.session import SessionLocal
from tests.conftest import API

ORDERS = f"{API}/orders"
STOCK = f"{API}/warehouse"
DEALS = f"{API}/deals"
DOCS = f"{API}/documents"
WB = f"{API}/waybills"


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    # Финансы не включаем: отчёты ждут их выключенными, а связям они не нужны.
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    root_client.patch(f"{API}/settings", json={"values": {"auto_waybill": "1", "auto_act": "1"}})


def tovar(root_client, name="Деталь", stock="10"):
    item = root_client.post(f"{STOCK}/products", json={"name": name, "unit": "pcs", "price": 500, "cost": 100}).json()
    root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": stock, "kind": "in"})
    return item


def ostatok(root_client, product_id) -> int:
    return root_client.get(f"{STOCK}/products/{product_id}").json()["stock_milli"]


def zakaz_s_tovarom(root_client, item, quantity="2"):
    order = root_client.post(ORDERS, json={"kind": "sales_order"}).json()
    r = root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": quantity})
    assert r.status_code == 201, r.text
    return root_client.get(f"{ORDERS}/{order['id']}").json()


def nakladnaya_zakaza(root_client, order) -> dict:
    [wb] = order["waybills"]
    return root_client.get(f"{WB}/{wb['id']}").json()


def stages(root_client) -> dict[str, str]:
    return {s["kind"]: s["key"] for s in root_client.get(f"{API}/pipeline/stages").json()["items"]}


def test_nakladnaya_provedyonnaya_rukami_zakryvaet_zakaz(root_client):
    """Д1. Товар уехал накладной — заказ закрыт, а не «принято» навсегда."""
    item = tovar(root_client)
    order = zakaz_s_tovarom(root_client, item)
    wb = nakladnaya_zakaza(root_client, order)
    assert root_client.post(f"{WB}/{wb['id']}/post", json={}).status_code == 200

    posle = root_client.get(f"{ORDERS}/{order['id']}").json()
    assert posle["status"] == "closed", "накладная проведена — заказ закрыт"
    assert ostatok(root_client, item["id"]) == 8000, "склад двинула накладная, и ровно один раз"
    zapisi = [e for e in posle["events"] if e["to_status"] == "closed"]
    assert zapisi and f"shipped by waybill {wb['number']}" in zapisi[-1]["note"]

    # Назад — только возвратом: он выписывает приходную и возвращает товар.
    vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()
    assert root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item["id"]) == 10000


def test_otmena_zakaza_s_uekhavshim_tovarom_otkaz(root_client):
    """Д2. Заказ, по которому товар уехал, нельзя «отменить» — только откатить сторно."""
    item = tovar(root_client)
    order = zakaz_s_tovarom(root_client, item)
    wb = nakladnaya_zakaza(root_client, order)
    root_client.post(f"{WB}/{wb['id']}/post", json={})
    # Заказ из прежних времён: накладная проведена, а сам он остался открытым.
    with SessionLocal() as db:
        db.get(Document, order["id"]).status = STATUS_ISSUED
        db.commit()
    otkaz = root_client.post(f"{ORDERS}/{order['id']}/cancel", json={})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "already_shipped_by_waybill"
    assert ostatok(root_client, item["id"]) == 8000


def test_provedyonnoe_storno_snimaet_otgruzheno(root_client):
    """Д7. После проведённого сторно возвращать по заказу нечего: возврат не заводится."""
    item = tovar(root_client)
    order = zakaz_s_tovarom(root_client, item)
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    wb = nakladnaya_zakaza(root_client, root_client.get(f"{ORDERS}/{order['id']}").json())
    storno = root_client.post(f"{WB}/{wb['id']}/reverse", json={}).json()
    assert root_client.post(f"{WB}/{storno['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item["id"]) == 10000, "товар вернулся сторно"

    otkaz = root_client.post(f"{ORDERS}/{order['id']}/returns")
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "nothing_to_return", otkaz.text
    assert ostatok(root_client, item["id"]) == 10000, "второго возврата нет"


def test_akt_ne_spisyvaet_to_chto_otgruzil_zakaz(root_client):
    """Д4. Заявка: заказ отгрузил деталь, акт её второй раз не списывает."""
    item = tovar(root_client, "Колодки")
    client = root_client.post(f"{API}/clients", json={"name": "Клиент акта"}).json()
    deal = root_client.post(DEALS, json={"title": "Тормоза", "client_id": client["id"]}).json()
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"name": "Работа", "quantity": "1", "price": 700})
    [act] = root_client.get(DOCS, params={"deal_id": deal["id"], "kind": "act"}).json()["items"]

    zakaz = root_client.post(f"{DEALS}/{deal['id']}/order", json={}).json()
    assert zakaz["client_name"] == "Клиент акта", "Д5: заказ из заявки несёт клиента именем"
    assert root_client.post(f"{ORDERS}/{zakaz['id']}/close", json={}).status_code == 200
    assert ostatok(root_client, item["id"]) == 9000

    done = root_client.post(f"{DOCS}/acts/{act['id']}/complete", json={"stage": stages(root_client)["won"]})
    assert done.status_code == 200, done.text
    assert ostatok(root_client, item["id"]) == 9000, "деталь ушла заказом — акт её не трогает"
    assert root_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == stages(root_client)["won"]


def test_zayavka_s_otkrytym_zakazom_ne_udalyaetsya(root_client):
    """Д6. Заявку с открытым заказом в корзину не убрать — сначала заказ."""
    item = tovar(root_client, "Лампа")
    client = root_client.post(f"{API}/clients", json={"name": "Клиент лампы"}).json()
    deal = root_client.post(DEALS, json={"title": "Свет", "client_id": client["id"]}).json()
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    zakaz = root_client.post(f"{DEALS}/{deal['id']}/order", json={}).json()

    otkaz = root_client.delete(f"{DEALS}/{deal['id']}")
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "deal_has_open_orders"
    assert root_client.post(f"{ORDERS}/{zakaz['id']}/cancel", json={}).status_code == 200
    assert root_client.delete(f"{DEALS}/{deal['id']}").status_code in (200, 204)


def test_proigrannaya_zayavka_otmenyaet_otkrytye_zakazy(root_client):
    """П9. Проигрыш на доске отменяет открытый заказ заявки — отгружать нечего."""
    item = tovar(root_client, "Ремень")
    client = root_client.post(f"{API}/clients", json={"name": "Клиент проигрыша"}).json()
    deal = root_client.post(DEALS, json={"title": "Ремень", "client_id": client["id"]}).json()
    root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    zakaz = root_client.post(f"{DEALS}/{deal['id']}/order", json={}).json()

    m = root_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stages(root_client)["lost"]})
    assert m.status_code == 200, m.text
    posle = root_client.get(f"{ORDERS}/{zakaz['id']}").json()
    assert posle["status"] == "cancelled"
    assert posle["waybills"] == [], "авто-черновик ушёл вместе с заказом"
    assert any("deal lost" in (e.get("note") or "") for e in posle["events"])
    assert ostatok(root_client, item["id"]) == 10000


def test_ruchnoy_akt_zakrytoy_zayavki_provoditsya(root_client):
    """П17. Акт, заведённый руками, проводится и после закрытия заявки на доске: этап не трогает."""
    item = tovar(root_client, "Свеча")
    client = root_client.post(f"{API}/clients", json={"name": "Клиент акта 2"}).json()
    deal = root_client.post(DEALS, json={"title": "ТО", "client_id": client["id"]}).json()
    root_client.patch(f"{API}/settings", json={"values": {"auto_act": "0"}})
    try:
        root_client.post(f"{DEALS}/{deal['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
        act = root_client.post(DOCS + "/acts", json={"deal_id": deal["id"]}).json()
        root_client.post(f"{DOCS}/acts/{act['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"auto_act": "1"}})
    won = stages(root_client)["won"]
    assert root_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": won}).status_code == 200
    assert ostatok(root_client, item["id"]) == 9000, "выигрыш списал деталь заявки"

    done = root_client.post(f"{DOCS}/acts/{act['id']}/complete", json={})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "closed"
    assert root_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == won, "этап остался"
    assert ostatok(root_client, item["id"]) == 9000, "деталь ушла выигрышем — акт её не списывает второй раз"


def test_sobran_ne_smotrit_na_razovye_pozitsii(root_client):
    """П16. Заказ с «упаковкой» становится собранным, когда собран товар."""
    item = tovar(root_client, "Масло")
    order = zakaz_s_tovarom(root_client, item, quantity="1")
    root_client.post(f"{ORDERS}/{order['id']}/lines", json={"name": "Упаковка товара", "quantity": "1", "price": 100})
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["assembled"] is False
    p = root_client.post(f"{ORDERS}/{order['id']}/pick", json={"code": item["sku"]})
    assert p.status_code == 200, p.text
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["assembled"] is True


def test_otgruzka_v_lente_klienta_odnoy_strokoy(root_client):
    """П2/П30. Отгрузка — строка в ленте клиента про заказ, без строки на каждую позицию."""
    item = tovar(root_client, "Фильтр")
    item2 = tovar(root_client, "Лампа")
    client = root_client.post(f"{API}/clients", json={"name": "Клиент ленты"}).json()
    order = root_client.post(ORDERS, json={"kind": "sales_order", "client_id": client["id"]}).json()
    for it in (item, item2):
        root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": it["id"], "quantity": "1"})
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    zapisi = root_client.get(f"{API}/clients/{client['id']}/notes", params={"per_page": 50}).json()["items"]
    teksty = [z.get("text") or z.get("body") or "" for z in zapisi]
    assert any(f"Order {order['number']} shipped" in t for t in teksty), teksty
    assert not [t for t in teksty if t.startswith("Stock")], "по строке на позицию — лишнее"


def test_publichnaya_stranitsa_ne_otkryvaet_zakaz(root_client):
    """П22. Снаружи по номеру открываются только квитанция и акт: заказ — «нет такого»."""
    from fastapi.testclient import TestClient

    from web.main import app

    item = tovar(root_client, "Гайка")
    order = zakaz_s_tovarom(root_client, item)
    anon = TestClient(app)
    assert anon.get(f"/d/{order['number']}").status_code == 404
    client = root_client.post(f"{API}/clients", json={"name": "Клиент снаружи"}).json()
    kv = root_client.post(DOCS, json={"kind": "intake", "client_id": client["id"], "item": "Ноутбук"}).json()
    assert anon.get(f"/d/{kv['number']}").status_code == 200
