"""Возвраты покупателя: бумага, склад, деньги, вложения, статистика.

Отмены проведения у заказа нет — решение владельца 05.09.2026: проведённый
заказ это свершившееся, назад дорога одна — возврат. Здесь проверяется, что
дорога эта ведёт ровно туда: товар возвращается один раз и не больше
отгруженного, деньги уходят минусом по доходной статье, к бумаге прикладываются
фото и видео, а выключенный блок молчит словами, а не движениями.
"""

import itertools
from datetime import date, timedelta

import pytest

from database.session import SessionLocal
from tests.conftest import API, png_bytes

ORDERS = f"{API}/orders"
RETURNS = f"{API}/returns"
STOCK = f"{API}/warehouse"
WB = f"{API}/waybills"
FINANCE = f"{API}/finance"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    # Финансы не включаем: отчёты ждут их выключенными; здесь они включаются
    # точечно и выключаются в `finally`.
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})


def tovar(root_client, stock="10", price=500):
    item = root_client.post(
        f"{STOCK}/products",
        json={"name": f"Товар возврата {uniq()}", "sku": f"RET-{uniq()}", "price": price, "cost": 100},
    ).json()
    if stock:
        root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "quantity": stock, "kind": "in"})
    return item


def klient(root_client) -> dict:
    return root_client.post(f"{API}/clients", json={"name": f"Покупатель возврата {uniq()}"}).json()


def ostatok(root_client, product_id) -> int:
    return root_client.get(f"{STOCK}/products/{product_id}").json()["stock_milli"]


def dvizheniya(root_client, product_id) -> list[str]:
    return [m["kind"] for m in root_client.get(f"{STOCK}/products/{product_id}/moves").json()["items"]]


def otgruzhennyy_zakaz(root_client, item, quantity="3", client=None, kind="sales_order") -> dict:
    telo = {"kind": kind}
    if client:
        telo["client_id"] = client["id"]
    order = root_client.post(ORDERS, json=telo).json()
    r = root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": quantity})
    assert r.status_code == 201, r.text
    zakryt = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert zakryt.status_code == 200, zakryt.text
    return root_client.get(f"{ORDERS}/{order['id']}").json()


def vozvrat(root_client, order) -> dict:
    r = root_client.post(f"{ORDERS}/{order['id']}/returns")
    assert r.status_code == 201, r.text
    return r.json()


def provesti(root_client, vozvrat_id, **telo):
    return root_client.post(f"{RETURNS}/{vozvrat_id}/post", json=telo)


def s_finansami(root_client):
    """Включить деньги на время проверки; выключает вызывающий в `finally`."""
    assert root_client.post(f"{API}/modules/finance", json={"enabled": True}).status_code == 200
    statya = root_client.post(
        f"{FINANCE}/categories", json={"name": f"Продажи {uniq()}", "direction": "income"}
    )
    assert statya.status_code == 201, statya.text
    return statya.json()


def bez_finansov(root_client):
    root_client.post(f"{API}/modules/finance", json={"enabled": False})


# --- заказ назад не откатывается ---------------------------------------------


def test_provedyonnyy_zakaz_nazad_ne_otkatyvaetsya(root_client):
    """Ручки отмены проведения нет: проведённый заказ остаётся проведённым, склад на месте."""
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="4")
    assert ostatok(root_client, item["id"]) == 6000

    otkaz = root_client.post(f"{ORDERS}/{order['id']}/revert", json={})
    # Адреса нет: 404, либо 405 от раздачи интерфейса, которая знает только GET.
    assert otkaz.status_code in (404, 405), otkaz.text
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["status"] == "closed"
    assert ostatok(root_client, item["id"]) == 6000


def test_vozvrat_tolko_po_provedyonnomu_zakazu_pokupatelya(root_client):
    item = tovar(root_client)
    otkrytyy = root_client.post(ORDERS, json={"kind": "sales_order"}).json()
    otkaz = root_client.post(f"{ORDERS}/{otkrytyy['id']}/returns")
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "order_not_closed"

    postavshchiku = otgruzhennyy_zakaz(root_client, item, quantity="2", kind="purchase_order")
    otkaz = root_client.post(f"{ORDERS}/{postavshchiku['id']}/returns")
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "not_a_sales_order"
    assert "returns" not in postavshchiku, "у заказа поставщику ключа возвратов быть не должно"


# --- бумага, склад, накладная -------------------------------------------------


