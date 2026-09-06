"""API сайта магазина: ключи, области, каталог, наличие, лента, заказы, клиенты.

Разбор — `docs/16-api-sayta.md`. Что здесь стережётся и почему:

- наружу виден остаток складов типа `shop` и только их, и только тот склад,
  что назван в ключе — опечатка при выдаче ключа не открывает карантин;
- ключ не может больше блока: выключенный склад отвечает `module_disabled`
  раньше, чем «нет области»;
- повтор заказа с тем же `site_ref` — тот же заказ и 200, а не второй заказ;
- «доступно» — остаток зала минус ВСЯ бронь, и заказ на большее отвергается
  числами, а не фразой;
- истёкшая бронь перестаёт держать товар без единой строки уборки;
- регистрация отвечает одной формой и чужую карточку не переписывает.
"""

import pytest

from core.services import api_key_service
from database.session import SessionLocal
from tests.conftest import API

SITE = f"{API}/site"
STOCK = f"{API}/warehouse"
KEYS = f"{API}/settings/api-keys"
H = api_key_service.HEADER


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    yield


def _uniq():
    import itertools

    if not hasattr(_uniq, "schyot"):
        _uniq.schyot = itertools.count(1)
    return f"{next(_uniq.schyot):05d}"


@pytest.fixture
def sklady(root_client):
    """Фабрика складов с уборкой.

    Уборка обязательна: база в наборе общая, а соседи считают склады («выбор
    склада появляется со вторым», «последний склад не закрыть»). Остаток
    обнуляется корректировкой, потом склад закрывается штатно.
    """
    sozdany: list[int] = []

    def sozdat(kind: str = "shop", imya: str | None = None) -> dict:
        r = root_client.post(f"{API}/warehouses", json={"name": imya or f"Зал {_uniq()}", "kind": kind})
        assert r.status_code == 201, r.text
        sozdany.append(r.json()["id"])
        return r.json()

    yield sozdat
    from database.models import StockMove
    from database.repositories import warehouses as places_repo

    with SessionLocal() as db:
        for sklad_id in sozdany:
            for product_id, ostatok in places_repo.nonzero_stock(db, sklad_id):
                db.add(StockMove(product_id=product_id, warehouse_id=sklad_id, kind="adjust", quantity_milli=-ostatok, comment="teardown"))
        db.commit()
    for sklad_id in sozdany:
        root_client.delete(f"{API}/warehouses/{sklad_id}")


@pytest.fixture
def shop(sklady):
    """Торговый зал: свой склад типа `shop` на каждую проверку."""
    return sklady("shop")


def product(root_client, warehouse_id=None, stock="10", price=500, service=False, sku=None):
    item = root_client.post(
        f"{STOCK}/products",
        json={"name": f"Товар {_uniq()}", "sku": sku or f"SITE-{_uniq()}", "price": price, "is_service": service},
    ).json()
    if stock and not service:
        r = root_client.post(
            f"{STOCK}/moves",
            json={"product_id": item["id"], "kind": "in", "quantity": stock, "warehouse_id": warehouse_id},
        )
        assert r.status_code == 201, r.text
    return item


