"""Номер заявки в бумагах и на складе — по той же видимости, что её карточка.

Разбор 28.09.2026: акт, квитанция, заказ, накладная и движение склада принимали
любой `deal_id`. Акт по чужой заявке двигал её по воронке, заказ уводил её бронь,
а ответы и списки по `deal_id` отдавали её название и себестоимость — мимо
`deals.view_others`.
"""

import pytest

from tests.conftest import API
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры

BLOKI = ("documents", "warehouse", "orders", "waybills")


@pytest.fixture
def bloki_vklyucheny(root_client):
    bylo = {m["key"]: m["enabled"] for m in root_client.get(f"{API}/modules").json()["items"]}
    for klyuch in BLOKI:
        assert root_client.post(f"{API}/modules/{klyuch}", json={"enabled": True}).status_code == 200
    yield
    for klyuch in reversed(BLOKI):
        root_client.post(f"{API}/modules/{klyuch}", json={"enabled": bylo[klyuch]})


def test_chuzhaya_zayavka_ne_prinimaetsya_bumagami_i_skladom(
    root_client, bloki_vklyucheny, role_maker, staff_maker  # noqa: F811
):
    rol = role_maker("Бумаги без чужих заявок", [
        "clients.view", "clients.create", "deals.view", "deals.create",
        "documents.view", "documents.create", "documents.edit", "documents.issue",
        "orders.view", "orders.create", "waybills.view", "waybills.create",
        "warehouse.view", "warehouse.create",
    ])
    menedzher = staff_maker("bumagi-chuzhie@test.local", rol["id"])
    klient = root_client.post(f"{API}/clients", json={"name": "Чужой заказчик"}).json()
    chuzhaya = root_client.post(f"{API}/deals", json={"title": "Чужая сделка", "client_id": klient["id"]}).json()
    tovar = root_client.post(f"{API}/warehouse/products", json={"name": "Деталь чужой", "sku": "CHUZH-1"}).json()

    otkazy = {
        "акт": menedzher.post(f"{API}/documents/acts", json={"deal_id": chuzhaya["id"]}),
        "квитанция": menedzher.post(f"{API}/documents", json={"deal_id": chuzhaya["id"], "item": "Вещь"}),
        "заказ": menedzher.post(f"{API}/orders", json={"kind": "sales_order", "deal_id": chuzhaya["id"]}),
        "накладная": menedzher.post(f"{API}/waybills", json={"kind": "waybill_out", "deal_id": chuzhaya["id"]}),
        "движение": menedzher.post(
            f"{API}/warehouse/moves",
            json={"product_id": tovar["id"], "kind": "writeoff", "quantity": "1", "deal_id": chuzhaya["id"]},
        ),
        "список бумаг": menedzher.get(f"{API}/documents", params={"deal_id": chuzhaya["id"]}),
        "список заказов": menedzher.get(f"{API}/orders", params={"deal_id": chuzhaya["id"]}),
        "список накладных": menedzher.get(f"{API}/waybills", params={"deal_id": chuzhaya["id"]}),
        "движения": menedzher.get(f"{API}/warehouse/moves", params={"deal_id": chuzhaya["id"]}),
    }
    for chto, otvet in otkazy.items():
        assert otvet.status_code == 403, f"{chto}: {otvet.status_code} {otvet.text[:200]}"
        assert otvet.json()["error"]["code"] == "permission_denied", chto

    svoy = menedzher.post(f"{API}/clients", json={"name": "Свой заказчик"}).json()
    svoya = menedzher.post(f"{API}/deals", json={"title": "Своя сделка", "client_id": svoy["id"]}).json()
    akt = menedzher.post(f"{API}/documents/acts", json={"deal_id": svoya["id"]})
    assert akt.status_code == 201, akt.text