def test_vozvrat_predzapolnen_i_vozvrashchaet_tovar_prikhodnoy(root_client):
    """Черновик повторяет отгруженное; проведение выписывает приходную и возвращает товар один раз."""
    pokupatel = klient(root_client)
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="3", client=pokupatel)
    assert ostatok(root_client, item["id"]) == 7000

    v = vozvrat(root_client, order)
    assert v["status"] == "draft" and v["pravitsya"] is True
    assert v["order_id"] == order["id"] and v["order_number"] == order["number"]
    assert v["client_id"] == pokupatel["id"] and v["client_name"] == pokupatel["name"]
    assert [(l["product_id"], l["quantity_milli"], l["price"]) for l in v["lines"]] == [(item["id"], 3000, 500)]
    assert v["refund"] == 1500, "сумма по умолчанию — цена возвращаемых строк"

    popravlen = root_client.patch(
        f"{RETURNS}/{v['id']}", json={"refund": 1000, "note": "Царапина на корпусе"}
    )
    assert popravlen.status_code == 200, popravlen.text
    assert popravlen.json()["refund"] == 1000 and popravlen.json()["note"] == "Царапина на корпусе"

    otvet = provesti(root_client, v["id"])
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "closed" and otvet.json()["pravitsya"] is False
    assert ostatok(root_client, item["id"]) == 10000, "товар не вернулся"
    assert dvizheniya(root_client, item["id"]).count("return") == 1, "возврат записан не возвратом или дважды"

    kartochka = root_client.get(f"{RETURNS}/{v['id']}").json()
    [prihod] = kartochka["waybills"]
    assert prihod["kind"] == "waybill_in" and prihod["status"] == "issued"
    assert any(f"received by waybill {prihod['number']}" in (e["note"] or "") for e in kartochka["events"])

    zakaz = root_client.get(f"{ORDERS}/{order['id']}").json()
    assert zakaz["status"] == "closed", "возврат не переписывает историю заказа"
    assert [(r["id"], r["status"], r["refund"]) for r in zakaz["returns"]] == [(v["id"], "closed", 1000)]

    # Лента клиента: одна строка про возврат.
    zapisi = root_client.get(f"{API}/clients/{pokupatel['id']}/notes", params={"per_page": 50}).json()["items"]
    assert any(f"Return {v['number']} for order {order['number']}" in (z.get("body") or "") for z in zapisi), zapisi


def test_provedyonnyy_vozvrat_ne_pravitsya(root_client):
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="2")
    v = vozvrat(root_client, order)
    assert provesti(root_client, v["id"]).status_code == 200
    for otvet in (
        root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 1}),
        root_client.post(f"{RETURNS}/{v['id']}/lines", json={"product_id": item["id"], "quantity": "1"}),
        provesti(root_client, v["id"]),
        root_client.post(f"{RETURNS}/{v['id']}/cancel", json={}),
    ):
        assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "return_not_draft", otvet.text
    udalenie = root_client.delete(f"{RETURNS}/{v['id']}")
    assert udalenie.status_code == 422 and udalenie.json()["error"]["code"] == "document_in_use"


def test_vozvrat_ne_bolshe_otgruzhennogo(root_client):
    """Вернуть можно то, что отгрузили, и ровно один раз — по всем возвратам заказа вместе."""
    item = tovar(root_client)
    chuzhoy = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="3")

    pervyy = vozvrat(root_client, order)
    [stroka] = pervyy["lines"]
    bolshe = root_client.patch(f"{RETURNS}/{pervyy['id']}/lines/{stroka['id']}", json={"quantity": "4"})
    assert bolshe.status_code == 200
    otkaz = provesti(root_client, pervyy["id"])
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "return_exceeds_shipped", otkaz.text
    assert ostatok(root_client, item["id"]) == 7000

    chuzhaya = root_client.post(f"{RETURNS}/{pervyy['id']}/lines", json={"product_id": chuzhoy["id"], "quantity": "1"})
    assert chuzhaya.status_code == 422 and chuzhaya.json()["error"]["code"] == "product_not_in_order"

    assert root_client.patch(f"{RETURNS}/{pervyy['id']}/lines/{stroka['id']}", json={"quantity": "2"}).status_code == 200
    assert provesti(root_client, pervyy["id"]).status_code == 200
    assert ostatok(root_client, item["id"]) == 9000

    vtoroy = vozvrat(root_client, order)
    assert [l["quantity_milli"] for l in vtoroy["lines"]] == [1000], "второй возврат предлагает только остаток"
    kartochka = root_client.get(f"{RETURNS}/{vtoroy['id']}").json()
    assert kartochka["order_lines"] == [
        {"product_id": item["id"], "name": item["name"], "price": 500, "max_milli": 1000}
    ]
    assert provesti(root_client, vtoroy["id"]).status_code == 200
    assert ostatok(root_client, item["id"]) == 10000

    nechego = root_client.post(f"{ORDERS}/{order['id']}/returns")
    assert nechego.status_code == 422 and nechego.json()["error"]["code"] == "nothing_to_return"