def make_key(root_client, scopes, warehouse_id=None, **extra):
    r = root_client.post(
        KEYS, json={"name": f"ключ {_uniq()}", "scopes": scopes, "warehouse_id": warehouse_id, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


ALL = ["catalog.read", "stock.read", "orders.write", "orders.read", "customers.write", "leads.write"]


# --- ключи ---------------------------------------------------------------------


def test_klyuch_pokazyvaetsya_odin_raz_i_v_baze_tolko_otpechatok(root_client, shop):
    vydan = make_key(root_client, ["catalog.read"])
    assert len(vydan["key"]) > 30 and vydan["prefix"] == vydan["key"][:8]
    spisok = root_client.get(KEYS).json()
    moy = next(k for k in spisok["items"] if k["id"] == vydan["id"])
    assert "key" not in moy and moy["state"] == "active" and moy["scopes"] == ["catalog.read"]
    assert spisok["alive"] >= 1
    with SessionLocal() as db:
        from database.repositories import api_keys as keys_repo

        row = keys_repo.get(db, vydan["id"])
        assert row.token_hash != vydan["key"] and len(row.token_hash) == 64


def test_bez_klyucha_i_s_chuzhim_klyuchom_otkaz_odin(root_client, shop):
    assert root_client.get(f"{SITE}/catalog").status_code == 401
    r = root_client.get(f"{SITE}/catalog", headers={H: "definitely-not-a-key"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_api_key"
    # Байты вне ASCII в заголовке: Starlette читает их как latin-1, и до
    # хэширования такая строка доходить не должна.
    r = root_client.get(f"{SITE}/catalog", headers={H.encode("ascii"): "ключ".encode("utf-8")})
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_api_key"


def test_stock_read_trebuet_magazinnyy_sklad(root_client, sklady):
    podsobka = sklady("stock", f"Подсобка {_uniq()}")
    assert podsobka["kind"] == "stock"
    r = root_client.post(KEYS, json={"name": "x", "scopes": ["stock.read"], "warehouse_id": podsobka["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warehouse_not_shop"
    r = root_client.post(KEYS, json={"name": "x", "scopes": ["stock.read"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warehouse_required"
    r = root_client.post(KEYS, json={"name": "x", "scopes": ["catalog.write"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_scope"


def test_oblast_i_blok_proveryayutsya_v_pravilnom_poryadke(root_client, shop):
    tolko_katalog = make_key(root_client, ["catalog.read"], shop["id"])["key"]
    r = root_client.get(f"{SITE}/stock?id=1", headers={H: tolko_katalog})
    assert r.status_code == 403 and r.json()["error"]["code"] == "scope_required"
    assert "stock.read" in r.json()["error"]["message"]

    polnyy = make_key(root_client, ALL, shop["id"])["key"]
    root_client.post(f"{API}/modules/orders", json={"enabled": False})
    try:
        r = root_client.post(f"{SITE}/orders", json={"site_ref": "x", "items": []}, headers={H: polnyy})
        assert r.status_code == 403 and r.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/orders", json={"enabled": True})


def test_otzyv_i_srok(root_client, shop):
    vydan = make_key(root_client, ["catalog.read"], shop["id"])
    assert root_client.get(f"{SITE}/catalog", headers={H: vydan["key"]}).status_code == 200
    root_client.post(f"{KEYS}/{vydan['id']}/revoke")
    r = root_client.get(f"{SITE}/catalog", headers={H: vydan["key"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "api_key_revoked"

    with SessionLocal() as db:
        from datetime import timedelta

        from core.utils import now_utc
        from database.repositories import api_keys as keys_repo

        prosrochen = make_key(root_client, ["catalog.read"], shop["id"])
        row = keys_repo.get(db, prosrochen["id"])
        row.expires_at = now_utc().replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    r = root_client.get(f"{SITE}/catalog", headers={H: prosrochen["key"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "api_key_expired"
    zhurnal = root_client.get(f"{API}/audit", params={"action": "apikey.revoked"}).json()["items"]
    assert any(e["action"] == "apikey.revoked" for e in zhurnal)


def test_rotatsiya_ostavlyaet_staryy_klyuch_zhit(root_client, shop):
    staryy = make_key(root_client, ["catalog.read"], shop["id"])
    r = root_client.post(f"{KEYS}/{staryy['id']}/rotate", json={"grace_hours": 24})
    assert r.status_code == 201, r.text
    novyy = r.json()
    assert novyy["id"] != staryy["id"] and novyy["scopes"] == ["catalog.read"]
    assert root_client.get(f"{SITE}/catalog", headers={H: novyy["key"]}).status_code == 200
    assert root_client.get(f"{SITE}/catalog", headers={H: staryy["key"]}).status_code == 200, "старый умер сразу"
    spisok = {k["id"]: k for k in root_client.get(KEYS).json()["items"]}
    assert spisok[staryy["id"]]["expires_at"] is not None


# --- каталог и наличие ---------------------------------------------------------


def test_sayt_vidit_tolko_zal_i_tolko_svoy(root_client, shop, sklady):
    """Тип склада плюс склад ключа — два независимых условия (§2)."""
    podsobka = sklady("stock", f"Подсобка {_uniq()}")
    v_zale = product(root_client, shop["id"], stock="3")
    v_podsobke = product(root_client, podsobka["id"], stock="7")
    usluga = product(root_client, service=True)
    bez_tseny = product(root_client, shop["id"], stock="1", price=None)
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]

    r = root_client.get(f"{SITE}/catalog?per_page=200", headers={H: key})
    assert r.status_code == 200 and r.headers.get("etag")
    ids = {i["id"] for i in r.json()["items"]}
    assert v_zale["id"] in ids and usluga["id"] in ids and bez_tseny["id"] in ids
    assert v_podsobke["id"] not in ids, "подсобка наружу не отдаётся никогда"
    karta = next(i for i in r.json()["items"] if i["id"] == bez_tseny["id"])
    assert karta["prices"] == [], "товар без цены остаётся на сайте, но без кнопки «купить»"
    assert r.json()["currency"]
    assert root_client.get(f"{SITE}/catalog?per_page=200", headers={H: key, "If-None-Match": r.headers["etag"]}).status_code == 304

    r = root_client.get(f"{SITE}/stock?id={v_zale['id']},{v_podsobke['id']},{usluga['id']}", headers={H: key})
    assert r.status_code == 200
    po_id = {i["id"]: i for i in r.json()["items"]}
    assert po_id[v_zale["id"]] == {"id": v_zale["id"], "sku": v_zale["sku"], "unit": "pcs", "state": "few", "available_milli": 3000}
    assert po_id[usluga["id"]]["state"] == "always" and "available_milli" not in po_id[usluga["id"]]
    assert v_podsobke["id"] not in po_id
    assert r.json()["ttl_sec"] == 60 and r.json()["as_of"].endswith("Z")

    # Режим bucket — числа нет ВОВСЕ, а не null.
    bucket = make_key(root_client, ["stock.read"], shop["id"], stock_mode="bucket", few_threshold_milli=2000)["key"]
    r = root_client.get(f"{SITE}/stock?sku={v_zale['sku']}", headers={H: bucket})
    assert r.json()["items"][0]["state"] == "many" and "available_milli" not in r.json()["items"][0]


def test_raspodannyy_tovar_kartochku_sohranyaet(root_client, shop):
    item = product(root_client, shop["id"], stock="2")
    root_client.post(f"{STOCK}/moves", json={"product_id": item["id"], "kind": "out", "quantity": "2", "warehouse_id": shop["id"]})
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    r = root_client.get(f"{SITE}/catalog/{item['id']}", headers={H: key})
    assert r.status_code == 200, "карточка обязана жить — иначе умирает адрес страницы"
    r = root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key})
    assert r.json()["items"][0]["state"] == "none"


def test_smena_tipa_sklada_zakryvaet_vitrinu_i_pishetsya_v_zhurnal(root_client, shop):
    item = product(root_client, shop["id"], stock="1")
    key = make_key(root_client, ALL, shop["id"])["key"]
    itog = root_client.get(f"{API}/warehouses/{shop['id']}/site").json()
    assert itog["published"] >= 1
    root_client.patch(f"{API}/warehouses/{shop['id']}", json={"kind": "defect"})
    r = root_client.get(f"{SITE}/catalog", headers={H: key})
    assert r.status_code == 409 and r.json()["error"]["code"] == "warehouse_not_serving"
    zhurnal = root_client.get(f"{API}/audit", params={"action": "warehouse.kind_changed"}).json()["items"]
    assert any(e["action"] == "warehouse.kind_changed" and e["value_after"] == "defect" for e in zhurnal)
    assert item["id"]


# --- лента изменений -----------------------------------------------------------


def test_lenta_vidit_pravku_dvizhenie_i_zakaz(root_client, shop):
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    item = product(root_client, shop["id"], stock="5")
    polnaya = root_client.get(f"{SITE}/changes?limit=200", headers={H: key}).json()
    assert item["id"] in {i["id"] for i in polnaya["items"]} and polnaya["has_more"] is False
    kursor = polnaya["next_since"]

    # Ничего не менялось — в ленте пусто (кроме перекрытия в десять секунд).
    tishina = root_client.get(f"{SITE}/changes?since={kursor}", headers={H: key}).json()
    assert isinstance(tishina["items"], list)

    root_client.patch(f"{STOCK}/products/{item['id']}", json={"site_description": "Матовая, 30 pin"})
    lenta = root_client.get(f"{SITE}/changes?since={kursor}", headers={H: key}).json()
    karta = next(i for i in lenta["items"] if i["id"] == item["id"])
    assert karta["description"] == "Матовая, 30 pin" and karta["stock"]["available_milli"] == 5000

    r = root_client.get(f"{SITE}/changes?since=не-курсор", headers={H: key})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_cursor"


def test_lenta_listaetsya_kursorom(root_client, shop):
    key = make_key(root_client, ALL, shop["id"])["key"]
    ids = {product(root_client, shop["id"], stock="1")["id"] for _ in range(3)}
    sobrano, since, krugov = set(), None, 0
    while True:
        adres = f"{SITE}/changes?limit=2" + (f"&since={since}" if since else "")
        stranitsa = root_client.get(adres, headers={H: key}).json()
        sobrano |= {i["id"] for i in stranitsa["items"]}
        since = stranitsa["next_since"]
        krugov += 1
        if not stranitsa["has_more"] or krugov > 50:
            break
    assert ids <= sobrano and krugov >= 2


# --- заказы --------------------------------------------------------------------


def _zakaz(root_client, key, site_ref, items, **extra):
    return root_client.post(f"{SITE}/orders", json={"site_ref": site_ref, "items": items, **extra}, headers={H: key})


def test_zakaz_zanimaet_tovar_a_povtor_vozvrashchaet_tot_zhe(root_client, shop):
    item = product(root_client, shop["id"], stock="5")
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    ref = f"web-{_uniq()}"
    r = _zakaz(root_client, key, ref, [{"sku": item["sku"], "quantity": "2"}], reserve_minutes=30)
    assert r.status_code == 201, r.text
    zakaz = r.json()
    assert zakaz["site_ref"] == ref and zakaz["status"] == "issued" and zakaz["total_minor"] == 1000
    assert zakaz["reserved_until"] and zakaz["lines"][0]["quantity_milli"] == 2000
    assert zakaz["number"]

    povtor = _zakaz(root_client, key, ref, [{"sku": item["sku"], "quantity": "2"}])
    assert povtor.status_code == 200, "повтор доставки — тот же заказ, не второй"
    assert povtor.json()["number"] == zakaz["number"]

    nalichie = root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]
    assert nalichie["available_milli"] == 3000, "бронь вычтена из «доступно»"
    assert root_client.get(f"{SITE}/orders/{ref}", headers={H: key}).json()["number"] == zakaz["number"]

    # Внутри CRM это обычный заказ покупателя со сроком брони.
    vnutri = root_client.get(f"{API}/orders", params={"search": zakaz["number"]}).json()["items"][0]
    assert vnutri["site_ref"] == ref and vnutri["reserved_until"] and vnutri["reserve_expired"] is False


def test_nehvatka_otvechaet_chislami_i_po_tovaru(root_client, shop):
    item = product(root_client, shop["id"], stock="6")
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    r = _zakaz(root_client, key, f"web-{_uniq()}", [{"id": item["id"], "quantity": "5"}, {"id": item["id"], "quantity": "5"}])
    assert r.status_code == 409, r.text
    oshibka = r.json()["error"]
    assert oshibka["code"] == "not_enough_stock"
    assert oshibka["details"]["items"] == [
        {"id": item["id"], "sku": item["sku"], "requested_milli": 10000, "available_milli": 6000}
    ]
    assert root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]["available_milli"] == 6000


def test_otkazy_zakaza(root_client, shop):
    item = product(root_client, shop["id"], stock="6")
    bez_tseny = product(root_client, shop["id"], stock="6", price=None)
    key = make_key(root_client, ALL, shop["id"], max_reserve_minutes=60)["key"]
    r = _zakaz(root_client, key, "", [{"id": item["id"], "quantity": "1"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "site_ref_required"
    r = _zakaz(root_client, key, f"web-{_uniq()}", [{"sku": "NO-SUCH-SKU", "quantity": "1"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "product_unknown"
    assert r.json()["error"]["details"]["items"] == [{"id": None, "sku": "NO-SUCH-SKU"}]
    r = _zakaz(root_client, key, f"web-{_uniq()}", [{"id": bez_tseny["id"], "quantity": "1"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "price_not_set"
    r = _zakaz(root_client, key, f"web-{_uniq()}", [{"id": item["id"], "quantity": "1.0001"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "quantity_too_precise"
    r = _zakaz(root_client, key, f"web-{_uniq()}", [{"id": item["id"], "quantity": "1"}], reserve_minutes=61)
    assert r.status_code == 422 and r.json()["error"]["code"] == "reserve_too_long"
    assert root_client.get(f"{SITE}/orders/no-such-ref", headers={H: key}).status_code == 404


def test_istyokshaya_bron_otpuskaet_tovar_sama(root_client, shop):
    """Ни таймера, ни уборки: срок прошёл — товар свободен, заказ виден как истёкший."""
    item = product(root_client, shop["id"], stock="4")
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    ref = f"web-{_uniq()}"
    zakaz = _zakaz(root_client, key, ref, [{"id": item["id"], "quantity": "4"}], reserve_minutes=5).json()
    assert root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]["available_milli"] == 0

    with SessionLocal() as db:
        from datetime import timedelta

        from core.utils import now_utc
        from database.repositories import documents as documents_repo

        row = documents_repo.get_by_site_ref(db, ref)
        row.reserved_until = now_utc().replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()

    assert root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]["available_milli"] == 4000
    assert root_client.get(f"{SITE}/orders/{ref}", headers={H: key}).json()["reserve_expired"] is True
    istekshie = root_client.get(f"{API}/orders", params={"reserve": "expired"}).json()["items"]
    assert any(o["number"] == zakaz["number"] for o in istekshie), "очередь на разбор, а не мусор"


def test_otmena_do_nakladnoy_i_otkaz_posle(root_client, shop):
    item = product(root_client, shop["id"], stock="3")
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    ref = f"web-{_uniq()}"
    _zakaz(root_client, key, ref, [{"id": item["id"], "quantity": "1"}])
    r = root_client.post(f"{SITE}/orders/{ref}/cancel", headers={H: key})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]["available_milli"] == 3000
    assert root_client.post(f"{SITE}/orders/{ref}/cancel", headers={H: key}).status_code == 200, "повтор отмены безвреден"

    ref2 = f"web-{_uniq()}"
    zakaz = _zakaz(root_client, key, ref2, [{"id": item["id"], "quantity": "1"}]).json()
    vnutri = root_client.get(f"{API}/orders", params={"search": zakaz["number"]}).json()["items"][0]
    r = root_client.post(f"{API}/orders/{vnutri['id']}/close", json={"warehouse_id": shop["id"]})
    assert r.status_code == 200, r.text
    r = root_client.post(f"{SITE}/orders/{ref2}/cancel", headers={H: key})
    assert r.status_code == 409 and r.json()["error"]["code"] == "order_already_fulfilled"


def test_chuzhoy_zakaz_nevidim(root_client, shop):
    item = product(root_client, shop["id"], stock="3")
    odin = make_key(root_client, ALL, shop["id"])["key"]
    drugoy = make_key(root_client, ALL, shop["id"])["key"]
    ref = f"web-{_uniq()}"
    _zakaz(root_client, odin, ref, [{"id": item["id"], "quantity": "1"}])
    assert root_client.get(f"{SITE}/orders/{ref}", headers={H: drugoy}).status_code == 404
    r = _zakaz(root_client, drugoy, ref, [{"id": item["id"], "quantity": "1"}])
    assert r.status_code == 404, "чужой site_ref выглядит как несуществующий, а не «занят»"


# --- регистрация и заявка ------------------------------------------------------


def test_registratsiya_otvechaet_odnoy_formoy_i_ne_pravit_chuzhuyu_kartochku(root_client, shop):
    # Ключ без склада: регистрации зал не нужен, и склад у него не спрашивается.
    key = make_key(root_client, ["customers.write", "catalog.read"])["key"]
    email = f"anna{_uniq()}@example.com"
    r = root_client.post(f"{SITE}/customers", json={"name": "Anna", "email": email, "consent": False}, headers={H: key})
    assert r.status_code == 422 and r.json()["error"]["code"] == "consent_required"
    r = root_client.post(f"{SITE}/customers", json={"name": "Anna", "consent": True}, headers={H: key})
    assert r.status_code == 422 and r.json()["error"]["code"] == "contact_required"

    r = root_client.post(
        f"{SITE}/customers",
        json={"name": "Anna Petrenko", "email": email, "consent": True, "consent_ref": "terms-2026-05"},
        headers={H: key},
    )
    assert r.status_code == 202, r.text
    pervyy = r.json()
    assert set(pervyy) == {"customer_ref", "status"} and len(pervyy["customer_ref"]) == 32
    vtoroy = root_client.post(
        f"{SITE}/customers",
        json={"name": "ДРУГОЕ ИМЯ", "email": email, "phone": "+380671234567", "consent": True},
        headers={H: key},
    ).json()
    assert vtoroy == pervyy, "«завели» и «узнали» неотличимы по форме"

    klienty = root_client.get(f"{API}/clients", params={"search": email}).json()["items"]
    assert len(klienty) == 1 and klienty[0]["name"] == "Anna Petrenko", "чужая карточка не переписана"
    assert klienty[0]["source"] == "site"
    lenta = root_client.get(f"{API}/clients/{klienty[0]['id']}/notes").json()["items"]
    assert any("Registered on the site" in n["body"] for n in lenta), "новые сведения — текстом в ленту"
    zhurnal = root_client.get(f"{API}/audit", params={"action": "customer.registered"}).json()["items"]
    assert any(e["source_ref"] == "terms-2026-05" and e["source"] == "site_api" for e in zhurnal)

    drugoy_klyuch = make_key(root_client, ["customers.write"])["key"]
    r = root_client.post(f"{SITE}/customers", json={"name": "Anna", "email": email, "consent": True}, headers={H: drugoy_klyuch})
    assert r.json()["customer_ref"] != pervyy["customer_ref"], "разные ключи — разные ссылки на одного человека"


def test_zayavka_po_klyuchu(root_client, shop):
    key = make_key(root_client, ["leads.write"])["key"]
    r = root_client.post(f"{SITE}/leads", json={"name": "Иван", "phone": "+380670000000", "message": "хочу"}, headers={H: key})
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    r = root_client.post(f"{SITE}/leads", json={"name": "Иван"}, headers={H: key})
    assert r.status_code == 422 and r.json()["error"]["code"] == "contact_required"


# --- снимок без ключа ----------------------------------------------------------


def test_snimok_tovara_otdayotsya_bez_klyucha_tolko_opublikovannomu(root_client, shop, tmp_path):
    from PIL import Image

    item = product(root_client, shop["id"], stock="1")
    skrytyy = product(root_client, stock="")
    kartinka = tmp_path / "foto.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(kartinka)
    with kartinka.open("rb") as f:
        r = root_client.post(f"{STOCK}/products/{item['id']}/photos", files={"file": ("foto.png", f, "image/png")})
    assert r.status_code == 201, r.text
    with kartinka.open("rb") as f:
        r2 = root_client.post(f"{STOCK}/products/{skrytyy['id']}/photos", files={"file": ("foto.png", f, "image/png")})
    assert r2.status_code == 201, r2.text

    key = make_key(root_client, ALL, shop["id"])["key"]
    karta = root_client.get(f"{SITE}/catalog/{item['id']}", headers={H: key}).json()
    assert karta["photos"] and karta["photos"][0]["url"].startswith("/media/product/")
    anon = root_client.__class__(root_client.app)
    r = anon.get(karta["photos"][0]["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"
    assert anon.get(karta["photos"][0]["thumb_url"]).status_code == 200

    with SessionLocal() as db:
        from database.repositories import warehouse as warehouse_repo

        uid = warehouse_repo.list_product_photos(db, skrytyy["id"])[0].photo_uid
    assert anon.get(f"/media/product/{uid}.webp").status_code == 404, "неопубликованный товар выглядит как несуществующий"
    assert anon.get("/media/product/..%2F..%2Fx.webp").status_code == 404


# --- консоль -------------------------------------------------------------------


def test_konsol_vydayot_i_otzyvaet_klyuch(root_client, shop, capsys):
    from scripts import apikey

    assert apikey.main(["new", "--name", "консоль", "--scopes", "catalog.read,stock.read", "--warehouse", str(shop["id"]), "--days", "0"]) == 0
    vyvod = capsys.readouterr().out
    assert "ОДИН раз" in vyvod and api_key_service.HEADER in vyvod
    raw = [s.strip() for s in vyvod.splitlines() if s.startswith("  ") and len(s.strip()) > 30][-1]
    assert root_client.get(f"{SITE}/catalog", headers={H: raw}).status_code == 200
    assert apikey.main(["list"]) == 0
    spisok = capsys.readouterr().out
    assert "консоль" in spisok and "БЕССРОЧНЫЙ" in spisok
    key_id = next(int(s.split()[0]) for s in spisok.splitlines() if "консоль" in s)
    assert apikey.main(["revoke", str(key_id)]) == 0
    assert root_client.get(f"{SITE}/catalog", headers={H: raw}).status_code == 401
    assert apikey.main(["new", "--name", "x", "--scopes", "stock.read"]) == 1
    assert "warehouse_required" in capsys.readouterr().err


def test_istekshuyu_bron_mozhno_prodlit(root_client, shop):
    """Истёкшая бронь — очередь на разбор; разбор бывает и таким: «продлить на
    три дня» (план З-06). Без брони продлевать нечего."""
    item = product(root_client, shop["id"], stock="6")
    key = make_key(root_client, ALL, shop["id"], stock_mode="exact")["key"]
    ref = f"web-{_uniq()}"
    zakaz = _zakaz(root_client, key, ref, [{"sku": item["sku"], "quantity": "2"}], reserve_minutes=30).json()
    with SessionLocal() as db:
        from datetime import timedelta

        from core.utils import now_utc
        from database.repositories import documents as documents_repo

        row = documents_repo.get_by_site_ref(db, ref)
        row.reserved_until = now_utc().replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    vnutri = root_client.get(f"{API}/orders", params={"search": zakaz["number"]}).json()["items"][0]
    assert vnutri["reserve_expired"] is True

    prodlen = root_client.post(f"{API}/orders/{vnutri['id']}/reserve", json={"days": 2})
    assert prodlen.status_code == 200, prodlen.text
    assert prodlen.json()["reserve_expired"] is False and prodlen.json()["reserved_until"]
    istekshie = root_client.get(f"{API}/orders", params={"reserve": "expired"}).json()["items"]
    assert not any(o["id"] == vnutri["id"] for o in istekshie)
    assert root_client.get(f"{SITE}/stock?id={item['id']}", headers={H: key}).json()["items"][0]["available_milli"] == 4000
    istoriya = root_client.get(f"{API}/orders/{vnutri['id']}").json()["events"]
    assert any("reservation extended" in (e["note"] or "") for e in istoriya)

    bez_broni = root_client.post(f"{API}/orders", json={"kind": "sales_order"}).json()
    otkaz = root_client.post(f"{API}/orders/{bez_broni['id']}/reserve", json={"days": 2})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "no_reservation"
