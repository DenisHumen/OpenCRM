"""Уведомления: кому, о чём, и что автор о своём действии не узнаёт дважды.

docs/21 §4. Адресат — тот, кто вправе видеть событие; автор — нет; текста в
базе нет, только вид и подстановки; список и счётчик — свои у каждого.
"""

import itertools

import pytest

from tests.conftest import API

ORDERS = f"{API}/orders"
STOCK = f"{API}/warehouse"
NTF = f"{API}/notifications"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client, manager_client):
    for key in ("documents", "warehouse", "orders", "waybills", "tasks"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    # Менеджеру по умолчанию не видны заказы и накладные — даём на время набора:
    # адресат уведомления определяется правом, и проверять это нужно на человеке с правом.
    role_id = manager_client.get(f"{API}/auth/me").json()["role_id"]
    bylo = root_client.get(f"{API}/roles/{role_id}").json()["permissions"]
    nuzhno = sorted(set(bylo) | {"orders.view", "waybills.view", "documents.view", "tasks.view"})
    root_client.patch(f"{API}/roles/{role_id}", json={"permissions": nuzhno})
    yield
    root_client.patch(f"{API}/roles/{role_id}", json={"permissions": bylo})


def _prochitat_vse(client):
    client.post(f"{NTF}/read", json={})


def _svezhie(client, kind: str) -> list[dict]:
    return [n for n in client.get(NTF).json()["items"] if n["kind"] == kind and not n["read"]]


def test_zakrytie_zakaza_uvedomlyaet_drugikh_a_ne_avtora(root_client, manager_client):
    _prochitat_vse(root_client); _prochitat_vse(manager_client)
    item = root_client.post(f"{STOCK}/products", json={"name": f"Деталь {uniq()}", "price": 500}).json()
    root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": "5", "kind": "in"})
    client = root_client.post(f"{API}/clients", json={"name": f"Клиент {uniq()}"}).json()
    order = root_client.post(f"{ORDERS}", json={"kind": "sales_order", "client_id": client["id"]}).json()
    root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200

    moi = _svezhie(root_client, "order_closed")
    assert moi == [], "автор действия о нём не уведомляется"
    chuzhie = _svezhie(manager_client, "order_closed")
    assert chuzhie, "менеджер с правом видеть заказы не узнал о закрытии"
    assert chuzhie[0]["params"]["number"] == order["number"]
    assert chuzhie[0]["link"] == f"/orders/{order['id']}"
    # Накладная по заказу тоже — и о черновике, и о проведении.
    assert _svezhie(manager_client, "auto_waybill"), "о заведённом самим черновике не сказали"
    assert _svezhie(manager_client, "waybill_posted"), "о проведённой накладной не сказали"


def test_schyotchik_i_prochtenie_svoi_u_kazhdogo(root_client, manager_client):
    _prochitat_vse(manager_client)
    assert manager_client.get(f"{NTF}/summary").json()["unread"] == 0
    task = root_client.post(f"{API}/tasks", json={"title": "Перезвонить", "assignee_id": manager_client.get(f"{API}/auth/me").json()["id"]}).json()
    assert task["id"]
    assert manager_client.get(f"{NTF}/summary").json()["unread"] == 1
    [n] = _svezhie(manager_client, "task_assigned")
    assert n["params"]["title"] == "Перезвонить"

    # Чужое прочитать нельзя: root отметит все свои — у менеджера останется.
    root_client.post(f"{NTF}/read", json={"ids": [n["id"]]})
    assert manager_client.get(f"{NTF}/summary").json()["unread"] == 1
    assert manager_client.post(f"{NTF}/read", json={"ids": [n["id"]]}).json()["marked"] == 1
    assert manager_client.get(f"{NTF}/summary").json()["unread"] == 0


def test_bez_prava_videt_blok_uvedomleniya_net(root_client, manager_client):
    """Право отбирается при записи: кому блок закрыт, тому о нём не рассказывают."""
    _prochitat_vse(manager_client)
    me = manager_client.get(f"{API}/auth/me").json()
    role_id = me["role_id"]
    prava = root_client.get(f"{API}/roles/{role_id}").json()
    bylo = list(prava["permissions"])
    root_client.patch(f"{API}/roles/{role_id}", json={"permissions": [p for p in bylo if not p.startswith("orders.")]})
    try:
        item = root_client.post(f"{STOCK}/products", json={"name": f"Деталь {uniq()}", "price": 500}).json()
        root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": "5", "kind": "in"})
        order = root_client.post(f"{ORDERS}", json={"kind": "sales_order"}).json()
        root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
        assert root_client.post(f"{ORDERS}/{order['id']}/cancel").status_code == 200
        assert _svezhie(manager_client, "order_cancelled") == []
    finally:
        root_client.patch(f"{API}/roles/{role_id}", json={"permissions": bylo})