def test_storno_nakladnoy_zakaza_schitaetsya_vozvrashchyonnym(root_client):
    """Сторно накладной заказа уже вернуло товар — возврат второй раз его не возвращает."""
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="2")
    [wb] = order["waybills"]
    storno = root_client.post(f"{WB}/{wb['id']}/reverse", json={}).json()
    assert root_client.post(f"{WB}/{storno['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item["id"]) == 10000

    nechego = root_client.post(f"{ORDERS}/{order['id']}/returns")
    assert nechego.status_code == 422 and nechego.json()["error"]["code"] == "nothing_to_return"
    assert ostatok(root_client, item["id"]) == 10000


def test_bez_nakladnykh_vozvrat_pishet_dvizheniya_sam(root_client):
    assert root_client.post(f"{API}/modules/waybills", json={"enabled": False}).status_code == 200
    try:
        item = tovar(root_client)
        order = otgruzhennyy_zakaz(root_client, item, quantity="2")
        assert ostatok(root_client, item["id"]) == 8000
        v = vozvrat(root_client, order)
        assert provesti(root_client, v["id"]).status_code == 200
        assert ostatok(root_client, item["id"]) == 10000
        assert dvizheniya(root_client, item["id"]).count("return") == 1
        assert "waybills" not in root_client.get(f"{RETURNS}/{v['id']}").json()
    finally:
        root_client.post(f"{API}/modules/waybills", json={"enabled": True})


def test_pri_vyklyuchennom_sklade_vozvrat_ne_dvigaet_sklad(root_client):
    """Склад выключен — движений нет, и история говорит об этом словами."""
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="3")
    assert ostatok(root_client, item["id"]) == 7000
    v = vozvrat(root_client, order)
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = provesti(root_client, v["id"])
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    assert ostatok(root_client, item["id"]) == 7000, "возврат вернул товар при выключенном складе"
    istoriya = root_client.get(f"{RETURNS}/{v['id']}").json()["events"]
    assert any("warehouse module off" in (e["note"] or "") for e in istoriya)


# --- деньги -------------------------------------------------------------------


def test_vozvrat_otdayot_dengi_minusom_po_dokhodnoy_state(root_client):
    """Деньги уходят минусом по доходной статье и висят на возврате, а не на заказе."""
    statya = s_finansami(root_client)
    try:
        item = tovar(root_client)
        order = otgruzhennyy_zakaz(root_client, item, quantity="2")
        oplata = root_client.post(
            f"{FINANCE}/payments", json={"category_id": statya["id"], "amount": 1000, "document_id": order["id"]}
        )
        assert oplata.status_code == 201, oplata.text

        v = vozvrat(root_client, order)
        root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 700})
        bez_stati = provesti(root_client, v["id"])
        assert bez_stati.status_code == 422 and bez_stati.json()["error"]["code"] == "refund_needs_category", bez_stati.text
        assert root_client.get(f"{RETURNS}/{v['id']}").json()["status"] == "draft", "отказ денег не откатил проведение"
        assert ostatok(root_client, item["id"]) == 8000

        root_client.patch(f"{RETURNS}/{v['id']}", json={"category_id": statya["id"]})
        assert provesti(root_client, v["id"]).status_code == 200

        from database.repositories import finance as finance_repo

        with SessionLocal() as db:
            po_vozvratu = finance_repo.operations_of_document(db, v["id"])
            assert [(o.amount_minor, o.category_id) for o in po_vozvratu] == [(-700, statya["id"])]
            po_zakazu = finance_repo.operations_of_document(db, order["id"])
            assert [o.amount_minor for o in po_zakazu] == [1000], "возврат тронул деньги заказа"
        dengi = root_client.get(f"{FINANCE}/documents/{order['id']}/money").json()
        assert dengi["received"] == 1000
    finally:
        bez_finansov(root_client)


def test_bez_bloka_deneg_vozvrat_zapisyvaet_summu_na_bumage(root_client):
    bez_finansov(root_client)
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 300})
    assert provesti(root_client, v["id"]).status_code == 200
    assert root_client.get(f"{RETURNS}/{v['id']}").json()["refund"] == 300


def test_summa_vozvrata_tselaya_i_ne_otritsatelnaya(root_client):
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    for plokho, kod in ((-1, "negative_money"), ("12.5", "bad_money")):
        otvet = root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": plokho})
        assert otvet.status_code == 422 and otvet.json()["error"]["code"] == kod, otvet.text
    assert root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 0}).status_code == 200, "обмен без денег законен"


# --- вложения -----------------------------------------------------------------


def test_k_vozvratu_prikladyvayutsya_foto_i_video_a_dokumenty_net(root_client):
    from core.services import return_service

    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)

    foto = root_client.post(
        f"{RETURNS}/{v['id']}/files", files={"file": ("tsarapina.png", png_bytes(), "image/png")}
    )
    assert foto.status_code == 201, foto.text
    zapis = foto.json()
    assert zapis["mime"] == "image/png" and zapis["document_id"] == v["id"]

    skachat = root_client.get(zapis["download_url"])
    assert skachat.status_code == 200 and skachat.headers["content-type"].startswith("image/png")
    assert skachat.headers["content-disposition"].startswith("inline")

    dogovor = root_client.post(
        f"{RETURNS}/{v['id']}/files", files={"file": ("dogovor.pdf", b"%PDF-1.4 x", "application/pdf")}
    )
    assert dogovor.status_code == 422 and dogovor.json()["error"]["code"] == "file_type_not_allowed"
    podmena = root_client.post(
        f"{RETURNS}/{v['id']}/files", files={"file": ("hack.png", b"MZ not a picture", "image/png")}
    )
    assert podmena.status_code == 422 and podmena.json()["error"]["code"] == "file_content_mismatch"

    kartochka = root_client.get(f"{RETURNS}/{v['id']}").json()
    assert [f["id"] for f in kartochka["files"]] == [zapis["id"]]

    with SessionLocal() as db:
        put = return_service.file_path_on_disk(return_service.get_file(db, v["id"], zapis["id"]))
    assert put.exists()
    assert root_client.delete(f"{RETURNS}/{v['id']}/files/{zapis['id']}").status_code == 200
    assert not put.exists(), "файл остался на диске после удаления"

    assert provesti(root_client, v["id"]).status_code == 200
    posle = root_client.post(
        f"{RETURNS}/{v['id']}/files", files={"file": ("pozdno.png", png_bytes(), "image/png")}
    )
    assert posle.status_code == 422 and posle.json()["error"]["code"] == "return_not_draft"


def test_chernovik_udalyaetsya_vmeste_s_vlozheniyami(root_client):
    from core.services import return_service

    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    zapis = root_client.post(
        f"{RETURNS}/{v['id']}/files", files={"file": ("foto.png", png_bytes(), "image/png")}
    ).json()
    with SessionLocal() as db:
        put = return_service.file_path_on_disk(return_service.get_file(db, v["id"], zapis["id"]))
    assert put.exists()
    assert root_client.delete(f"{RETURNS}/{v['id']}").status_code == 200
    assert root_client.get(f"{RETURNS}/{v['id']}").status_code == 404
    assert not put.exists()
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["returns"] == []


def test_chernovik_otmenyaetsya(root_client):
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    otmena = root_client.post(f"{RETURNS}/{v['id']}/cancel", json={"note": "передумал"})
    assert otmena.status_code == 200 and otmena.json()["status"] == "cancelled"
    assert provesti(root_client, v["id"]).status_code == 422
    assert ostatok(root_client, item["id"]) == 9000
    # Отменённый в счёт не идёт: новый возврат снова предлагает всё отгруженное.
    assert [l["quantity_milli"] for l in vozvrat(root_client, order)["lines"]] == [1000]


# --- список, права, блок, статистика -----------------------------------------


def test_spisok_i_schyot_po_sostoyaniyam(root_client):
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="2")
    v = vozvrat(root_client, order)
    spisok = root_client.get(RETURNS, params={"order_id": order["id"]}).json()
    assert [r["id"] for r in spisok["items"]] == [v["id"]]
    assert spisok["counts"] == {"draft": 1}
    assert spisok["items"][0]["order_number"] == order["number"]
    otkaz = root_client.get(RETURNS, params={"sort": "random"})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "unknown_sort"


def test_provesti_vozvrat_pravo_otdelnoye(manager_client, root_client):
    """Заполнять и проводить — разные полномочия, как у отгрузки."""
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    denied = manager_client.post(f"{RETURNS}/{v['id']}/post", json={})
    assert denied.status_code == 403, denied.text
    assert ostatok(root_client, item["id"]) == 9000


def test_vyklyuchennyy_blok_zakazov_unosit_vozvraty(root_client):
    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    assert root_client.post(f"{API}/modules/orders", json={"enabled": False}).status_code == 200
    try:
        for otvet in (root_client.get(RETURNS), root_client.get(f"{RETURNS}/{v['id']}"), root_client.get(f"{RETURNS}/stats")):
            assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "module_disabled", otvet.text
    finally:
        root_client.post(f"{API}/modules/orders", json={"enabled": True})


def test_statistika_vozvratov(root_client):
    """Своя статистика: сколько, на сколько, доля от отгрузок, по месяцам, что возвращают."""
    # Дюжина, а не две: список «что возвращают чаще» — десятка по количеству, и
    # возвраты соседних файлов (по две-пять штук) в общей базе вытесняли наш товар.
    item = tovar(root_client, stock="20")
    order = otgruzhennyy_zakaz(root_client, item, quantity="12")
    v = vozvrat(root_client, order)
    root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 650})
    assert provesti(root_client, v["id"]).status_code == 200

    segodnya = date.today()
    otvet = root_client.get(
        f"{RETURNS}/stats",
        params={"from": (segodnya - timedelta(days=1)).isoformat(), "to": (segodnya + timedelta(days=1)).isoformat()},
    )
    assert otvet.status_code == 200, otvet.text
    svodka = otvet.json()
    assert svodka["count"] >= 1 and svodka["refund_amount"] >= 650
    assert svodka["shipped_count"] >= svodka["count"], "отгрузок не меньше, чем возвратов"
    assert 0 < svodka["share"] <= 100, "доля — в процентах, как у отчётов"
    assert svodka["avg_refund"] is not None and svodka["currency"]
    assert sum(m["count"] for m in svodka["months"]) == svodka["count"]
    assert sum(m["refund_amount"] for m in svodka["months"]) == svodka["refund_amount"]
    nash = [p for p in svodka["products"] if p["product_id"] == item["id"]]
    assert nash and nash[0]["quantity_milli"] == 12000 and nash[0]["returns"] == 1


def test_zhurnal_i_zapis_v_karte_tem(root_client):
    """Проведение — строка в журнале действий; возврат живёт в теме заказов."""
    from core.live import topics
    from database.models import Document

    item = tovar(root_client)
    order = otgruzhennyy_zakaz(root_client, item, quantity="1")
    v = vozvrat(root_client, order)
    assert provesti(root_client, v["id"]).status_code == 200

    zhurnal = root_client.get(f"{API}/audit", params={"per_page": 50}).json()["items"]
    assert any(z["action"] == "return.posted" and z["entity_label"] == v["number"] for z in zhurnal), zhurnal[:3]

    with SessionLocal() as db:
        assert topics._po_vidu_blanka(db.get(Document, v["id"])) is topics.T_ORDERS


def test_dengi_zakaza_i_vozvrata_znayut_vozvrashchennoe(root_client):
    """Карточка заказа: «получено» не трогается, возвращённое — отдельным числом.
    У самого возврата бумага — сумма к возврату, и он «рассчитан», когда деньги
    отданы; прежде ручка денег показывала по возврату долг клиента вдвое."""
    statya = s_finansami(root_client)
    try:
        item = tovar(root_client)
        order = otgruzhennyy_zakaz(root_client, item, quantity="2")
        root_client.post(
            f"{FINANCE}/payments", json={"category_id": statya["id"], "amount": 1000, "document_id": order["id"]}
        )
        v = vozvrat(root_client, order)
        root_client.patch(f"{RETURNS}/{v['id']}", json={"refund": 700, "category_id": statya["id"]})
        assert provesti(root_client, v["id"]).status_code == 200

        po_zakazu = root_client.get(f"{FINANCE}/documents/{order['id']}/money").json()
        assert po_zakazu["received"] == 1000 and po_zakazu["refunded"] == 700
        assert po_zakazu["paid"] is True
        po_vozvratu = root_client.get(f"{FINANCE}/documents/{v['id']}/money").json()
        assert po_vozvratu["total"] == 700 and po_vozvratu["refunded"] == 700
        assert po_vozvratu["due"] == 0 and po_vozvratu["paid"] is True
    finally:
        bez_finansov(root_client)
